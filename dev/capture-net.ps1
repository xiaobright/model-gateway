# 上游黑洞抓包（Windows pktmon）
#
# 用途：「请求发出去了但上游没收到 / 完全连不上」这类故障时，抓网关出口方向的包，
# 看 SYN 到底出没出去、有没有 SYN-ACK、数据发出去后对端有没有任何回应。
# 典型场景：走 Clash 时代理路径黑洞（切直连才好）、或直连时对端静默丢包。
#
# 用法（**管理员** PowerShell，在项目根目录）：
#   pwsh -NoProfile -File dev\capture-net.ps1 -Start          # 开始抓包（自动解析所有已启用上游的 IP）
#   ... 复现问题（让 codex 发几次请求，等到卡住）...
#   pwsh -NoProfile -File dev\capture-net.ps1 -Stop           # 停止并转成 pcapng
#
# 可选参数：
#   -Upstream AgentRouter,DDD   只抓指定供应商（按名字过滤，减小噪音）
#   -Ip 1.2.3.4,5.6.7.8         额外要抓的 IP（比如 Clash 节点服务器地址）
#   -ClashPort 7890             Clash 混合端口，默认 7890；传 0 表示不抓这一路
#   -FullPackets                记录完整包体（默认每包只记 256 字节，够看握手/流向/大小）
#   -MaxSizeMb 256              ETL 文件大小上限，默认 256
#
# 产出：dev\captures\PktMon-<时间戳>.pcapng，可用 Wireshark 打开。
# 抓包含未加密的请求/响应元数据，分享前先检查有没有敏感内容。
#
# pktmon 是 Windows 自带组件，不需要额外安装。

param(
    [switch]$Start,
    [switch]$Stop,
    [switch]$Status,
    [string]$Upstream = "",
    [string]$Ip = "",
    [int]$ClashPort = 7890,
    [switch]$FullPackets,
    [int]$MaxSizeMb = 256
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$captureDir = Join-Path $root "dev\captures"
$python = Join-Path $root ".venv\Scripts\python.exe"

function Test-Admin {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    (New-Object Security.Principal.WindowsPrincipal($id)).IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Get-UpstreamIps([string]$only) {
    # 从正式库读已启用供应商的站址，解析成 IP；库不在或读失败就返回空，由调用方决定退路
    if (-not (Test-Path -LiteralPath $python)) { return @() }
    $code = @'
import json, sqlite3, urllib.parse
con = sqlite3.connect("data/gateway.db")
rows = con.execute("SELECT name, base_url FROM upstreams WHERE enabled=1").fetchall()
out = []
for name, base in rows:
    host = urllib.parse.urlsplit(base).hostname or ""
    if host:
        out.append({"name": name, "host": host})
print(json.dumps(out))
'@
    $tmp = New-TemporaryFile
    try {
        Set-Content -LiteralPath $tmp -Value $code -Encoding utf8
        $raw = & $python $tmp 2>$null
        if ($LASTEXITCODE -ne 0) { return @() }
        $entries = $raw | ConvertFrom-Json
    } finally {
        Remove-Item -LiteralPath $tmp -Force
    }
    if ($only) {
        $want = $only.Split(",") | ForEach-Object { $_.Trim() }
        $entries = $entries | Where-Object { $want -contains $_.name }
    }
    $ips = @()
    foreach ($e in $entries) {
        try {
            $resolved = [System.Net.Dns]::GetHostAddresses($e.host) |
                Where-Object { $_.AddressFamily -eq "InterNetwork" }
            foreach ($r in $resolved) { $ips += $r.IPAddressToString }
        } catch {
            Write-Warning "解析不了 $($e.name) ($($e.host))，跳过"
        }
    }
    return ($ips | Sort-Object -Unique)
}

if (-not (Test-Admin)) {
    Write-Error "需要管理员权限：请用管理员 PowerShell 重新运行。"
    exit 1
}

if ($Status -or (-not $Start -and -not $Stop)) {
    pktmon status
    Write-Output "--- 当前过滤器 ---"
    pktmon filter list
    exit 0
}

if ($Stop) {
    pktmon stop
    $etl = Get-ChildItem -Path $env:USERPROFILE -Recurse -Filter "PktMon*.etl" -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTime -Descending | Select-Object -First 1
    if (-not $etl) {
        Write-Warning "没找到 ETL 文件；如果 -Start 时指定了 --file-name，请手动 pktmon etl2pcap <file> --out <pcapng>"
        exit 1
    }
    New-Item -ItemType Directory -Force -Path $captureDir | Out-Null
    $stamp = Get-Date -Format "yyyyMMdd-HHmmss"
    $out = Join-Path $captureDir "PktMon-$stamp.pcapng"
    pktmon etl2pcap $etl.FullName --out $out
    if (Test-Path -LiteralPath $out) {
        Write-Output "抓包已生成：$out"
        Write-Output "用 Wireshark 打开；先看网关进程（pythonw.exe）到上游 IP 的 TCP 流："
        Write-Output "  过滤器示例：ip.addr == 8.214.160.125 && tcp.port == 443"
    } else {
        Write-Warning "转换失败，ETL 还在：$($etl.FullName)"
    }
    exit 0
}

# -Start
pktmon filter remove 2>$null
$filters = 0
foreach ($ip in (Get-UpstreamIps $Upstream)) {
    pktmon filter add "up-$ip" -i $ip | Out-Null
    $filters++
}
foreach ($extra in ($Ip.Split(",") | ForEach-Object { $_.Trim() } | Where-Object { $_ })) {
    pktmon filter add "extra-$extra" -i $extra | Out-Null
    $filters++
}
if ($ClashPort -gt 0) {
    pktmon filter add "clash-mixed" -p $ClashPort | Out-Null
    $filters++
}
pktmon filter add "dns" -p 53 | Out-Null
$filters++

if ($filters -gt 30) {
    # 过滤器上限 32：IP 太碎就退回到按端口抓（噪音大但完整，事后在 Wireshark 里过）
    Write-Warning "过滤器数量 $filters 接近上限，退回按 443/7890/53 端口抓全部流量"
    pktmon filter remove 2>$null
    pktmon filter add "tcp443" -p 443 | Out-Null
    if ($ClashPort -gt 0) { pktmon filter add "clash-mixed" -p $ClashPort | Out-Null }
    pktmon filter add "dns" -p 53 | Out-Null
}

New-Item -ItemType Directory -Force -Path $captureDir | Out-Null
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$etlPath = Join-Path $captureDir "PktMon-$stamp.etl"
$pktSize = if ($FullPackets) { 0 } else { 256 }
Write-Output "开始抓包 -> $etlPath（过滤器 $(pktmon filter list | Select-String -Pattern 'Filter' | Measure-Object | Select-Object -ExpandProperty Count) 条）"
Write-Output "复现问题后运行：pwsh -NoProfile -File dev\capture-net.ps1 -Stop"
pktmon start --capture --file-name $etlPath --file-size $MaxSizeMb --pkt-size $pktSize

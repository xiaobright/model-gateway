"""定位「上游 sensitive words detected」到底命中的是哪一段。

上游（站A）的内容审核是黑盒，只回一句 `sensitive words detected`，不告诉你
哪个词。这个脚本用**分块 + 块内二分**把请求体扫一遍，找出所有触发点，再给每个触发点
试几种「中性化」写法，最后直接吐出可粘贴的规则。

用法::

    # 1. 开抓包，复现一次
    echo {} > data/capture-stream.flag

    # 2. 对着抓到的请求跑
    .venv/Scripts/python.exe dev/find-sensitive-line.py data/captured_stream/<时间戳>-<模型>

    # 3. 把输出里的 rules 粘进管理页「上游站点 → 敏感词绕行」

为什么是「分块 + 二分」而不是纯逐行、也不是纯二分：

- 纯逐行最慢（42 行要 ~46 次请求），而且**抓不到跨行触发** —— 每行单发都能过，
  结果 0 命中。
- 纯二分快，但**一趟只能找到一处**。必须迭代：找到 → 洗掉 → 整段重测 → 还挂就再二分。
  少做那个「整段重测」的确认步，就会像第一次那样只配了一条规则、照样 500。
- 分块（默认 8 行）先整块试，挂了的块再二分：请求数约 1/3，且块内二分卡住
  （两半都不挂 = 跨中点，或块内不止一处）时自动退化成逐行，不会漏。

两个必须注意的坑，脚本都已处理：

1. **探测必须隔离**：只在原消息里替换待试的那段、留下其余消息是不行的 —— 别的消息里
   只要还有一处触发，试哪段都是 500，全是假阳性。默认把待试文本单独作为一条 user 消息
   发出去（其余顶层参数照抄）。
2. **已配的规则会掩盖命中**：脚本会临时清空网关里的绕行规则再还原 —— 不清的话规则把
   命中行洗掉了，连对照项都变 200，全是漏报。
"""

from __future__ import annotations

import argparse
import copy
import json
import sys
from pathlib import Path

import httpx

DEFAULT_API = "http://127.0.0.1:8317"
DEFAULT_CHUNK = 8
MAX_ROUNDS = 5


# ---------------------------------------------------------------- 中性化候选
# 都是「只改标点/空白、不改词」的保守变换 —— 目标是骗过误报，不是改意思。
def neutral_candidates(line: str) -> list[str]:
    out = []
    seen = {line}

    def add(text: str) -> None:
        if text and text != line and text not in seen:
            seen.add(text)
            out.append(text)

    for marker in ("- ", "* ", "+ "):
        if line.startswith(marker):
            add(line[len(marker):])
    # 箭头 -> ASCII：实测救回过一条「原文带 → 就挂、换成 -> 就过」的规则
    for arrow, ascii_arrow in (("→", "->"), ("←", "<-"), ("↔", "<->"), ("⇒", "=>")):
        if arrow in line:
            add(line.replace(arrow, ascii_arrow))
            break
    if line.startswith('"') and line.endswith('"'):
        add(line[1:-1])
    add(" ".join(line.split()))
    stripped = line.strip()
    if stripped != line:
        add(stripped)
    return out


# ---------------------------------------------------------------- 请求体读写
def message_texts(payload: dict) -> list[tuple[int, str, str]]:
    """[(消息下标, role, 文本)] —— 兼容 str 和 [{'type':'text',...}] 两种 content。"""
    out = []
    for i, msg in enumerate(payload.get("messages") or []):
        if not isinstance(msg, dict):
            continue
        role = str(msg.get("role", "?"))
        content = msg.get("content")
        if isinstance(content, str):
            out.append((i, role, content))
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and isinstance(part.get("text"), str):
                    out.append((i, role, part["text"]))
    return out


def set_text(payload: dict, index: int, text: str) -> dict:
    """把第 index 条消息的 content 换成 text（保持它原本的形状）。"""
    new = copy.deepcopy(payload)
    msg = new["messages"][index]
    if isinstance(msg.get("content"), list):
        for part in msg["content"]:
            if isinstance(part, dict) and "text" in part:
                part["text"] = text
                break
        else:
            msg["content"] = text
    else:
        msg["content"] = text
    return new


def drop_lines(payload: dict, banned: list[str]) -> dict:
    """把所有文本里等于 banned 的行整行删掉 —— 用来验证「还剩不剩别的触发点」。"""
    new = copy.deepcopy(payload)
    banned_set = set(banned)

    def clean(text: str) -> str:
        return "\n".join(l for l in text.split("\n") if l not in banned_set)

    for msg in new.get("messages") or []:
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")
        if isinstance(content, str):
            msg["content"] = clean(content)
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and isinstance(part.get("text"), str):
                    part["text"] = clean(part["text"])
    return new


# ---------------------------------------------------------------- 扫描
class Scanner:
    def __init__(self, client: httpx.Client, api: str, payload: dict, max_calls: int) -> None:
        self.c = client
        self.api = api
        self.payload = payload
        self.max_calls = max_calls
        self.calls = 0

    def bad_payload(self, body: dict) -> bool:
        """发一个请求体，看是不是 500。

        探测只求状态码，所以**必须把 max_tokens 压到 16**：审核是在生成之前做的，
        500 照样马上回来，但过审的那些请求就不会真去生成几万 token。第一版照抄了
        抓来的请求体（max_tokens=32000），150 次过审请求白烧了 16MB 生成内容 ——
        对一个按量计费的上游来说，这才是真正的代价，比触发审核本身贵得多。
        """
        if self.calls >= self.max_calls:
            raise RuntimeError(f"已达到 --max-calls={self.max_calls} 上限，停手")
        self.calls += 1
        try:
            resp = self.c.post(
                f"{self.api}/v1/chat/completions",
                json={**body, "stream": False, "max_tokens": 16},
            )
            return resp.status_code == 500
        except httpx.HTTPError as exc:
            print(f"  请求失败：{exc}", file=sys.stderr)
            return False

    def bad_text(self, text: str) -> bool:
        """隔离探测：把 text 单独作为一条 user 消息发出去。"""
        return self.bad_payload({**self.payload, "messages": [{"role": "user", "content": text}]})

    def minimize(self, lines: list[str]) -> list[str]:
        """二分收敛到一个极小失败块；两半都不挂时退化成逐行。"""
        lo, hi = 0, len(lines)
        while hi - lo > 1:
            mid = (lo + hi) // 2
            if self.bad_text("\n".join(lines[lo:mid])):
                hi = mid
            elif self.bad_text("\n".join(lines[mid:hi])):
                lo = mid
            else:
                break  # 跨中点组合，或块内不止一处
        block = lines[lo:hi]
        if len(block) == 1:
            return block
        singles = [l for l in block if self.bad_text(l)]
        return singles or block  # 逐行也定位不到 = 跨行触发，整块返回

    def find_all_in(self, lines: list[str]) -> list[str]:
        """在 lines 里把触发点全找出来（不只是第一个）。"""
        found: list[str] = []
        work = [l for l in lines if l.strip()]
        while work and self.bad_text("\n".join(work)):
            block = self.minimize(work)
            if not block:
                break
            found.extend(block)
            banned = set(block)
            remaining = [l for l in work if l not in banned]
            if len(remaining) == len(work):
                break  # 没进展，别死循环
            work = remaining
        return found


def chunks(items: list[str], size: int) -> list[list[str]]:
    return [items[i:i + size] for i in range(0, len(items), size)] or [[]]


# ---------------------------------------------------------------- 主流程
def main() -> int:
    ap = argparse.ArgumentParser(
        description="定位上游 sensitive words detected 命中的文本",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("target", help="抓包目录（含 request.json）或 request.json 本身")
    ap.add_argument("--api", default=DEFAULT_API, help=f"网关地址（默认 {DEFAULT_API}）")
    ap.add_argument("--chunk", type=int, default=DEFAULT_CHUNK,
                    help=f"分块行数（默认 {DEFAULT_CHUNK}；设为 1 退化为逐行）")
    ap.add_argument("--no-suggest", action="store_true", help="只定位，不试中性化写法")
    ap.add_argument("--max-calls", type=int, default=80,
                    help="请求数硬上限（默认 80），超了直接停手 —— 别对着按量计费的上游瞎打")
    ap.add_argument("--timeout", type=float, default=60.0)
    args = ap.parse_args()

    target = Path(args.target)
    request_path = target / "request.json" if target.is_dir() else target
    if not request_path.exists():
        print(f"找不到 {request_path}", file=sys.stderr)
        return 2

    payload = json.loads(request_path.read_text("utf-8", errors="replace"))
    api = args.api.rstrip("/")

    with httpx.Client(timeout=args.timeout) as c:
        saved = c.get(f"{api}/admin/api/rewrite-rules").json()["rules"]
        c.put(f"{api}/admin/api/rewrite-rules", json={"rules": []})

        try:
            sc = Scanner(c, api, payload, args.max_calls)

            if not sc.bad_payload({**payload, "stream": False}):
                print("这个请求体现在根本不 500 —— 要么已经修好，要么触发点不在这份请求体里。")
                return 1

            texts = message_texts(payload)
            plan = sum(
                max(1, -(-len([l for l in t.split("\n") if l.strip()]) // max(1, args.chunk)))
                for _i, _r, t in texts
            )
            print(f"先分块扫 {plan} 块（每块 {max(1, args.chunk)} 行），"
                  f"挂了的块再二分；请求数上限 {args.max_calls}\n")

            banned: list[str] = []

            # 第一轮：按消息分块扫
            for index, role, text in texts:
                lines = [l for l in text.split("\n") if l.strip()]
                for block in chunks(lines, max(1, args.chunk)):
                    if not block:
                        continue
                    if not sc.bad_text("\n".join(block)):
                        continue
                    found = sc.find_all_in(block)
                    for f in found:
                        print(f"  ✗ msg{index}/{role}: {f[:80]}")
                    banned.extend(found)

            # 第二轮：删掉已找到的，整段重测 —— 这一步不能省，省了就会漏
            for _ in range(MAX_ROUNDS):
                cleaned = drop_lines(payload, banned)
                if not sc.bad_payload({**cleaned, "stream": False}):
                    break
                rest: list[str] = []
                for _i, _r, text in message_texts(cleaned):
                    rest.extend(l for l in text.split("\n") if l.strip())
                more = sc.find_all_in(rest)
                if not more:
                    print("\n剩下的触发点定位不出来（跨行/跨消息组合，或不在文本里）。")
                    break
                for f in more:
                    print(f"  ✗ 二轮: {f[:80]}")
                banned.extend(more)

            # 去重保序
            banned = list(dict.fromkeys(banned))
            print(f"\n共 {sc.calls} 次请求，命中 {len(banned)} 处")
            if not banned:
                return 1

            if args.no_suggest:
                print(json.dumps([{"from": b, "to": ""} for b in banned],
                                 ensure_ascii=False, indent=2))
                return 0

            print("\n试中性化写法（取第一条能过审的）：")
            rules = []
            for line in banned:
                pick = None
                for cand in neutral_candidates(line):
                    ok = not sc.bad_text(cand)
                    print(f"   [{'200' if ok else '500'}] {cand[:80]}")
                    if ok and pick is None:
                        pick = cand
                if pick is None:
                    print(f"  !! 没有能过审的写法，只能整段删掉：{line[:60]}")
                    pick = ""
                rules.append({"from": line, "to": pick})

            print("\n可以粘进「敏感词绕行」的规则：")
            print(json.dumps(rules, ensure_ascii=False, indent=2))
            return 0
        finally:
            c.put(f"{api}/admin/api/rewrite-rules", json={"rules": saved})
            print(f"\n（已还原原有 {len(saved)} 条规则）")


if __name__ == "__main__":
    raise SystemExit(main())

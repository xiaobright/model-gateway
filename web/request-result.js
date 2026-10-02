/* 结果与 HTTP 状态分开：200 不能盖过截断、断流和重试。
   实时卡片、记录筛选与详情共用这份口径。 */
const NOTES = {
  truncated: ['流被截断', 'warn'], connect_failed: ['连接失败', 'crit'],
  upstream_abort: ['上游断流', 'crit'], client_abort: ['客户端断开', ''],
  manual_abort: ['手动中断', ''], stall_timeout: ['无内容超时', 'warn'],
  failed_over: ['失败后换站', 'warn'], hold_retry: ['截断后重试', 'warn'],
};

export function requestResult(record) {
  const note = record.note || '';
  if (NOTES[note]) return { label: NOTES[note][0], tone: NOTES[note][1], issue: true };
  if (note && note !== 'ok') return { label: note, tone: 'warn', issue: true };
  const status = Number(record.status);
  if (!status) return { label: '未收到响应', tone: '', issue: true };
  if (status >= 400) return { label: '请求失败', tone: status >= 500 || status === 401 || status === 403 ? 'crit' : 'warn', issue: true };
  return { label: status >= 300 ? '重定向' : '正常结束', tone: status >= 300 ? '' : 'good', issue: false };
}

/** 时间显示工具：消息级本地时间戳。审计以服务端 created_at 为准。 */

export const pad2 = (n) => String(n).padStart(2, '0')

export function formatClock(ts) {
  const d = new Date(ts)
  return `${pad2(d.getHours())}:${pad2(d.getMinutes())}`
}

export function formatDateTime(ts) {
  const d = new Date(ts)
  return `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())} ${formatClock(ts)} · 本地时间，审计以服务端为准`
}

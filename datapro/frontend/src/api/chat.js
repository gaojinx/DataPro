/**
 * SSE 对话客户端（接口规范 §4.1）。
 * 用 fetch + ReadableStream 实现 POST 流式（EventSource 不支持 POST）。
 * 支持 AbortController 中断生成。
 * 事件类型：node / clarify / result / reject / escalate / error
 */
export async function streamChat({ sessionId, message, onEvent, signal }) {
  const resp = await fetch('/api/v1/chat', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ session_id: sessionId, message }),
    signal,
  })

  if (!resp.ok) {
    let detail = `HTTP ${resp.status}`
    try {
      const body = await resp.json()
      detail = body.detail || detail
    } catch { /* 忽略非 JSON 响应 */ }
    onEvent({ type: 'error', message: detail, reason_code: 'HTTP' })
    return
  }

  const reader = resp.body.getReader()
  const decoder = new TextDecoder('utf-8')
  let buffer = ''

  while (true) {
    const { done, value } = await reader.read()
    if (done) break
    buffer += decoder.decode(value, { stream: true })
    // SSE 帧以空行分隔
    const frames = buffer.split('\n\n')
    buffer = frames.pop() ?? ''
    for (const frame of frames) {
      const line = frame.trim()
      if (!line.startsWith('data:')) continue
      try {
        onEvent(JSON.parse(line.slice(5).trim()))
      } catch { /* 跳过不完整帧 */ }
    }
  }
}

export async function sendFeedback(queryId, rating, reason) {
  const q = `rating=${rating}${reason ? `&reason=${encodeURIComponent(reason)}` : ''}`
  const resp = await fetch(`/api/v1/queries/${queryId}/feedback?${q}`, { method: 'POST' })
  return resp.ok
}

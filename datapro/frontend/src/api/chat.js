/**
 * SSE 对话客户端（接口规范 §4.1）。
 * 用 fetch + ReadableStream 实现 POST 流式（EventSource 不支持 POST）。
 * 支持 AbortController 中断生成。
 * 事件类型：node / clarify / result / reject / escalate / error
 */
import { authHeaders, sessionExpired } from './auth'
import { toApiError } from './errors'

export async function streamChat({ sessionId, message, attachments, onEvent, signal }) {
  const resp = await fetch('/api/v1/chat', {
    method: 'POST',
    headers: authHeaders({ 'Content-Type': 'application/json' }),
    // attachments：附件引用快照（file_id/name/ext/size/origin），随消息落库供历史恢复
    body: JSON.stringify({ session_id: sessionId, message,
                           attachments: attachments?.length ? attachments : undefined }),
    signal,
  })

  if (resp.status === 401) {
    sessionExpired()
    onEvent({ type: 'error', auth: true, message: '登录已过期，请重新登录', reason_code: 'HTTP' })
    return
  }

  if (!resp.ok) {
    const data = await resp.json().catch(() => ({}))
    const err = toApiError(resp, data)
    onEvent({ type: 'error', message: err.message, reason_code: err.code, code: err.code })
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
  const resp = await fetch(`/api/v1/queries/${queryId}/feedback?${q}`, {
    method: 'POST',
    headers: authHeaders(),
  })
  if (resp.status === 401) sessionExpired()
  return resp.ok
}

/** 会话历史消息（升序）：跨设备/清缓存后恢复消息流；附件引用可点击预览。 */
export async function fetchSessionMessages(sessionId, limit = 200) {
  const resp = await fetch(`/api/v1/sessions/${sessionId}/messages?limit=${limit}`, {
    headers: authHeaders(),
  })
  if (resp.status === 401) { sessionExpired(); throw new Error('登录已过期') }
  if (!resp.ok) {
    const data = await resp.json().catch(() => ({}))
    throw new Error(data.detail || `读取历史失败（${resp.status}）`)
  }
  const data = await resp.json()
  return data.messages || []
}

/** 服务端会话归档：本人参与过的会话列表（跨设备可见）。 */
export async function fetchSessionArchive(limit = 100) {
  const resp = await fetch(`/api/v1/sessions/archive?limit=${limit}`, { headers: authHeaders() })
  if (resp.status === 401) { sessionExpired(); throw new Error('登录已过期') }
  if (!resp.ok) throw new Error(`读取会话归档失败（${resp.status}）`)
  const data = await resp.json()
  return data.sessions || []
}

/**
 * 登录态与请求头（B12）：Token 存 localStorage，所有受保护接口带 Bearer。
 * 任何接口返回 401 时统一调用 sessionExpired()：清本地登录态并通知 App 回到登录页。
 */
const TOKEN_KEY = 'dp_token'
const USER_KEY = 'dp_user'
const AUTH_EXPIRED_EVENT = 'dp-auth-expired'

let expiredNotified = false

export const getToken = () => localStorage.getItem(TOKEN_KEY) || ''

export function getUser() {
  try { return JSON.parse(localStorage.getItem(USER_KEY) || 'null') } catch { return null }
}

export function setAuth(token, user) {
  localStorage.setItem(TOKEN_KEY, token)
  localStorage.setItem(USER_KEY, JSON.stringify(user))
  expiredNotified = false          // 重新登录后允许再次触发过期通知
}

export function clearAuth() {
  localStorage.removeItem(TOKEN_KEY)
  localStorage.removeItem(USER_KEY)
}

/** 会话失效：清本地登录态 + 通知 App 跳回登录页（同一轮只通知一次，避免并发请求重复弹提示）。 */
export function sessionExpired() {
  clearAuth()
  if (expiredNotified) return
  expiredNotified = true
  window.dispatchEvent(new CustomEvent(AUTH_EXPIRED_EVENT))
}

/** App 启动时注册；返回取消监听的函数。 */
export function onAuthExpired(handler) {
  const listener = () => handler()
  window.addEventListener(AUTH_EXPIRED_EVENT, listener)
  return () => window.removeEventListener(AUTH_EXPIRED_EVENT, listener)
}

export function authHeaders(extra = {}) {
  const token = getToken()
  return token ? { ...extra, Authorization: `Bearer ${token}` } : { ...extra }
}

export async function login(username, password) {
  const resp = await fetch('/api/v1/auth/login', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ username, password }),
  })
  const data = await resp.json().catch(() => ({}))
  if (!resp.ok) {
    // 登录接口返回 401 是「账号或密码错误」，不应触发 sessionExpired 的跳转通知，
    // 这里直接拼装可读文案（保留后端 detail 人话，附带错误码与追踪号）。
    const code = String(data.code || '').toUpperCase() || 'E008'
    const trace = data.trace_id ? `（追踪号 ${data.trace_id}）` : ''
    throw new Error(`${data.detail || data.message || '账号或密码错误'} [${code}]${trace}`)
  }
  setAuth(data.token, data.user)
  return data.user
}

export async function logout() {
  try {
    await fetch('/api/v1/auth/logout', { method: 'POST', headers: authHeaders() })
  } catch { /* 网络异常也要清本地登录态 */ }
  clearAuth()
}

// ---- Token 续期（B12） ----
function decodePayload(token) {
  try {
    const raw = token.split('.')[0].replace(/-/g, '+').replace(/_/g, '/')
    const padded = raw + '='.repeat((4 - (raw.length % 4)) % 4)
    return JSON.parse(atob(padded))
  } catch { return null }
}

export function tokenExpiresAt() {
  const payload = decodePayload(getToken())
  return payload?.exp ? payload.exp * 1000 : 0
}

export async function refreshAuth() {
  if (!getToken()) return false
  try {
    const resp = await fetch('/api/v1/auth/refresh', { method: 'POST', headers: authHeaders() })
    if (resp.status === 401) { sessionExpired(); return false }
    if (!resp.ok) return false
    const data = await resp.json()
    setAuth(data.token, data.user)
    return true
  } catch { return false }
}

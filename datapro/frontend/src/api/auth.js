/**
 * 登录态与请求头（B12）：Token 存 localStorage，所有受保护接口带 Bearer。
 */
const TOKEN_KEY = 'dp_token'
const USER_KEY = 'dp_user'

export const getToken = () => localStorage.getItem(TOKEN_KEY) || ''

export function getUser() {
  try { return JSON.parse(localStorage.getItem(USER_KEY) || 'null') } catch { return null }
}

export function setAuth(token, user) {
  localStorage.setItem(TOKEN_KEY, token)
  localStorage.setItem(USER_KEY, JSON.stringify(user))
}

export function clearAuth() {
  localStorage.removeItem(TOKEN_KEY)
  localStorage.removeItem(USER_KEY)
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
  if (!resp.ok) throw new Error(data.detail || `登录失败（${resp.status}）`)
  setAuth(data.token, data.user)
  return data.user
}

export async function logout() {
  try {
    await fetch('/api/v1/auth/logout', { method: 'POST', headers: authHeaders() })
  } catch { /* 网络异常也要清本地登录态 */ }
  clearAuth()
}

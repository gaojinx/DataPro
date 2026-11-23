/**
 * 设置/管理接口（功能清单 F17/F18/B16）。
 * 管理接口需 Bearer Token；非管理员由后端返回 403。
 */
import { authHeaders, clearAuth } from './auth'

const jsonHeaders = () => authHeaders({ 'Content-Type': 'application/json' })

async function handle(resp) {
  if (resp.status === 401) {
    clearAuth()
    throw new Error('登录已过期，请重新登录')
  }
  if (!resp.ok) {
    let detail = `HTTP ${resp.status}`
    try { detail = (await resp.json()).detail || detail } catch { /* 忽略 */ }
    throw new Error(detail)
  }
  return resp.json()
}

export const fetchLlmStatus = () =>
  fetch('/admin/llm/status').then(handle)

export const fetchApis = () =>
  fetch('/admin/llm-apis', { headers: authHeaders() }).then(handle)

export const saveApi = (data) =>
  data.id
    ? fetch(`/admin/llm-apis/${data.id}`, { method: 'PUT', headers: jsonHeaders(), body: JSON.stringify(data) }).then(handle)
    : fetch('/admin/llm-apis', { method: 'POST', headers: jsonHeaders(), body: JSON.stringify(data) }).then(handle)

export const deleteApi = (id) =>
  fetch(`/admin/llm-apis/${id}`, { method: 'DELETE', headers: authHeaders() }).then(handle)

export const setDefaultApi = (id) =>
  fetch(`/admin/llm-apis/${id}/default`, { method: 'POST', headers: authHeaders() }).then(handle)

export const testApi = (id) =>
  fetch(`/admin/llm-apis/${id}/test`, { method: 'POST', headers: authHeaders() }).then(handle)

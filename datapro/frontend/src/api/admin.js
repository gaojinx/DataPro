/**
 * 设置/管理接口（功能清单 F17/F18/B16）。
 * 管理接口需 Bearer Token；非管理员由后端返回 403。
 */
import { authHeaders } from './auth'
import { parseResponse } from './errors'

const jsonHeaders = () => authHeaders({ 'Content-Type': 'application/json' })

async function handle(resp) {
  return parseResponse(resp)
}

export const fetchLlmStatus = () =>
  fetch('/admin/llm/status', { headers: authHeaders() }).then(handle)

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

// ---- 用户管理（B12） ----
export const fetchUsers = () =>
  fetch('/admin/users', { headers: authHeaders() }).then(handle)

export const createUser = (data) =>
  fetch('/admin/users', { method: 'POST', headers: jsonHeaders(), body: JSON.stringify(data) }).then(handle)

export const updateUser = (id, data) =>
  fetch(`/admin/users/${id}`, { method: 'PATCH', headers: jsonHeaders(), body: JSON.stringify(data) }).then(handle)

export const deleteUser = (id) =>
  fetch(`/admin/users/${id}`, { method: 'DELETE', headers: authHeaders() }).then(handle)

// ---- 权限矩阵（B12） ----
export const fetchPermissions = () =>
  fetch('/admin/permissions', { headers: authHeaders() }).then(handle)

export const saveRolePermissions = (role, permissions) =>
  fetch(`/admin/permissions/roles/${role}`, {
    method: 'PUT',
    headers: jsonHeaders(),
    body: JSON.stringify({ permissions }),
  }).then(handle)

// ---- 用户级数据表权限（A02/S1） ----
export const fetchTableAccess = (userId) =>
  fetch(`/admin/users/${userId}/table-access`, { headers: authHeaders() }).then(handle)

export const saveTableAccess = (userId, data) =>
  fetch(`/admin/users/${userId}/table-access`, {
    method: 'PUT',
    headers: jsonHeaders(),
    body: JSON.stringify(data),
  }).then(handle)

export const fetchMetaTables = () =>
  fetch('/admin/metadata/tables', { headers: authHeaders() }).then(handle)

// ---- 对话记录（管理后台） ----
export const fetchRecords = (params = {}) => {
  const query = new URLSearchParams(
    Object.entries(params).filter(([, v]) => v !== '' && v !== null && v !== undefined),
  ).toString()
  return fetch(`/admin/records?${query}`, { headers: authHeaders() }).then(handle)
}

// ---- 口径裁决（D03）与会话回收（S07）：U-F10 管理端入口 ----
export const fetchRulings = () =>
  fetch('/admin/calibers/rulings', { headers: authHeaders() }).then(handle)

export const saveRuling = (term, data) =>
  fetch(`/admin/calibers/rulings/${encodeURIComponent(term)}`, {
    method: 'PUT',
    headers: jsonHeaders(),
    body: JSON.stringify(data),
  }).then(handle)

export const deleteRuling = (term) =>
  fetch(`/admin/calibers/rulings/${encodeURIComponent(term)}`, {
    method: 'DELETE',
    headers: authHeaders(),
  }).then(handle)

export const sweepSessions = () =>
  fetch('/admin/sessions/sweep', { method: 'POST', headers: authHeaders() }).then(handle)

// ---- 限定范围缓存（U-B03）：只读状态 + 人工清空 ----
export const fetchCacheStats = () =>
  fetch('/admin/cache/stats', { headers: authHeaders() }).then(handle)

export const clearCache = (scope = '') => {
  const query = scope ? `?scope=${encodeURIComponent(scope)}` : ''
  return fetch(`/admin/cache/clear${query}`, { method: 'POST', headers: authHeaders() }).then(handle)
}

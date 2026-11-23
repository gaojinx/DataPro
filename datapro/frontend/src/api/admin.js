/**
 * 设置/管理接口（功能清单 F17/F18/B16）。
 * 骨架阶段角色通过 X-User-Role 头传递（localStorage），正式认证见 B12。
 */
const roleHeaders = () => ({
  'Content-Type': 'application/json',
  'X-User-Role': localStorage.getItem('dp_role') || 'user',
})

async function handle(resp) {
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
  fetch('/admin/llm-apis', { headers: roleHeaders() }).then(handle)

export const saveApi = (data) =>
  data.id
    ? fetch(`/admin/llm-apis/${data.id}`, { method: 'PUT', headers: roleHeaders(), body: JSON.stringify(data) }).then(handle)
    : fetch('/admin/llm-apis', { method: 'POST', headers: roleHeaders(), body: JSON.stringify(data) }).then(handle)

export const deleteApi = (id) =>
  fetch(`/admin/llm-apis/${id}`, { method: 'DELETE', headers: roleHeaders() }).then(handle)

export const setDefaultApi = (id) =>
  fetch(`/admin/llm-apis/${id}/default`, { method: 'POST', headers: roleHeaders() }).then(handle)

export const testApi = (id) =>
  fetch(`/admin/llm-apis/${id}/test`, { method: 'POST', headers: roleHeaders() }).then(handle)

// 数据表权限自助申请（U-B05）：本人申请进度 + 管理员审批
import { authHeaders } from './auth'
import { parseResponse } from './errors'

async function parse(r) {
  return parseResponse(r)
}

/** 可申请的数据表（表名 + 中文名，不含字段） */
export async function fetchAccessCatalog() {
  const r = await fetch('/api/v1/access/catalog', { headers: authHeaders() })
  return parse(r)
}

export async function createAccessRequest({ table_name, reason = '', duration_days = 30 }) {
  const r = await fetch('/api/v1/access/requests', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...authHeaders() },
    body: JSON.stringify({ table_name, reason, duration_days }),
  })
  return parse(r)
}

export async function fetchMyAccessRequests(status = '', limit = 50) {
  const qs = new URLSearchParams({ limit: String(limit) })
  if (status) qs.set('status', status)
  const r = await fetch(`/api/v1/access/requests?${qs}`, { headers: authHeaders() })
  return parse(r)
}

/** 管理员：全部申请（可按状态过滤） */
export async function fetchAllAccessRequests(status = '', limit = 200) {
  const qs = new URLSearchParams({ limit: String(limit) })
  if (status) qs.set('status', status)
  const r = await fetch(`/admin/access/requests?${qs}`, { headers: authHeaders() })
  return parse(r)
}

export async function decideAccessRequest(id, action, { note = '', duration_days = null } = {}) {
  const r = await fetch(`/admin/access/requests/${id}/${action}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...authHeaders() },
    body: JSON.stringify({ note, duration_days }),
  })
  return parse(r)
}

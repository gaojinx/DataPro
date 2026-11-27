// 大结果集导出申请与审批（B13/F11）
import { authHeaders, sessionExpired } from './auth'
import { parseResponse, toApiError } from './errors'

async function parse(r) {
  return parseResponse(r)
}

export async function fetchExports({ all = 0, status = '' } = {}) {
  const qs = new URLSearchParams({ all: String(all) })
  if (status) qs.set('status', status)
  const r = await fetch(`/api/v1/exports?${qs}`, { headers: authHeaders() })
  return parse(r)
}

export async function createExport(payload) {
  const r = await fetch('/api/v1/exports', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...authHeaders() },
    body: JSON.stringify(payload),
  })
  return parse(r)
}

export async function approveExport(id, note = '') {
  const r = await fetch(`/api/v1/exports/${id}/approve`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...authHeaders() },
    body: JSON.stringify({ note }),
  })
  return parse(r)
}

export async function rejectExport(id, note = '') {
  const r = await fetch(`/api/v1/exports/${id}/reject`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...authHeaders() },
    body: JSON.stringify({ note }),
  })
  return parse(r)
}

export async function downloadExport(id) {
  const r = await fetch(`/api/v1/exports/${id}/download`, { headers: authHeaders() })
  if (r.status === 401) sessionExpired()
  if (!r.ok) {
    const data = await r.json().catch(() => ({}))
    throw toApiError(r, data)
  }
  return r.blob()
}

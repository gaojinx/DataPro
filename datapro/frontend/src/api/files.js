import { authHeaders } from './auth'

const BASE = '/api/v1/files'

async function parse(r) {
  const data = await r.json().catch(() => ({}))
  if (!r.ok) throw new Error(data.detail || `请求失败（${r.status}）`)
  return data
}

export async function uploadFile({ sessionId, file, origin = 'upload' }) {
  const fd = new FormData()
  fd.append('file', file)
  fd.append('session_id', sessionId || 'default')
  fd.append('origin', origin)
  return parse(await fetch(BASE, { method: 'POST', headers: authHeaders(), body: fd }))
}

export async function listFiles(sessionId) {
  const r = await fetch(`${BASE}?session_id=${encodeURIComponent(sessionId || 'default')}`,
                        { headers: authHeaders() })
  return (await parse(r)).files
}

export async function deleteFile(fileId) {
  return parse(await fetch(`${BASE}/${fileId}`, { method: 'DELETE', headers: authHeaders() }))
}

export const downloadUrl = (fileId) => `${BASE}/${fileId}/download`
export const inlineUrl = (fileId) => `${BASE}/${fileId}/download?inline=1`

export async function fetchPreview(fileId, limit = 200) {
  return parse(await fetch(`${BASE}/${fileId}/preview?limit=${limit}`, { headers: authHeaders() }))
}

async function lockCall(fileId, owner, action) {
  return parse(await fetch(`${BASE}/${fileId}/${action}`, {
    method: 'POST',
    headers: authHeaders({ 'Content-Type': 'application/json' }),
    body: JSON.stringify({ owner: owner || 'default' }),
  }))
}

export const lockFile = (fileId, owner) => lockCall(fileId, owner, 'lock')
export const unlockFile = (fileId, owner) => lockCall(fileId, owner, 'unlock')

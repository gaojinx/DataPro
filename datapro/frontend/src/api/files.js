import { authHeaders, getToken, sessionExpired } from './auth'
import { parseResponse } from './errors'

const BASE = '/api/v1/files'

async function parse(r) {
  return parseResponse(r)
}

export async function uploadFile({ sessionId, file, origin = 'upload', rootId = '' }) {
  const fd = new FormData()
  fd.append('file', file)
  fd.append('session_id', sessionId || 'default')
  fd.append('origin', origin)
  if (rootId) fd.append('root_id', rootId)
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

// 浏览器原生 <a>/<img>/<iframe> 无法携带 Authorization，下载与内联预览改用 ?token=
function withToken(url) {
  const token = getToken()
  return token ? `${url}${url.includes('?') ? '&' : '?'}token=${encodeURIComponent(token)}` : url
}

export const downloadUrl = (fileId) => withToken(`${BASE}/${fileId}/download`)
export const inlineUrl = (fileId) => withToken(`${BASE}/${fileId}/download?inline=1`)

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

export async function fetchFileVersions(fileId) {
  const r = await fetch(`${BASE}/${fileId}/versions`, { headers: authHeaders() })
  return (await parse(r)).versions
}

export async function restoreFileVersion(fileId) {
  const r = await fetch(`${BASE}/${fileId}/restore`, { method: 'POST', headers: authHeaders() })
  return parse(r)
}

export async function fetchFileDiff(fileId, against) {
  const r = await fetch(`${BASE}/${fileId}/diff?against=${encodeURIComponent(against)}`,
                        { headers: authHeaders() })
  return parse(r)
}

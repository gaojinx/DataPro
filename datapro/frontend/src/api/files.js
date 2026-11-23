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
  return parse(await fetch(BASE, { method: 'POST', body: fd }))
}

export async function listFiles(sessionId) {
  const r = await fetch(`${BASE}?session_id=${encodeURIComponent(sessionId || 'default')}`)
  return (await parse(r)).files
}

export async function deleteFile(fileId) {
  return parse(await fetch(`${BASE}/${fileId}`, { method: 'DELETE' }))
}

export const downloadUrl = (fileId) => `${BASE}/${fileId}/download`

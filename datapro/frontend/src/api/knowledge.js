// 知识库 API：结构化条目（术语/口径）+ 文档知识库（RAG）
import { authHeaders } from './auth'
import { parseResponse } from './errors'

async function parse(r) {
  return parseResponse(r)
}

export async function fetchKnowledge(q = '') {
  const r = await fetch(`/api/v1/knowledge${q ? '?q=' + encodeURIComponent(q) : ''}`, { headers: authHeaders() })
  return parse(r)
}

export async function saveKnowledge(item) {
  const isNew = !item.id
  const r = await fetch(`/api/v1/knowledge${isNew ? '' : '/' + item.id}`, {
    method: isNew ? 'POST' : 'PUT',
    headers: { 'Content-Type': 'application/json', ...authHeaders() },
    body: JSON.stringify(item),
  })
  return parse(r)
}

export async function deleteKnowledge(id) {
  const r = await fetch(`/api/v1/knowledge/${id}`, { method: 'DELETE', headers: authHeaders() })
  return parse(r)
}

/** 文档列表：id/title/chunk_count/char_count/embed_model/status */
export async function fetchDocs() {
  const r = await fetch('/api/v1/knowledge/docs', { headers: authHeaders() })
  return parse(r)
}

/** 上传文档文件（.md/.txt/.html）：走后端解析与切分，前端不解析正文 */
export async function uploadDoc(file, title = '', category = '通用') {
  const form = new FormData()
  form.append('file', file)
  const params = new URLSearchParams({ title, category })
  const r = await fetch(`/api/v1/knowledge/docs/upload?${params}`, {
    method: 'POST',
    headers: authHeaders(),      // 不要手写 Content-Type：交给浏览器带 boundary
    body: form,
  })
  return parse(r)
}

export async function deleteDoc(id) {
  const r = await fetch(`/api/v1/knowledge/docs/${id}`, { method: 'DELETE', headers: authHeaders() })
  return parse(r)
}

export async function reindexDoc(id) {
  const r = await fetch(`/api/v1/knowledge/docs/${id}/reindex`, { method: 'POST', headers: authHeaders() })
  return parse(r)
}

/** 检索预览：管理员调阈值时用真实问题看命中与分数 */
export async function searchDocs(q, topK = 5) {
  const r = await fetch(`/api/v1/knowledge/docs/search?q=${encodeURIComponent(q)}&top_k=${topK}`,
    { headers: authHeaders() })
  return parse(r)
}

export async function fetchDocStats() {
  const r = await fetch('/api/v1/knowledge/docs/stats', { headers: authHeaders() })
  return parse(r)
}

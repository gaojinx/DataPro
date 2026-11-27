// 标记码溯源 API：按 DP-xxxx 回查原始问题与结果
import { authHeaders } from './auth'
import { parseResponse } from './errors'

export async function fetchTrace(code) {
  const r = await fetch(`/api/v1/trace/${encodeURIComponent(code)}`, { headers: authHeaders() })
  return parseResponse(r)
}

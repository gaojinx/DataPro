// S-F05 最小用量页：query_record 计数，非计量计费账单
import { authHeaders } from './auth'
import { parseResponse } from './errors'

async function parse(r) {
  return parseResponse(r)
}

export async function fetchUsage(days = 30, all = 0) {
  // all=1 仅管理员生效（后端按角色判断），普通用户传了也只返回本人
  const r = await fetch(`/api/v1/usage/summary?days=${days}&all=${all}`, { headers: authHeaders() })
  return parse(r)
}

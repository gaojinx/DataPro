// 巡检（A08/F16）：结果、规则、手动触发（仅管理员）
import { authHeaders } from './auth'
import { parseResponse } from './errors'

async function parse(r) {
  return parseResponse(r)
}

export async function fetchInspections() {
  const r = await fetch('/api/v1/inspections', { headers: authHeaders() })
  return parse(r)
}

export async function fetchInspectionRules() {
  const r = await fetch('/api/v1/inspections/rules', { headers: authHeaders() })
  return parse(r)
}

export async function saveInspectionRules(rules) {
  const r = await fetch('/api/v1/inspections/rules', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json', ...authHeaders() },
    body: JSON.stringify({ rules }),
  })
  return parse(r)
}

export async function runInspection() {
  const r = await fetch('/api/v1/inspections/run', { method: 'POST', headers: authHeaders() })
  return parse(r)
}

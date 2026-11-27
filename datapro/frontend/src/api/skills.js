// 技能包 API
import { authHeaders } from './auth'
import { parseResponse } from './errors'

async function parse(r) {
  return parseResponse(r)
}

export async function fetchSkills(all = false) {
  const r = await fetch(`/api/v1/skills${all ? '?all=1' : ''}`, { headers: authHeaders() })
  return parse(r)
}

export async function saveSkill(skill) {
  const isNew = !skill.id
  const r = await fetch(`/api/v1/skills${isNew ? '' : '/' + skill.id}`, {
    method: isNew ? 'POST' : 'PUT',
    headers: { 'Content-Type': 'application/json', ...authHeaders() },
    body: JSON.stringify(skill),
  })
  return parse(r)
}

export async function deleteSkill(id) {
  const r = await fetch(`/api/v1/skills/${id}`, { method: 'DELETE', headers: authHeaders() })
  return parse(r)
}

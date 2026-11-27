// 自动学习（A11）：技能规范与长期记忆的查看/启停/删除
import { authHeaders } from './auth'
import { parseResponse } from './errors'

async function parse(r) {
  return parseResponse(r)
}

export const fetchLearnedSkills = (all = false) =>
  fetch(`/api/v1/learning/skills${all ? '?all=1' : ''}`, { headers: authHeaders() }).then(parse)

export const updateLearnedSkill = (id, enabled) =>
  fetch(`/api/v1/learning/skills/${id}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json', ...authHeaders() },
    body: JSON.stringify({ enabled }),
  }).then(parse)

export const deleteLearnedSkill = (id) =>
  fetch(`/api/v1/learning/skills/${id}`, { method: 'DELETE', headers: authHeaders() }).then(parse)

export const fetchMemories = (all = false) =>
  fetch(`/api/v1/learning/memories${all ? '?all=1' : ''}`, { headers: authHeaders() }).then(parse)

export const updateMemory = (id, enabled) =>
  fetch(`/api/v1/learning/memories/${id}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json', ...authHeaders() },
    body: JSON.stringify({ enabled }),
  }).then(parse)

export const deleteMemory = (id) =>
  fetch(`/api/v1/learning/memories/${id}`, { method: 'DELETE', headers: authHeaders() }).then(parse)

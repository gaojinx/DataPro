// 定时任务 API
import { authHeaders } from './auth'
import { parseResponse } from './errors'

async function parse(r) {
  return parseResponse(r)
}

export async function fetchSchedules() {
  const r = await fetch('/api/v1/schedules', { headers: authHeaders() })
  return parse(r)
}

export async function saveSchedule(task) {
  const isNew = !task.id
  const r = await fetch(`/api/v1/schedules${isNew ? '' : '/' + task.id}`, {
    method: isNew ? 'POST' : 'PUT',
    headers: { 'Content-Type': 'application/json', ...authHeaders() },
    body: JSON.stringify(task),
  })
  return parse(r)
}

export async function deleteSchedule(id) {
  const r = await fetch(`/api/v1/schedules/${id}`, { method: 'DELETE', headers: authHeaders() })
  return parse(r)
}

export async function runSchedule(id) {
  const r = await fetch(`/api/v1/schedules/${id}/run`, { method: 'POST', headers: authHeaders() })
  return parse(r)
}

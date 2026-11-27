import { beforeEach, describe, expect, it } from 'vitest'
import { setAuth } from './auth'
import { parseResponse, toApiError } from './errors'

function mockResp(status, body = {}, trace = '') {
  return {
    status,
    ok: status >= 200 && status < 300,
    headers: {
      get: (name) => (String(name).toLowerCase() === 'x-trace-id' ? (trace || null) : null),
    },
    json: async () => body,
  }
}

beforeEach(() => {
  localStorage.clear()
  setAuth('test-token', { username: 'tester' })   // 重置 sessionExpired 的 once 标记
})

describe('toApiError 错误映射', () => {
  it('优先后端 message，并附错误码与追踪号', () => {
    const e = toApiError(mockResp(403), {
      code: 'E003', message: '仅管理员可删除该技能', trace_id: 'tr-1',
    })
    expect(e.code).toBe('E003')
    expect(e.status).toBe(403)
    expect(e.traceId).toBe('tr-1')
    expect(e.message).toBe('仅管理员可删除该技能 [E003]（追踪号 tr-1）')
  })

  it('无 code 时按 HTTP 状态映射', () => {
    const e = toApiError(mockResp(404), {})
    expect(e.code).toBe('E002')
    expect(e.message).toBe('请求的资源不存在或已被移除 [E002]')
  })

  it('未知业务码回退到状态码映射', () => {
    const e = toApiError(mockResp(429), { code: 'E999' })
    expect(e.code).toBe('E007')
    expect(e.message).toContain('操作过于频繁')
  })

  it('英文通用 detail 不透出，换成映射人话，但 detail 仍保留供排障', () => {
    const e = toApiError(mockResp(500), { detail: 'Internal Server Error' })
    expect(e.code).toBe('E010')
    expect(e.message).toContain('服务开小差了')
    expect(e.message).not.toContain('Internal Server Error')
    expect(e.detail).toBe('Internal Server Error')
  })

  it('优先读取 x-trace-id 响应头', () => {
    const e = toApiError(mockResp(500, { trace_id: 'body-trace' }, 'header-trace'), {})
    expect(e.traceId).toBe('header-trace')
    expect(e.message).toContain('追踪号 header-trace')
  })

  it('401 静默（message 为空）并标记 auth，避免并发重复弹窗', () => {
    const e = toApiError(mockResp(401), { message: '登录已失效' })
    expect(e.auth).toBe(true)
    expect(e.code).toBe('E008')
    expect(e.message).toBe('')
  })

  it('非对象响应体不会抛错', () => {
    const e = toApiError(mockResp(400), 'oops')
    expect(e.code).toBe('E004')
    expect(e.message).toContain('[E004]')
  })
})

describe('parseResponse', () => {
  it('2xx 返回 JSON', async () => {
    await expect(parseResponse(mockResp(200, { ok: 1 }))).resolves.toEqual({ ok: 1 })
  })

  it('非 2xx 抛出可读错误', async () => {
    await expect(parseResponse(mockResp(403, { code: 'E003', message: '没有权限' })))
      .rejects.toThrow('没有权限 [E003]')
  })

  it('错误响应体不是合法 JSON 时仍按状态码映射', async () => {
    const resp = mockResp(502)
    resp.json = async () => { throw new SyntaxError('bad json') }
    await expect(parseResponse(resp)).rejects.toThrow('上游服务暂时不可用')
  })
})

import { describe, expect, it } from 'vitest'
import { formatClock, formatDateTime, pad2 } from './time'

describe('时间显示（X-C01）', () => {
  it('pad2 补零', () => {
    expect(pad2(0)).toBe('00')
    expect(pad2(7)).toBe('07')
    expect(pad2(23)).toBe('23')
  })

  it('formatClock 输出 HH:mm', () => {
    const ts = new Date(2026, 10, 26, 9, 5).getTime()
    expect(formatClock(ts)).toBe('09:05')
  })

  it('formatDateTime 含完整日期并注明审计口径', () => {
    const ts = new Date(2026, 10, 26, 14, 3).getTime()
    const text = formatDateTime(ts)
    expect(text).toContain('2026-11-26 14:03')
    expect(text).toContain('本地时间')
    expect(text).toContain('审计以服务端为准')
  })
})

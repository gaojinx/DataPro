import { describe, expect, it } from 'vitest'
import { asOfText, sourceGroups, trustBadges, emptyGuidance } from './trust'

const SOURCES = [
  { type: 'caliber', name: '支付订单数', detail: '去重支付单' },
  { type: 'table', name: 'dwd_order_detail_di' },
  { type: 'knowledge', name: '支付订单' },
  { type: 'doc', name: '口径手册' },
]
const RESULT = {
  row_count: 7, data_as_of: '2026-11-26', data_as_of_basis: 'dt 分区探针',
  time_window: '最近 7 天', masked: 2, sources: SOURCES,
}

describe('trust utils', () => {
  it('数据截止日：按天算新鲜度，缺失/非法值返回 null', () => {
    const now = new Date('2026-11-27T10:00:00')
    expect(asOfText(RESULT, now)).toMatchObject({ days: 1, freshness: '最新', basis: 'dt 分区探针' })
    expect(asOfText({ data_as_of: '2026-11-01' }, now).freshness).toBe('偏旧')
    expect(asOfText({}, now)).toBeNull()
    expect(asOfText({ data_as_of: 'bad' }, now)).toBeNull()
  })

  it('来源按 type 分组，不把 doc/knowledge 算进口径或表', () => {
    const g = sourceGroups(RESULT)
    expect(g.calibers).toHaveLength(1)
    expect(g.tables).toHaveLength(1)
    expect(g.knowledge).toHaveLength(1)
    expect(g.docs).toHaveLength(1)
  })

  it('可信徽标：缺失字段不渲染，不臆造数据', () => {
    const badges = trustBadges(RESULT)
    const keys = badges.map(b => b.key)
    expect(keys).toEqual(['asof', 'caliber', 'table', 'window', 'mask', 'rows'])
    expect(trustBadges({})).toEqual([])
    expect(trustBadges(null)).toEqual([])
    const fresh = badges.find(b => b.key === 'asof')
    expect(fresh.kind).toBe('good')
  })

  it('数据偏旧时徽标转 warn，脱敏为 0 时不显示', () => {
    const old = trustBadges({ ...RESULT, data_as_of: '2026-11-01', masked: 0 }, new Date('2026-11-27T10:00:00'))
    expect(old.find(b => b.key === 'asof').kind).toBe('warn')
    expect(old.some(b => b.key === 'mask')).toBe(false)
  })

  it('空结果引导：仅 0 行且真实 data_as_of 才给日期提示', () => {
    expect(emptyGuidance({ row_count: 1 })).toBeNull()
    expect(emptyGuidance(null)).toBeNull()
    const g = emptyGuidance({ ...RESULT, row_count: 0 })
    expect(g.title).toBe('这次没有查到数据')
    expect(g.tips.some(t => t.includes('2026-11-26'))).toBe(true)
    expect(g.tips.some(t => t.includes('最近 7 天'))).toBe(true)
    expect(g.canAskBoundary).toBe(true)
  })

  it('空结果缺字段时不臆造具体日期或时间窗', () => {
    const g = emptyGuidance({ row_count: 0 })
    expect(g.tips.every(t => !t.includes('最新到'))).toBe(true)
    expect(g.tips.every(t => !t.includes('时间窗为'))).toBe(true)
  })
})

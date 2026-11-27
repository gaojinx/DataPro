import { describe, expect, it } from 'vitest'
import { buildReportHtml } from './report'

const MSG = {
  asked: '最近 7 天支付订单数',
  text: '最近 7 天共 602 单。',
  result: {
    row_count: 2, columns: ['渠道', '订单数', '订单号'],
    rows: [
      { 渠道: '自然流量', 订单数: 1000, 订单号: '202611220001' },
      { 渠道: '搜索广告', 订单数: null, 订单号: '202611220002' },
    ],
    numeric_columns: ['订单数'],
    sql: 'SELECT channel, COUNT(*) FROM dwd_order WHERE dt >= ...',
    explanation: '按渠道聚合支付订单',
    data_as_of: '2026-11-26', time_window: '最近 7 天', masked: 1,
    sources: [{ type: 'caliber', name: '支付订单数', detail: '去重支付单' }],
  },
}

describe('buildReportHtml', () => {
  it('自包含 HTML：含问题、摘要、SQL、口径、表格与徽标', () => {
    const html = buildReportHtml(MSG, { traceCode: 'DP-0001' })
    expect(html).toContain('<!doctype html>')
    expect(html).toContain('最近 7 天支付订单数')
    expect(html).toContain('DP-0001')
    expect(html).toContain('口径依据')
    expect(html).toContain('去重支付单')
    expect(html).toContain('查询 SQL')
    expect(html).toContain('2026-11-26')
    expect(html).toContain('@media print')
  })

  it('数值列千分位、null 占位；编码列原样，不改语义', () => {
    const html = buildReportHtml(MSG)
    expect(html).toContain('1,000')
    expect(html).toContain('<td>—</td>')
    expect(html).toContain('202611220001')   // 订单号不被千分位
    expect(html).not.toContain('2,026,112,200,001')
  })

  it('转义 HTML 特殊字符，避免报告注入', () => {
    const html = buildReportHtml({ ...MSG, asked: '<img src=x onerror=alert(1)>' })
    expect(html).not.toContain('<img src=x')
    expect(html).toContain('&lt;img')
  })

  it('缺字段时优雅降级，不渲染空块', () => {
    const html = buildReportHtml({ result: {} })
    expect(html).not.toContain('口径依据')
    expect(html).not.toContain('查询 SQL')
    expect(html).toContain('（无提问）')
  })
})

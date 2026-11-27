import { describe, expect, it } from 'vitest'
import { createSSRApp, h } from 'vue'
import { renderToString } from 'vue/server-renderer'
import ChartView from './ChartView.vue'

/** 用 Vue 自带 SSR 渲染组件，node 环境即可验证结构与数值，不需要 jsdom。 */
function render(props) {
  return renderToString(createSSRApp({ render: () => h(ChartView, props) }))
}

const rows = [
  { region: '华东', amount: 1200 },
  { region: '华北', amount: 300 },
  { region: '华南', amount: 900 },
]

describe('ChartView 条形图', () => {
  it('按行渲染标签与格式化数值，不改变原始数值', async () => {
    const html = await render({ columns: ['region', 'amount'], rows, mode: 'bar' })
    expect(html).toContain('chart-bar')
    expect(html).toContain('华东')
    expect(html).toContain('1,200')
    expect(html).toContain('300')
  })

  it('零值不会被当成空数据吞掉', async () => {
    const html = await render({
      columns: ['region', 'amount'],
      rows: [{ region: '华东', amount: 0 }, { region: '华北', amount: 10 }],
      mode: 'bar',
    })
    expect(html).toContain('华东')
    expect(html).toContain('>0<')
  })
})

describe('ChartView 折线图', () => {
  it('多指标各渲染一张小多图，并给出可读 aria-label', async () => {
    const multi = [
      { dt: '11-20', orders: 10, users: 4 },
      { dt: '11-21', orders: 12, users: 6 },
      { dt: '11-22', orders: 9, users: 5 },
    ]
    const html = await render({ columns: ['dt', 'orders', 'users'], rows: multi, mode: 'line' })
    expect((html.match(/class="cv-spark"/g) || []).length).toBe(2)
    expect(html).toContain('orders 趋势')
    expect(html).toContain('users 趋势')
    expect(html).toContain('<polyline')
  })

  it('单行/无可用数值列时给出降级提示', async () => {
    const html = await render({
      columns: ['dt', 'orders'], rows: [{ dt: '11-20', orders: 10 }], mode: 'line',
    })
    expect(html).toContain('当前数据不适合该图表')
  })
})

describe('ChartView 环形图', () => {
  it('只用正值计算占比，合计与图例可见', async () => {
    const html = await render({ columns: ['region', 'amount'], rows, mode: 'donut' })
    expect(html).toContain('cv-donut-seg')
    expect(html).toContain('合计')
    expect(html).toContain('2,400')
    expect(html).toContain('50%')       // 1200 / 2400
  })

  it('全为负值时退回空态，不画误导性占比', async () => {
    const html = await render({
      columns: ['region', 'amount'],
      rows: [{ region: '华东', amount: -1 }, { region: '华北', amount: -2 }],
      mode: 'donut',
    })
    expect(html).toContain('当前数据不适合该图表')
    expect(html).not.toContain('cv-donut-seg')
  })
})

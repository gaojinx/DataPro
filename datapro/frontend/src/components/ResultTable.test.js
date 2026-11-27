// @vitest-environment happy-dom
import { describe, expect, it } from 'vitest'
import { mount } from '@vue/test-utils'
import { defineComponent, h, ref } from 'vue'
import ResultTable from './ResultTable.vue'

// el-icon 由主应用全局注册；单测里用轻量 stub，避免 Element Plus 全量挂载
const IconStub = defineComponent({
  name: 'ElIcon',
  setup(_, { slots }) { return () => h('span', { class: 'el-icon' }, slots.default?.()) },
})

const BASE_ROWS = [
  { region: '华东', amount: '100' },
  { region: '华北', amount: '9' },
  { region: '华南', amount: '20' },
]

/** 用宿主组件承接 v-model:sort / v-model:page，模拟 App.vue 的真实受控用法。 */
function mountTable(overrides = {}) {
  const rows = overrides.rows ?? BASE_ROWS
  const Host = defineComponent({
    setup() {
      const sort = ref(overrides.sort ?? null)
      const page = ref(overrides.page ?? 1)
      return () => h(ResultTable, {
        columns: overrides.columns ?? ['region', 'amount'],
        rows,
        numericColumns: overrides.numericColumns ?? ['amount'],
        truncated: overrides.truncated ?? false,
        rowCount: overrides.rowCount ?? rows.length,
        pageSize: overrides.pageSize ?? 50,
        sort: sort.value,
        page: page.value,
        'onUpdate:sort': (v) => { sort.value = v },
        'onUpdate:page': (v) => { page.value = v },
      })
    },
  })
  return mount(Host, { global: { stubs: { 'el-icon': IconStub, ElIcon: IconStub } } })
}

const bodyCells = (w) => w.findAll('tbody tr').map(tr => tr.findAll('td').map(td => td.text()))
const header = (w, i) => w.findAll('th.sortable')[i]

describe('ResultTable · 渲染', () => {
  it('渲染行号、列值与数值格式化', () => {
    const w = mountTable({
      rows: [
        { region: '华东', amount: 1200 },
        { region: '华北', amount: 9 },
      ],
    })
    expect(bodyCells(w)).toEqual([
      ['1', '华东', '1,200'],
      ['2', '华北', '9'],
    ])
    expect(w.find('th.rownum-th').text()).toBe('#')
  })

  it('null 显示 —，0 原样显示', () => {
    const w = mountTable({
      rows: [
        { region: '华东', amount: null },
        { region: '华北', amount: 0 },
      ],
    })
    expect(bodyCells(w).map(r => r[2])).toEqual(['—', '0'])
  })

  it('小结果不显示分页器；truncated 时显示上限提示', () => {
    expect(mountTable().find('.table-pager').exists()).toBe(false)

    const w = mountTable({ truncated: true, rowCount: 200, rows: BASE_ROWS })
    expect(w.find('.pager-note').text()).toContain('仅前 200 行')
  })
})

describe('ResultTable · 表头排序', () => {
  it('三态循环：升 → 降 → 原序，且 aria-sort 同步', async () => {
    const w = mountTable()
    const th = () => header(w, 1)

    expect(th().attributes('aria-sort')).toBe('none')

    await th().trigger('click')
    expect(bodyCells(w).map(r => r[1])).toEqual(['华北', '华南', '华东'])
    expect(th().attributes('aria-sort')).toBe('ascending')

    await th().trigger('click')
    expect(bodyCells(w).map(r => r[1])).toEqual(['华东', '华南', '华北'])
    expect(th().attributes('aria-sort')).toBe('descending')

    await th().trigger('click')
    expect(bodyCells(w).map(r => r[1])).toEqual(['华东', '华北', '华南'])
    expect(th().attributes('aria-sort')).toBe('none')
  })

  it('排序不修改传入的原数组', async () => {
    const rows = [
      { region: '华东', amount: '100' },
      { region: '华北', amount: '9' },
    ]
    const w = mountTable({ rows })
    await header(w, 1).trigger('click')
    expect(rows.map(r => r.region)).toEqual(['华东', '华北'])
  })

  it('排序后回到第 1 页', async () => {
    const many = Array.from({ length: 5 }, (_, i) => ({ region: `R${i + 1}`, amount: i + 1 }))
    const w = mountTable({ rows: many, pageSize: 2 })
    await w.find('[aria-label="下一页"]').trigger('click')
    expect(w.find('.pager-info').text()).toBe('2 / 3')

    await header(w, 1).trigger('click')
    expect(w.find('.pager-info').text()).toBe('1 / 3')
    expect(bodyCells(w).map(r => r[1])).toEqual(['R1', 'R2'])
  })
})

describe('ResultTable · 分页', () => {
  const many = Array.from({ length: 5 }, (_, i) => ({ region: `R${i + 1}`, amount: i + 1 }))

  it('按 pageSize 切片、行号跨页连续、首尾页按钮禁用', async () => {
    const w = mountTable({ rows: many, pageSize: 2 })
    expect(w.find('.pager-info').text()).toBe('1 / 3')
    expect(bodyCells(w)).toEqual([['1', 'R1', '1'], ['2', 'R2', '2']])
    expect(w.find('[aria-label="上一页"]').attributes('disabled')).toBeDefined()

    await w.find('[aria-label="下一页"]').trigger('click')
    expect(w.find('.pager-info').text()).toBe('2 / 3')
    expect(bodyCells(w)).toEqual([['3', 'R3', '3'], ['4', 'R4', '4']])

    await w.find('[aria-label="下一页"]').trigger('click')
    expect(w.find('.pager-info').text()).toBe('3 / 3')
    expect(bodyCells(w)).toEqual([['5', 'R5', '5']])
    expect(w.find('[aria-label="下一页"]').attributes('disabled')).toBeDefined()
  })

  it('页码越界时夹到合法范围', async () => {
    const w = mountTable({ rows: many, pageSize: 2, page: 99 })
    expect(w.find('.pager-info').text()).toBe('3 / 3')
    expect(bodyCells(w)).toEqual([['5', 'R5', '5']])
  })
})

describe('ResultTable · 复制', () => {
  it('点击复制按钮只发 copy 事件，由父层取原始行', async () => {
    const w = mountTable()
    await w.find('[aria-label="复制整表"]').trigger('click')
    const inner = w.findComponent(ResultTable)
    expect(inner.emitted('copy')).toHaveLength(1)
  })
})

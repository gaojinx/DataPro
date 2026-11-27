import { describe, expect, it } from 'vitest'
import {
  PAGE_SIZE, buildTsv, displayCell, displayIndex, pageCount, pageSlice,
  sortRows, toNumber, tsvCell,
} from './table'

describe('tsvCell / buildTsv', () => {
  it('普通值原样输出', () => {
    expect(tsvCell('华东')).toBe('华东')
    expect(tsvCell(12)).toBe('12')
  })

  it('null/undefined 输出空串，0 仍输出 0', () => {
    expect(tsvCell(null)).toBe('')
    expect(tsvCell(undefined)).toBe('')
    expect(tsvCell(0)).toBe('0')
  })

  it('含制表符/换行/引号时整体加引号并翻倍内部引号', () => {
    expect(tsvCell('a\tb')).toBe('"a\tb"')
    expect(tsvCell('a\nb')).toBe('"a\nb"')
    expect(tsvCell('他说"好"')).toBe('"他说""好"""')
  })

  it('按列顺序生成表头与数据行', () => {
    const tsv = buildTsv(['name', 'amount'], [
      { name: '华东', amount: 1 },
      { name: '华北\t区', amount: 2 },
    ])
    expect(tsv).toBe('name\tamount\n华东\t1\n"华北\t区"\t2')
  })

  it('空列表/空列返回空串', () => {
    expect(buildTsv([], [])).toBe('')
    expect(buildTsv(['a'], [])).toBe('')
    expect(buildTsv(undefined, undefined)).toBe('')
  })

  it('生成 TSV 不修改原始行', () => {
    const rows = [{ a: 1, b: 'x' }]
    buildTsv(['a', 'b'], rows)
    expect(rows).toEqual([{ a: 1, b: 'x' }])
  })
})

describe('displayCell（空值与数值格式化）', () => {
  it('null/undefined 显示占位符，0 原样显示', () => {
    expect(displayCell(null, 'amount')).toBe('—')
    expect(displayCell(undefined, 'amount')).toBe('—')
    expect(displayCell(0, 'amount')).toBe('0')
    expect(displayCell('0', 'amount', ['amount'])).toBe('0')
  })

  it('白名单内的字符串金额做千分位（DECIMAL 序列化场景）', () => {
    expect(displayCell('72470.55', 'pay_amount', ['pay_amount'])).toBe('72,470.55')
    expect(displayCell('1234567.891', 'pay_amount', ['pay_amount'])).toBe('1,234,567.89')
  })

  it('订单号等编码字符串不在白名单时保持原样', () => {
    expect(displayCell('202611220001', 'order_no', ['pay_amount'])).toBe('202611220001')
    expect(displayCell('202611220001', 'order_no')).toBe('202611220001')
  })

  it('真正的 JS number 即使无白名单也格式化', () => {
    expect(displayCell(1234567.891, 'cnt')).toBe('1,234,567.89')
  })

  it('非数值字符串原样返回', () => {
    expect(displayCell('华东', 'region', ['region'])).toBe('华东')
  })
})

describe('toNumber', () => {
  it('解析 number 与纯数字字符串', () => {
    expect(toNumber(3)).toBe(3)
    expect(toNumber(' 3.5 ')).toBe(3.5)
    expect(toNumber('-2')).toBe(-2)
  })

  it('纯数字字符串按数值解析（显示层仍靠白名单保护编码列）', () => {
    expect(toNumber('202611220001')).toBe(202611220001)
  })

  it('含字母的编码/空值/Infinity 返回 null', () => {
    expect(toNumber('ORDER-1')).toBeNull()
    expect(toNumber(null)).toBeNull()
    expect(toNumber(Infinity)).toBeNull()
  })
})

describe('sortRows（视图排序，不改原数组）', () => {
  const columns = ['region', 'amount']
  const rows = [
    { region: '华东', amount: '100' },
    { region: '华北', amount: '9' },
    { region: '华南', amount: '20' },
  ]

  it('数值列按数值升序，避免 "100" < "9"', () => {
    const out = sortRows(rows, columns, { col: 'amount', dir: 'asc' })
    expect(out.map(r => r.amount)).toEqual(['9', '20', '100'])
  })

  it('降序为升序反转', () => {
    const out = sortRows(rows, columns, { col: 'amount', dir: 'desc' })
    expect(out.map(r => r.amount)).toEqual(['100', '20', '9'])
  })

  it('不修改原数组与原对象顺序', () => {
    const before = rows.map(r => r.amount)
    sortRows(rows, columns, { col: 'amount', dir: 'desc' })
    expect(rows.map(r => r.amount)).toEqual(before)
  })

  it('无排序或非法列返回原数组引用', () => {
    expect(sortRows(rows, columns, null)).toBe(rows)
    expect(sortRows(rows, columns, { col: 'nope', dir: 'asc' })).toBe(rows)
  })

  it('空值与中文列排序不抛错', () => {
    const data = [{ name: 'b' }, { name: null }, { name: 'a' }]
    const out = sortRows(data, ['name'], { col: 'name', dir: 'asc' })
    expect(out.map(r => r.name)).toEqual([null, 'a', 'b'])
  })
})

describe('分页', () => {
  const rows = Array.from({ length: 200 }, (_, i) => ({ i }))

  it('200 行按每页 50 分为 4 页，0 行仍为 1 页', () => {
    expect(PAGE_SIZE).toBe(50)
    expect(pageCount(200)).toBe(4)
    expect(pageCount(0)).toBe(1)
    expect(pageCount(51)).toBe(2)
  })

  it('pageSlice 取对应页并在越界时夹取', () => {
    expect(pageSlice(rows, 1)[0].i).toBe(0)
    expect(pageSlice(rows, 2)[0].i).toBe(50)
    expect(pageSlice(rows, 4).length).toBe(50)
    expect(pageSlice(rows, 99)[0].i).toBe(150)
    expect(pageSlice(rows, 0)[0].i).toBe(0)
  })

  it('显示序号跨页连续', () => {
    expect(displayIndex(1, 0)).toBe(1)
    expect(displayIndex(1, 49)).toBe(50)
    expect(displayIndex(2, 0)).toBe(51)
    expect(displayIndex(3, 7)).toBe(108)
  })
})

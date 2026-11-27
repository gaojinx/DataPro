/**
 * 结果表格的纯函数工具：TSV 复制 / 单元格显示 / 排序 / 分页。
 *
 * 数据语义红线（AGENTS.md §4.2-11）：
 * - 只把 null/undefined 显示为「—」，0 必须原样显示；
 * - 千分位只作用于后端 numeric_columns 白名单或真正的 JS number，编码字符串保持原样；
 * - 排序/分页只生成视图，绝不原地修改 result.rows（导出与复制依赖原始行序）。
 */

export const PAGE_SIZE = 50

/** TSV 单元格转义：含制表符/换行/引号时整体加引号，并把内部引号翻倍。 */
export function tsvCell(v) {
  const s = v == null ? '' : String(v)
  return /[\t\n\r"]/.test(s) ? `"${s.replaceAll('"', '""')}"` : s
}

/** 按 columns 顺序生成整表 TSV（含表头）；空表返回空串。 */
export function buildTsv(columns, rows) {
  if (!Array.isArray(columns) || !columns.length || !Array.isArray(rows) || !rows.length) return ''
  return [
    columns.map(tsvCell).join('\t'),
    ...rows.map(r => columns.map(c => tsvCell(r?.[c])).join('\t')),
  ].join('\n')
}

const NUM_RE = /^-?\d+(\.\d+)?$/

/** 宽松数值解析：number 与纯数字字符串；其它（含订单号）返回 null。 */
export function toNumber(v) {
  if (typeof v === 'number') return Number.isFinite(v) ? v : null
  if (typeof v === 'string' && NUM_RE.test(v.trim())) return Number(v)
  return null
}

/**
 * 单元格显示值（display-only）：
 * - null/undefined → '—'；0 → '0'；
 * - numeric_columns 白名单列（DECIMAL 序列化后是字符串）或 JS number → 千分位；
 * - 其它字符串原样返回，避免把订单号格式化坏。
 */
export function displayCell(value, column, numericColumns) {
  if (value == null) return '—'
  const whitelisted = Array.isArray(numericColumns) && numericColumns.includes(column)
  if (whitelisted || typeof value === 'number') {
    const n = Number(value)
    if (Number.isFinite(n)) return n.toLocaleString('zh-CN', { maximumFractionDigits: 2 })
  }
  return String(value)
}

/**
 * 排序视图：返回新数组，原 rows 不变。
 * 数值列按数值比较（9 < 100），非数值列按中文本地化比较。
 */
export function sortRows(rows, columns, sort) {
  const source = Array.isArray(rows) ? rows : []
  if (!sort?.col || !Array.isArray(columns) || !columns.includes(sort.col)) return source
  const cmp = (a, b) => {
    const av = toNumber(a?.[sort.col])
    const bv = toNumber(b?.[sort.col])
    if (av !== null && bv !== null) return av - bv
    const as = a?.[sort.col] == null ? '' : String(a[sort.col])
    const bs = b?.[sort.col] == null ? '' : String(b[sort.col])
    return as.localeCompare(bs, 'zh-CN')
  }
  const out = [...source].sort(cmp)
  return sort.dir === 'desc' ? out.reverse() : out
}

export function pageCount(total, pageSize = PAGE_SIZE) {
  const size = pageSize > 0 ? pageSize : PAGE_SIZE
  return Math.max(1, Math.ceil((total || 0) / size))
}

/** 取某页数据；页码越界自动夹到 [1, 总页数]。 */
export function pageSlice(rows, page, pageSize = PAGE_SIZE) {
  const list = Array.isArray(rows) ? rows : []
  const totalPages = pageCount(list.length, pageSize)
  const current = Math.min(Math.max(1, page || 1), totalPages)
  const size = pageSize > 0 ? pageSize : PAGE_SIZE
  return list.slice((current - 1) * size, current * size)
}

/** 显示序号：跨页连续，1 起。 */
export function displayIndex(page, indexInPage, pageSize = PAGE_SIZE) {
  const size = pageSize > 0 ? pageSize : PAGE_SIZE
  return ((page || 1) - 1) * size + indexInPage + 1
}

/**
 * 口径可信信息提取（纯函数）
 * 只读取 result 事件已有字段，产出供「可信卡 / 分享报告 / 空结果引导」使用的视图模型。
 * 红线：只做展示层归纳，不修改任何数据、不臆造缺失信息。
 */

// 数据截止日的人类可读文案：只有后端真实探针下发 data_as_of 才展示
export function asOfText(result, now = new Date()) {
  const d = result?.data_as_of
  if (!d) return null
  const date = new Date(d)
  if (Number.isNaN(date.getTime())) return null
  const days = Math.floor((now - date) / 86400000)
  const freshness = days <= 1 ? '最新' : days <= 3 ? '较新' : '偏旧'
  return { date: d, days, freshness, basis: result?.data_as_of_basis || null }
}

// 从 sources 里按 type 归类，只统计真实返回的条目
export function sourceGroups(result) {
  const sources = Array.isArray(result?.sources) ? result.sources : []
  return {
    calibers: sources.filter(s => s?.type === 'caliber'),
    tables: sources.filter(s => s?.type === 'table'),
    knowledge: sources.filter(s => s?.type === 'knowledge'),
    docs: sources.filter(s => s?.type === 'doc'),
  }
}

// 可信卡视图模型：各字段缺失时返回 null，由组件决定不渲染该项
export function trustBadges(result) {
  if (!result) return []
  const badges = []
  const asOf = asOfText(result)
  if (asOf) badges.push({ key: 'asof', label: asOf.date, tip: `数据截止日 · ${asOf.freshness}${asOf.basis ? ` · ${asOf.basis}` : ''}`, kind: asOf.days <= 3 ? 'good' : 'warn' })
  const g = sourceGroups(result)
  if (g.calibers.length) badges.push({ key: 'caliber', label: `口径 ${g.calibers.length}`, tip: g.calibers.map(c => c.name).join('、'), kind: 'info' })
  if (g.tables.length) badges.push({ key: 'table', label: `来源 ${g.tables.length}`, tip: g.tables.map(t => t.name).join('、'), kind: 'info' })
  if (result.time_window) badges.push({ key: 'window', label: result.time_window, tip: '本次查询的时间范围', kind: 'neutral' })
  const masked = Number(result.masked) || 0
  if (masked > 0) badges.push({ key: 'mask', label: '已脱敏', tip: `${masked} 个字段已打码，数值不受影响`, kind: 'good' })
  const n = Number(result.row_count) || 0
  if (n > 0) badges.push({ key: 'rows', label: `${n} 行`, tip: result.truncated ? '对话内仅展示前 200 行，完整请导出' : '本轮返回行数', kind: 'neutral' })
  return badges
}

// 空结果引导：只在真正返回 0 行且有结果事件时给出，不臆造原因
export function emptyGuidance(result) {
  if (!result || (Number(result.row_count) || 0) !== 0) return null
  const tips = []
  const asOf = asOfText(result)
  if (asOf) tips.push(`数据最新到 ${asOf.date}，若问题里指定了更晚的日期可能查不到`)
  if (result.time_window) tips.push(`本次时间窗为「${result.time_window}」，可尝试放宽时间范围`)
  if (gHasCaliber(result)) tips.push('可换一种口径或直接问「数据到几号」确认覆盖范围')
  return { title: '这次没有查到数据', tips, canAskBoundary: true }
}

function gHasCaliber(result) {
  return sourceGroups(result).calibers.length > 0
}

/**
 * 分享报告生成（纯函数）：把问题 + 可信信息 + 结果表 + SQL 组装成自包含 HTML。
 * 纯前端、零新依赖：新窗口打开即可「打印 / 另存 PDF / 全选复制」。
 * 红线：报告内容全部来自已脱敏的 msg.result.rows，不新增暴露面、不改语义。
 */
import { trustBadges, sourceGroups } from './trust'

function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]))
}

// 单元格显示：与结果表一致——null 占位、0 保留、数值千分位仅白名单列
function display(v, numericCols, col) {
  if (v == null) return '—'
  if ((Array.isArray(numericCols) && numericCols.includes(col)) || typeof v === 'number') {
    const n = Number(v)
    if (Number.isFinite(n)) return n.toLocaleString('zh-CN', { maximumFractionDigits: 2 })
  }
  return String(v)
}

export function buildReportHtml(msg, meta = {}) {
  const r = msg?.result || {}
  const cols = Array.isArray(r.columns) ? r.columns : []
  const rows = Array.isArray(r.rows) ? r.rows : []
  const badges = trustBadges(r)
  const g = sourceGroups(r)

  const badgeHtml = badges.map(b => `<span class="badge ${esc(b.kind)}">${esc(b.label)}</span>`).join('')
  const caliberHtml = g.calibers.length
    ? `<div class="block"><h3>口径依据</h3><ul>${g.calibers.map(c => `<li><b>${esc(c.name)}</b>${c.detail ? `：${esc(c.detail)}` : ''}</li>`).join('')}</ul></div>`
    : ''
  const tableHtml = cols.length
    ? `<div class="block"><h3>结果数据<span class="muted">（${rows.length} 行${r.truncated ? '，仅前 200 行' : ''}）</span></h3>
       <div class="tw"><table><thead><tr>${cols.map(c => `<th>${esc(c)}</th>`).join('')}</tr></thead>
       <tbody>${rows.map(row => `<tr>${cols.map(c => `<td>${esc(display(row[c], r.numeric_columns, c))}</td>`).join('')}</tr>`).join('')}</tbody></table></div></div>`
    : ''
  const sqlHtml = r.sql ? `<div class="block"><h3>查询 SQL</h3><pre>${esc(r.sql)}</pre></div>` : ''
  const explainHtml = r.explanation ? `<p class="explain">${esc(r.explanation)}</p>` : ''
  const summaryHtml = msg.text ? `<p class="summary">${esc(msg.text)}</p>` : ''
  const question = esc(msg.asked || '')
  const code = esc(meta.traceCode || r.trace_code || '')
  const generated = esc(new Date().toLocaleString('zh-CN'))
  const brand = esc(meta.branding?.product_name || 'DataPro')

  return `<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<title>${brand} · 取数报告</title>
<style>
*{box-sizing:border-box}body{font-family:-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;color:#12140f;background:#faf9f6;max-width:900px;margin:0 auto;padding:32px 28px;line-height:1.6}
.head{border-bottom:2px solid #2f5bd8;padding-bottom:14px;margin-bottom:20px}
.brand{color:#2f5bd8;font-weight:700;font-size:14px;letter-spacing:.5px}
h1{font-size:22px;margin:8px 0 4px}h2{font-size:15px;margin:0}.meta{color:#6f7468;font-size:12px}
.badges{display:flex;flex-wrap:wrap;gap:6px;margin:14px 0}
.badge{font-size:12px;padding:3px 9px;border-radius:999px;background:#ebeffb;color:#2449b4;border:1px solid #ccd7f4}
.badge.good{background:#eaf3de;color:#3b6d11;border-color:#c0dd97}
.badge.warn{background:#fdf7e8;color:#77591b;border-color:#ead9ad}
.block{margin:20px 0}h3{font-size:14px;margin:0 0 8px}.muted{color:#9aa094;font-weight:400;font-size:12px}
.summary{background:#fff;border:1px solid #e6e3da;border-radius:10px;padding:12px 14px;margin:14px 0}
.explain{color:#3d4138;font-size:13px}
.tw{overflow:auto;border:1px solid #e6e3da;border-radius:10px;background:#fff}
table{border-collapse:collapse;width:100%;font-size:12px}th,td{padding:7px 10px;text-align:left;border-bottom:1px solid #f0eee8;white-space:nowrap}
th{background:#f5f3ed;position:sticky;top:0}tr:last-child td{border-bottom:none}
pre{background:#f4f2ec;padding:12px;border-radius:10px;font-size:12px;overflow:auto;white-space:pre-wrap}
ul{margin:0;padding-left:20px;font-size:13px}li{margin:3px 0}
.foot{margin-top:28px;padding-top:12px;border-top:1px solid #e6e3da;color:#9aa094;font-size:11px}
@media print{body{background:#fff;padding:0}.tw{max-height:none}}
</style></head><body>
<div class="head"><div class="brand">${brand} · 取数报告</div><h1>${question || '（无提问）'}</h1>
<div class="meta">生成于 ${generated}${code ? ` · 溯源 ${code}` : ''}</div></div>
<div class="badges">${badgeHtml}</div>
${summaryHtml}${explainHtml}${caliberHtml}${tableHtml}${sqlHtml}
<div class="foot">本报告由 ${brand} 自动生成，数据已经只读查询与脱敏处理；口径与来源以上述徽标及依据为准。</div>
</body></html>`
}

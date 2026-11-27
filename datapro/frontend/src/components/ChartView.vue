<script setup>
import { computed } from 'vue'

// U-F04：零第三方依赖的轻量 SVG 图表。只做可视化，不改变传入数值。
const props = defineProps({
  columns: { type: Array, default: () => [] },
  rows: { type: Array, default: () => [] },
  mode: { type: String, default: 'bar' }, // bar | line | donut
})

const NUM_RE = /^-?\d+(\.\d+)?$/
function toNum(v) {
  if (typeof v === 'number') return Number.isFinite(v) ? v : null
  if (typeof v === 'string' && NUM_RE.test(v.trim())) return Number(v)
  return null
}
function fmt(v) {
  const n = Number(v)
  if (!Number.isFinite(n)) return String(v ?? '—')
  return n.toLocaleString('zh-CN', { maximumFractionDigits: 2 })
}

const SERIES_COLORS = [
  'var(--accent)', 'var(--accent-hover)', 'var(--muted)', 'var(--faint)',
]

const numericCols = computed(() => (props.columns || []).filter(c => {
  const vals = (props.rows || []).map(r => toNum(r[c]))
  return vals.length > 0 && vals.every(v => v !== null) && new Set(vals).size > 1
}))
const valueCol = computed(() => numericCols.value[numericCols.value.length - 1])
const labelCol = computed(() => {
  if (!valueCol.value) return (props.columns || [])[0]
  return (props.columns || []).find(c => c !== valueCol.value
    && (props.rows || []).every(r => toNum(r[c]) === null))
    || (props.columns || []).find(c => c !== valueCol.value)
})
const items = computed(() => (props.rows || []).slice(0, 20).map(r => ({
  label: String(r[labelCol.value] ?? '—'),
  value: toNum(r[valueCol.value]) ?? 0,
})))
const maxAbs = computed(() => Math.max(0, ...items.value.map(i => Math.abs(i.value))))

// 条形图沿用文档基线里的紧凑横条，不动数值
const bars = computed(() => items.value.map(i => ({
  ...i,
  pct: maxAbs.value ? Math.max(2, Math.round(Math.abs(i.value) / maxAbs.value * 100)) : 0,
})))

// 折线图用「小多图」：每个指标独立纵轴，避免量级差异把次要指标压成贴底直线。
// 点、端点标注的都是原始数值，不对用户可见数字做归一化。
const series = computed(() => numericCols.value.slice(0, 4).map((key, i) => {
  const values = (props.rows || []).slice(0, 20).map(r => toNum(r[key]) ?? 0)
  const min = Math.min(...values)
  const max = Math.max(...values)
  const range = max - min || 1
  const sx = idx => 8 + idx * 604 / Math.max(values.length - 1, 1)
  const sy = v => 44 - (v - min) / range * 32
  return {
    key, color: SERIES_COLORS[i], values, min, max,
    sx, sy,
    points: values.map((v, idx) => `${sx(idx)},${sy(v)}`).join(' '),
  }
}))

// 环形图只取正值占比，避免负值把结构图变成误导性图形
const donutItems = computed(() => items.value
  .filter(i => i.value > 0)
  .sort((a, b) => b.value - a.value)
  .slice(0, 8))
const donutTotal = computed(() => donutItems.value.reduce((s, i) => s + i.value, 0))
const C = 2 * Math.PI * 68
const donutSegments = computed(() => {
  let acc = 0
  return donutItems.value.map((it, i) => {
    const len = donutTotal.value ? it.value / donutTotal.value * C : 0
    const seg = { ...it, len, offset: acc, color: SERIES_COLORS[i % SERIES_COLORS.length],
      pct: donutTotal.value ? Math.round(it.value / donutTotal.value * 100) : 0 }
    acc += len
    return seg
  })
})
</script>

<template>
  <div class="chart-view">
    <!-- 表格之外的默认视图：与既有条形图视觉一致 -->
    <div v-if="mode === 'bar'" class="chart">
      <div v-for="(it, k) in bars" :key="k" class="chart-row">
        <span class="chart-label" :title="it.label">{{ it.label }}</span>
        <div class="chart-bar-wrap">
          <div class="chart-bar" :style="{ width: it.pct + '%' }"></div>
        </div>
        <span class="chart-value">{{ fmt(it.value) }}</span>
      </div>
    </div>

    <!-- 折线（趋势 / 多指标）：每个指标一条独立纵轴的迷你折线，避免量级差把小指标压平 -->
    <div v-else-if="mode === 'line' && series.length" class="cv-sparks">
      <div v-for="s in series" :key="s.key" class="cv-spark-row">
        <div class="cv-spark-head">
          <span><i :style="{ background: s.color }"></i>{{ s.key }}</span>
          <b>{{ fmt(s.values[s.values.length - 1]) }}</b>
        </div>
        <svg class="cv-spark" viewBox="0 0 620 52" role="img"
             :aria-label="s.key + ' 趋势，最新值 ' + fmt(s.values[s.values.length - 1])">
          <line class="cv-grid" x1="8" x2="612" y1="44" y2="44" />
          <polyline class="cv-line" :style="{ stroke: s.color }" :points="s.points" />
          <circle v-for="(v, i) in s.values" :key="i" class="cv-dot"
                  :style="{ fill: s.color }" :cx="s.sx(i)" :cy="s.sy(v)" r="3">
            <title>{{ items[i].label }} · {{ s.key }}: {{ fmt(v) }}</title>
          </circle>
          <text class="cv-spark-range" x="8" y="11">{{ fmt(s.min) }}</text>
          <text class="cv-spark-range" x="612" y="11" text-anchor="end">{{ fmt(s.max) }}</text>
        </svg>
      </div>
    </div>

    <!-- 环形（结构占比） -->
    <svg v-else-if="mode === 'donut' && donutSegments.length" class="chart-donut"
         viewBox="0 0 380 220" role="img" aria-label="结构占比图">
      <g transform="rotate(-90 110 110)">
        <circle v-for="(seg, i) in donutSegments" :key="i" class="cv-donut-seg"
                :style="{ stroke: seg.color }" cx="110" cy="110" r="68"
                :stroke-dasharray="seg.len + ' ' + (C - seg.len)"
                :stroke-dashoffset="-seg.offset">
          <title>{{ seg.label }}: {{ fmt(seg.value) }}（{{ seg.pct }}%）</title>
        </circle>
      </g>
      <text class="cv-donut-total" x="110" y="106" text-anchor="middle">{{ fmt(donutTotal) }}</text>
      <text class="cv-donut-label" x="110" y="126" text-anchor="middle">合计</text>
      <g class="cv-donut-legend">
        <g v-for="(seg, i) in donutSegments" :key="'lg' + i"
           :transform="'translate(206,' + (28 + i * 23) + ')'">
          <rect x="0" y="-7" width="8" height="8" rx="2" :style="{ fill: seg.color }" />
          <text class="cv-donut-name" x="14" y="0">{{ seg.label }}</text>
          <text class="cv-donut-pct" x="164" y="0" text-anchor="end">{{ seg.pct }}%</text>
        </g>
      </g>
    </svg>

    <div v-else class="empty-hint">当前数据不适合该图表</div>
  </div>
</template>

<script setup>
import { onBeforeUnmount, onMounted, ref } from 'vue'
import { createUniver, defaultTheme, LocaleType, merge } from '@univerjs/presets'
import { UniverSheetsCorePreset } from '@univerjs/preset-sheets-core'
import zhCN from '@univerjs/preset-sheets-core/locales/zh-CN'
import '@univerjs/preset-sheets-core/lib/index.css'

const props = defineProps({
  // [{ name, labels, columns, rows }]
  sheets: { type: Array, default: () => [] },
})
const emit = defineEmits(['ready'])

const host = ref()
let api = null
let instance = null

function cleanName(raw, used, index) {
  let name = String(raw || '').replace(/[\[\]:*?/\\]/g, '_').trim().slice(0, 31)
  if (!name) name = `工作表${index + 1}`
  let candidate = name
  let n = 2
  while (used.has(candidate)) candidate = `${name}(${n++})`.slice(0, 31)
  used.add(candidate)
  return candidate
}

function readValues(sheet) {
  const lastRow = sheet.getLastRow()
  const lastCol = sheet.getLastColumn()
  if (lastRow < 0 || lastCol < 0) return []
  return sheet.getRange(0, 0, lastRow + 1, lastCol + 1).getValues()
}

onMounted(() => {
  const created = createUniver({
    locale: LocaleType.ZH_CN,
    locales: { [LocaleType.ZH_CN]: merge({}, zhCN) },
    theme: defaultTheme,
    presets: [UniverSheetsCorePreset({ container: host.value })],
  })
  api = created.univerAPI
  instance = created.univer

  const used = new Set()
  const order = []
  const sheets = {}
  props.sheets.forEach((s, i) => {
    const id = `sheet-${i + 1}`
    order.push(id)
    const cellData = {}
    const offset = s.labels?.length ? 1 : 0
    if (offset) {
      const head = {}
      s.labels.forEach((v, c) => { head[c] = { v: v ?? '' } })
      cellData[0] = head
    }
    ;(s.rows || []).forEach((row, r) => {
      const line = {}
      ;(s.columns || []).forEach((col, c) => { line[c] = { v: row[col] ?? '' } })
      cellData[r + offset] = line
    })
    sheets[id] = { id, name: cleanName(s.name, used, i), cellData }
  })

  api.createWorkbook({ name: 'DataPro', sheetOrder: order, sheets })
  emit('ready')
})

onBeforeUnmount(() => {
  try { instance?.dispose?.() } catch { /* ignore */ }
  api = null
  instance = null
})

defineExpose({
  // 返回 [{ name, values: 二维数组（首行表头） }]
  extract() {
    const workbook = api?.getActiveWorkbook()
    if (!workbook) return []
    return workbook.getSheets().map(sheet => ({
      name: sheet.getSheetName(),
      values: readValues(sheet),
    }))
  },
})
</script>

<template>
  <div ref="host" class="univer-host"></div>
</template>

<style scoped>
.univer-host {
  width: 100%;
  height: calc(100vh - 300px);
  min-height: 420px;
  border: 1px solid var(--border);
  border-radius: 10px;
  overflow: hidden;
}
</style>

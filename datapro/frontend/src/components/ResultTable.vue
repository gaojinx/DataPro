<script setup>
import { computed } from 'vue'
import { ArrowDown, ArrowLeft, ArrowRight, ArrowUp, CopyDocument } from '@element-plus/icons-vue'
import { PAGE_SIZE, displayCell, displayIndex, pageCount, pageSlice, sortRows } from '../utils/table'

/**
 * 结果表（X-R01/X-R02/X-R03/X-R04/X-R05/X-R06）：
 * 排序与分页都由纯函数生成视图，绝不修改传入的 rows；复制只发事件，由父层取原始行。
 */
const props = defineProps({
  columns: { type: Array, default: () => [] },
  rows: { type: Array, default: () => [] },
  numericColumns: { type: Array, default: () => [] },
  truncated: { type: Boolean, default: false },
  rowCount: { type: Number, default: 0 },
  sort: { type: Object, default: null },
  page: { type: Number, default: 1 },
  pageSize: { type: Number, default: PAGE_SIZE },
})
const emit = defineEmits(['update:sort', 'update:page', 'copy'])

const totalPages = computed(() => pageCount(props.rows?.length || 0, props.pageSize))
const currentPage = computed(() => Math.min(Math.max(1, props.page || 1), totalPages.value))
const viewRows = computed(() => sortRows(props.rows, props.columns, props.sort))
const visibleRows = computed(() => pageSlice(viewRows.value, currentPage.value, props.pageSize))
const showPager = computed(() => (props.rows?.length || 0) > props.pageSize || props.truncated)

function toggleSort(col) {
  const s = props.sort
  if (!s || s.col !== col) emit('update:sort', { col, dir: 'asc' })
  else if (s.dir === 'asc') emit('update:sort', { col, dir: 'desc' })
  else emit('update:sort', null)          // 三态：升 → 降 → 原序
  emit('update:page', 1)
}
function goPage(p) {
  emit('update:page', Math.min(Math.max(1, p), totalPages.value))
}
function cell(col, v) { return displayCell(v, col, props.numericColumns) }
function rowNo(i) { return displayIndex(currentPage.value, i, props.pageSize) }
</script>

<template>
  <div class="result-table-block">
    <div class="rt-toolbar">
      <button class="foot-link" data-tip="复制整表 TSV（可直接粘贴到 Excel）"
              aria-label="复制整表" @click="emit('copy')">
        <el-icon><CopyDocument /></el-icon>
      </button>
    </div>

    <div class="result-table">
      <table>
        <thead>
          <tr>
            <th class="rownum-th" data-tip="显示序号，非数据库主键">#</th>
            <th v-for="col in columns" :key="col" class="sortable"
                :class="{ sorted: sort?.col === col }"
                :aria-sort="sort?.col === col ? (sort.dir === 'asc' ? 'ascending' : 'descending') : 'none'"
                data-tip="点击排序：升 → 降 → 原序（作用于已返回的全部行）"
                @click="toggleSort(col)">
              {{ col }}
              <span v-if="sort?.col === col" class="sort-arrow">
                <el-icon><ArrowUp v-if="sort.dir === 'asc'" /><ArrowDown v-else /></el-icon>
              </span>
            </th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="(row, r) in visibleRows" :key="(currentPage - 1) * pageSize + r">
            <td class="rownum-cell">{{ rowNo(r) }}</td>
            <td v-for="col in columns" :key="col" :title="String(row[col] ?? '')">{{ cell(col, row[col]) }}</td>
          </tr>
        </tbody>
      </table>
    </div>

    <div v-if="showPager" class="table-pager">
      <button class="pager-btn" :disabled="currentPage <= 1" data-tip="上一页"
              aria-label="上一页" @click="goPage(currentPage - 1)"><el-icon><ArrowLeft /></el-icon></button>
      <span class="pager-info mono">{{ currentPage }} / {{ totalPages }}</span>
      <button class="pager-btn" :disabled="currentPage >= totalPages" data-tip="下一页"
              aria-label="下一页" @click="goPage(currentPage + 1)"><el-icon><ArrowRight /></el-icon></button>
      <span v-if="truncated" class="pager-note"
            data-tip="对话内结果上限 200 行，完整数据请走导出申请">仅前 {{ rowCount }} 行，完整请导出</span>
    </div>
  </div>
</template>

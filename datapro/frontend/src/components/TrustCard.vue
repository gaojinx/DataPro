<script setup>
/**
 * 口径可信卡：把数据截止日 / 口径 / 来源 / 脱敏 / 行数 常驻提到结果卡顶部。
 * 使用者最关心「这个数能不能信」，这些徽标把依据从折叠区里拎出来。
 */
import { CircleCheck, DataLine, Grid, InfoFilled, Reading, Timer, WarningFilled } from '@element-plus/icons-vue'
import { trustBadges } from '../utils/trust'

const props = defineProps({ result: { type: Object, default: null } })

const ICONS = {
  asof: Timer, caliber: Reading, table: DataLine,
  window: InfoFilled, mask: CircleCheck, rows: Grid,
}
function iconFor(key) { return ICONS[key] || InfoFilled }
</script>

<template>
  <div v-if="trustBadges(result).length" class="trust-card" role="group" aria-label="取数可信信息">
    <span class="trust-lead" data-tip="本轮取数的依据与状态，点击查看下方完整依据">
      <el-icon><CircleCheck /></el-icon>可信
    </span>
    <span v-for="b in trustBadges(result)" :key="b.key" class="trust-badge" :class="b.kind" :data-tip="b.tip">
      <el-icon><component :is="iconFor(b.key)" /></el-icon>{{ b.label }}
    </span>
    <span v-if="result?.masked" class="trust-warn" data-tip="敏感字段已打码，聚合数值未受影响">
      <el-icon><WarningFilled /></el-icon>
    </span>
  </div>
</template>

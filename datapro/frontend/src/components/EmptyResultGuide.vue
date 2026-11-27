<script setup>
/**
 * 空结果引导：结果 0 行时，不再只显示空表，给出基于真实字段的排查建议。
 * 所有建议都来自 result 已有信息，不臆造原因。
 */
import { ArrowRight, InfoFilled } from '@element-plus/icons-vue'
import { emptyGuidance } from '../utils/trust'

const props = defineProps({ result: { type: Object, default: null } })
const emit = defineEmits(['ask-boundary'])
</script>

<template>
  <div v-if="emptyGuidance(result)" class="empty-guide" role="status">
    <div class="eg-head">
      <span class="eg-ico"><el-icon><InfoFilled /></el-icon></span>
      <b>{{ emptyGuidance(result).title }}</b>
    </div>
    <ul class="eg-tips">
      <li v-for="(t, j) in emptyGuidance(result).tips" :key="j">{{ t }}</li>
    </ul>
    <button v-if="emptyGuidance(result).canAskBoundary" class="eg-action" @click="emit('ask-boundary')">
      数据到几号 <el-icon><ArrowRight /></el-icon>
    </button>
  </div>
</template>

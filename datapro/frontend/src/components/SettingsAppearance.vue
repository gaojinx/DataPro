<script setup>
import { Sunny } from '@element-plus/icons-vue'

/** 外观风格：只上报主题选择，持久化与 DOM 应用由 App.setTheme 负责。 */
defineProps({ theme: { type: String, default: 'light' } })
const emit = defineEmits(['update:theme'])
</script>

<template>
  <div class="model-card tab-panel">
    <div class="model-card-head">
      <div class="model-card-title">
        <span class="model-ico"><el-icon><Sunny /></el-icon></span>
        <div><b>外观风格</b></div>
      </div>
    </div>
    <div class="settings-row">
      <span>界面主题</span>
      <el-radio-group :model-value="theme" @update:model-value="emit('update:theme', $event)">
        <el-radio-button value="light">亮色</el-radio-button>
        <el-radio-button value="dark">暗色</el-radio-button>
      </el-radio-group>
    </div>
  </div>
</template>

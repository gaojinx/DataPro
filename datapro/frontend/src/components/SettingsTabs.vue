<script setup>
import { computed } from 'vue'

/** 设置页签：个人 / 管理两组，图标即语义；只负责展示与派发选择。 */
const props = defineProps({
  tabs: { type: Array, default: () => [] },
  active: { type: String, default: '' },
})
const emit = defineEmits(['select'])

const personal = computed(() => props.tabs.filter(t => !t.admin))
const admin = computed(() => props.tabs.filter(t => t.admin))
</script>

<template>
  <div class="settings-tabs" role="tablist">
    <button v-for="t in personal" :key="t.k" class="settings-tab" role="tab"
            :aria-selected="active === t.k" :class="{ active: active === t.k }"
            @click="emit('select', t.k)">
      <el-icon><component :is="t.icon" /></el-icon>{{ t.label }}
    </button>
    <template v-if="admin.length">
      <span class="tabs-divider" aria-hidden="true"></span>
      <button v-for="t in admin" :key="t.k" class="settings-tab admin" role="tab"
              :aria-selected="active === t.k" :class="{ active: active === t.k }"
              @click="emit('select', t.k)">
        <el-icon><component :is="t.icon" /></el-icon>{{ t.label }}
      </button>
    </template>
  </div>
</template>

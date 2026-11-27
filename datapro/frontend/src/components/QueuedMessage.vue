<script setup>
/**
 * 待发送小框 · 回答进行中再次「发送」时，消息暂存于输入框正上方，本轮结束后自动发出。
 * 参考 WorkBuddy 排队消息设计：浅灰紧凑卡片与输入框同宽贴合；
 * 左侧拖拽点 + 内容，右侧三个操作（↑ 插入输入框 / 修改 / 删除）。
 */
import { nextTick, ref, watch } from 'vue'
import { ArrowUp, Delete, Edit } from '@element-plus/icons-vue'

const props = defineProps({
  text: { type: String, default: '' },
})
const emit = defineEmits(['update:text', 'remove', 'insert'])

const editing = ref(false)
const draft = ref('')
const editRef = ref(null)

function startEdit() {
  draft.value = props.text
  editing.value = true
  nextTick(() => {
    editRef.value?.focus()
    const len = draft.value.length
    editRef.value?.setSelectionRange?.(len, len)
  })
}

function commitEdit() {
  if (!editing.value) return
  editing.value = false
  const next = draft.value.trim()
  if (next) emit('update:text', next)
  else emit('remove')
}

function cancelEdit() { editing.value = false }

// 外部清空（发送完成 / 停止生成 / 登出）时立即退出编辑态，避免残留草稿
watch(() => props.text, (v) => { if (!v) editing.value = false })
</script>

<template>
  <div v-if="text" class="queue-card" role="group" :aria-label="`待发送：${text}`">
    <template v-if="!editing">
      <span class="queue-grip" aria-hidden="true" title="排队消息">
        <svg viewBox="0 0 10 16" width="10" height="16">
          <circle cx="2" cy="3" r="1.15" /><circle cx="8" cy="3" r="1.15" />
          <circle cx="2" cy="8" r="1.15" /><circle cx="8" cy="8" r="1.15" />
          <circle cx="2" cy="13" r="1.15" /><circle cx="8" cy="13" r="1.15" />
        </svg>
      </span>
      <div class="queue-text" title="点击修改" @click="startEdit">{{ text }}</div>
      <div class="queue-actions">
        <button class="queue-act" title="插入输入框" aria-label="插入到输入框" @click="emit('insert')">
          <el-icon><ArrowUp /></el-icon>
        </button>
        <button class="queue-act" title="修改" aria-label="修改待发送内容" @click="startEdit">
          <el-icon><Edit /></el-icon>
        </button>
        <button class="queue-act danger" title="删除" aria-label="删除待发送内容" @click="emit('remove')">
          <el-icon><Delete /></el-icon>
        </button>
      </div>
    </template>
    <textarea v-else ref="editRef" v-model="draft" class="queue-edit" rows="1"
              aria-label="编辑待发送内容"
              @keydown.enter.exact.prevent="commitEdit"
              @keydown.esc.prevent="cancelEdit"
              @blur="commitEdit" />
  </div>
</template>

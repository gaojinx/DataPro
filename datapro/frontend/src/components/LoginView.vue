<script setup>
import { ref } from 'vue'
import { ElMessage } from 'element-plus'
import { DataLine, Document, Hide, Lock, User, View } from '@element-plus/icons-vue'
import { login } from '../api/auth'

const emit = defineEmits(['success'])

const form = ref({ username: 'admin', password: '' })
const loading = ref(false)
const error = ref('')
const showPwd = ref(false)

async function submit() {
  if (loading.value) return
  const username = form.value.username.trim()
  if (!username || !form.value.password) {
    error.value = '请输入账号与密码'
    return
  }
  loading.value = true
  error.value = ''
  try {
    const user = await login(username, form.value.password)
    ElMessage.success(`欢迎回来，${user.display_name}`)
    emit('success', user)
  } catch (e) {
    error.value = e.message || '登录失败'
  } finally {
    loading.value = false
  }
}

function fill(username, password) {
  form.value = { username, password }
  error.value = ''
}
</script>

<template>
  <div class="login-page">
    <section class="login-brand">
      <div class="brand-grid" />
      <div class="login-brand-inner">
        <div class="login-logo">DP</div>
        <h1>DataPro</h1>
        <div class="login-tag">数据管家 Agent</div>
        <div class="brand-points">
          <div class="brand-point">
            <span class="point-ico"><el-icon><Lock /></el-icon></span>
            <span>只读 · 安全</span>
          </div>
          <div class="brand-point">
            <span class="point-ico"><el-icon><DataLine /></el-icon></span>
            <span>先口径 · 后取数</span>
          </div>
          <div class="brand-point">
            <span class="point-ico"><el-icon><Document /></el-icon></span>
            <span>全程留痕</span>
          </div>
        </div>
      </div>
      <div class="login-brand-foot">v0.1</div>
    </section>

    <section class="login-panel">
      <form class="login-card" @submit.prevent="submit">
        <div class="login-card-head">
          <h2>登录</h2>
          <span class="login-chip">DataPro</span>
        </div>

        <el-input v-model="form.username" size="large" placeholder="账号"
                  :prefix-icon="User" autocomplete="username" @keyup.enter="submit" />
        <el-input v-model="form.password" size="large" placeholder="密码"
                  :type="showPwd ? 'text' : 'password'" :prefix-icon="Lock"
                  autocomplete="current-password" @keyup.enter="submit">
          <template #suffix>
            <button type="button" class="pwd-eye" tabindex="-1" @click="showPwd = !showPwd">
              <el-icon><View v-if="!showPwd" /><Hide v-else /></el-icon>
            </button>
          </template>
        </el-input>

        <transition name="login-err">
          <div v-if="error" class="login-error">{{ error }}</div>
        </transition>

        <button class="login-submit" type="submit" :disabled="loading">
          <span v-if="loading" class="login-spinner" />
          {{ loading ? '登录中' : '进入 DataPro' }}
        </button>

        <div class="login-demo">
          <span class="demo-label">演示</span>
          <button type="button" class="demo-chip" @click="fill('admin', 'admin123')">admin</button>
          <button type="button" class="demo-chip" @click="fill('analyst', 'user123')">analyst</button>
        </div>
      </form>
      <div class="login-foot">© 2026 DataPro</div>
    </section>
  </div>
</template>

<script setup>
import { ref } from 'vue'
import { ElMessage } from 'element-plus'
import { Hide, Lock, User, View } from '@element-plus/icons-vue'
import { login } from '../api/auth'

const emit = defineEmits(['success'])

const form = ref({ username: 'admin', password: '' })
const loading = ref(false)
const error = ref('')
const showPwd = ref(false)

// 背景光晕轨迹：三段贝塞尔光带自动沿路径流动，与鼠标无关。
// 同一份 SVG 会复制到卡片内的实时折射副本，保证透过卡片看到的轨迹也同步弯折。
const TRAILS = [
  { d: 'M -160 760 C 120 560 360 250 660 320 S 1120 690 1760 210', cls: 'trail-blue', duration: 19, delay: 0 },
  { d: 'M -120 180 C 240 40 520 430 840 320 S 1280 60 1740 560', cls: 'trail-amber', duration: 25, delay: -8 },
  { d: 'M 180 1040 C 440 760 640 900 940 640 S 1320 450 1680 780', cls: 'trail-steel', duration: 29, delay: -15 }
]

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
    <!-- 玻璃折射滤镜：作用于卡片内的「实时背景副本」，让透过卡片看到的光环弯折。
         卡片本体轮廓不参与滤镜，保持规整矩形。Chrome 对 backdrop-filter 中的
         SVG 位移支持有限，故改用普通 filter 作用于同步的背景副本。 -->
    <svg class="glass-defs" aria-hidden="true" focusable="false" width="0" height="0">
      <defs>
        <filter id="glassRefract" x="-20%" y="-20%" width="140%" height="140%"
                color-interpolation-filters="sRGB">
          <feTurbulence type="fractalNoise" baseFrequency="0.014 0.02" numOctaves="2"
                        seed="7" result="noise" />
          <feDisplacementMap in="SourceGraphic" in2="noise" scale="14"
                             xChannelSelector="R" yChannelSelector="G" />
        </filter>
        <!-- 边缘环带：更强位移，模拟厚玻璃边框对背景的弯折 -->
        <filter id="glassEdgeRefract" x="-20%" y="-20%" width="140%" height="140%"
                color-interpolation-filters="sRGB">
          <feTurbulence type="fractalNoise" baseFrequency="0.016 0.022" numOctaves="2"
                        seed="11" result="noise" />
          <feDisplacementMap in="SourceGraphic" in2="noise" scale="22"
                             xChannelSelector="R" yChannelSelector="G" />
        </filter>
      </defs>
    </svg>

    <!-- 动态背景：同心光环呼吸 + 四团光斑漂移 + 双向扫描光带 + 自动流动的光晕轨迹 -->
    <div class="login-bg" aria-hidden="true">
      <div class="bg-rings" />
      <div class="bg-orb orb-a" />
      <div class="bg-orb orb-b" />
      <div class="bg-orb orb-c" />
      <div class="bg-orb orb-d" />
      <div class="bg-scan" />
      <div class="bg-scan bg-scan-b" />
      <svg class="bg-trails" viewBox="0 0 1600 900" preserveAspectRatio="none">
        <g v-for="(trail, i) in TRAILS" :key="trail.d + i" class="trail-group" :class="trail.cls"
           :style="{ '--trail-duration': trail.duration + 's', '--trail-delay': trail.delay + 's' }">
          <path class="trail-glow" :d="trail.d" />
          <path class="trail-halo" :d="trail.d" />
          <path class="trail-core" :d="trail.d" />
        </g>
      </svg>
    </div>

    <!-- 居中主体：品牌标记 + 登录卡 -->
    <main class="login-stage">
      <div class="login-mark">
        <div class="login-logo">DP</div>
        <h1>DataPro</h1>
      </div>

      <form class="login-card" @submit.prevent="submit">
        <!-- 实时折射层：这里是动态背景的同步副本（相同节点与动画），背景呼吸/旋转/
             扫描时透过卡片看到的形变也跟着实时变化；field 整卡轻形变，edge 边框环带强形变。 -->
        <div class="card-refract" aria-hidden="true">
          <div v-for="layer in ['field', 'edge']" :key="layer"
               :class="'refract-ring refract-ring-' + layer">
            <div class="refract-viewport">
              <div class="bg-rings" />
              <div class="bg-orb orb-a" />
              <div class="bg-orb orb-b" />
              <div class="bg-orb orb-c" />
              <div class="bg-orb orb-d" />
              <div class="bg-scan" />
              <div class="bg-scan bg-scan-b" />
              <svg class="bg-trails" viewBox="0 0 1600 900" preserveAspectRatio="none">
                <g v-for="(trail, i) in TRAILS" :key="trail.d + i" class="trail-group" :class="trail.cls"
                   :style="{ '--trail-duration': trail.duration + 's', '--trail-delay': trail.delay + 's' }">
                  <path class="trail-glow" :d="trail.d" />
                  <path class="trail-halo" :d="trail.d" />
                  <path class="trail-core" :d="trail.d" />
                </g>
              </svg>
            </div>
          </div>
        </div>

        <div class="float-field" :class="{ 'is-filled': !!form.username }">
          <label class="float-label">账号</label>
          <el-input v-model="form.username" size="large" placeholder=" "
                    :prefix-icon="User" autocomplete="username" @keyup.enter="submit" />
        </div>
        <div class="float-field" :class="{ 'is-filled': !!form.password }">
          <label class="float-label">密码</label>
          <el-input v-model="form.password" size="large" placeholder=" "
                    :type="showPwd ? 'text' : 'password'" :prefix-icon="Lock"
                    autocomplete="current-password" @keyup.enter="submit">
            <template #suffix>
              <button type="button" class="pwd-eye" tabindex="-1" @click="showPwd = !showPwd">
                <el-icon><View v-if="!showPwd" /><Hide v-else /></el-icon>
              </button>
            </template>
          </el-input>
        </div>

        <transition name="login-err">
          <div v-if="error" class="login-error">{{ error }}</div>
        </transition>

        <button class="login-submit" type="submit" :disabled="loading">
          <span v-if="loading" class="login-spinner" />
          {{ loading ? '登录中' : '进入 DataPro' }}
        </button>

        <div class="login-demo">
          <button type="button" class="demo-chip" @click="fill('admin', 'admin123')">admin</button>
          <button type="button" class="demo-chip" @click="fill('analyst', 'user123')">analyst</button>
        </div>
      </form>
    </main>
  </div>
</template>

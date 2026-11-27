<script setup>
import { Grid, SwitchButton, User } from '@element-plus/icons-vue'

/** 账户与统计：本人信息 + 能力概览（普通用户）+ 全部账户只读表（管理员）。 */
defineProps({
  username: { type: String, default: '' },
  displayName: { type: String, default: '' },
  role: { type: String, default: 'user' },
  roleName: { type: String, default: '普通用户' },
  abilities: { type: Array, default: () => [] },
  users: { type: Array, default: () => [] },
})
const emit = defineEmits(['navigate', 'logout'])
</script>

<template>
  <div class="model-card tab-panel">
    <div class="model-card-head">
      <div class="model-card-title">
        <span class="model-ico"><el-icon><User /></el-icon></span>
        <div><b>账户与统计</b></div>
      </div>
    </div>

    <div v-if="role !== 'admin'" class="ability-card">
      <div class="ability-head"><el-icon><Grid /></el-icon><b>我能使用</b></div>
      <div class="ability-grid">
        <button v-for="a in abilities" :key="a.k" class="ability-item" @click="emit('navigate', a.view)">
          <span class="ability-ico"><el-icon><component :is="a.icon" /></el-icon></span>
          <span>{{ a.label }}</span>
        </button>
      </div>
    </div>

    <div class="settings-row"><span>账号</span><b>{{ username || '—' }}</b></div>
    <div class="settings-row"><span>显示名</span><b>{{ displayName }}</b></div>
    <div class="settings-row">
      <span>角色</span>
      <span class="pill-current">{{ roleName }}</span>
    </div>
    <div class="settings-row">
      <span>登录状态</span>
      <button class="model-act" @click="emit('logout')">
        <el-icon><SwitchButton /></el-icon>退出登录
      </button>
    </div>

    <template v-if="role === 'admin'">
      <div class="settings-subhead" style="margin-top:16px">
        <b>全部账户</b>
        <span class="faint-cell">{{ users.length }} 个账号 · 账号维护在「权限与角色」</span>
      </div>
      <table class="model-table">
        <thead>
          <tr>
            <th>账号</th>
            <th>显示名</th>
            <th>角色</th>
            <th>状态</th>
            <th>最近登录</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="u in users" :key="u.id">
            <td><b>{{ u.username }}</b></td>
            <td>{{ u.display_name }}</td>
            <td>{{ u.role === 'admin' ? '管理员' : '普通用户' }}</td>
            <td>
              <span :class="u.status === 'active' ? 'pill-neutral' : 'pill-lock'">
                {{ u.status === 'active' ? '启用' : '禁用' }}
              </span>
            </td>
            <td class="faint-cell">{{ (u.last_login_at || '').replace('T', ' ') || '—' }}</td>
          </tr>
          <tr v-if="!users.length">
            <td colspan="5" class="faint-cell">暂无账户</td>
          </tr>
        </tbody>
      </table>
    </template>
  </div>
</template>

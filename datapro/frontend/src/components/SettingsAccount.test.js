// @vitest-environment happy-dom
import { describe, expect, it } from 'vitest'
import { mount } from '@vue/test-utils'
import { defineComponent, h } from 'vue'
import SettingsAccount from './SettingsAccount.vue'

const IconStub = defineComponent({
  name: 'ElIcon',
  setup(_, { slots }) { return () => h('span', { class: 'el-icon' }, slots.default?.()) },
})
const stubs = { 'el-icon': IconStub, ElIcon: IconStub }
const ABILITIES = [
  { k: 'chat', label: '智能问数', view: 'chat' },
  { k: 'files', label: '文件分析', view: 'files' },
]

describe('SettingsAccount · 普通用户', () => {
  function mountUser() {
    return mount(SettingsAccount, {
      props: {
        username: 'analyst', displayName: '小王', role: 'user', roleName: '普通用户',
        abilities: ABILITIES,
      },
      global: { stubs },
    })
  }

  it('展示本人信息与能力概览，不显示管理员账户表', () => {
    const w = mountUser()
    const text = w.text()
    expect(text).toContain('analyst')
    expect(text).toContain('小王')
    expect(text).toContain('普通用户')
    expect(w.find('.ability-card').exists()).toBe(true)
    expect(w.find('.model-table').exists()).toBe(false)
  })

  it('点击能力项派发 navigate(view)，退出派发 logout', async () => {
    const w = mountUser()
    await w.findAll('.ability-item')[1].trigger('click')
    expect(w.emitted('navigate')?.[0]).toEqual(['files'])

    await w.find('.model-act').trigger('click')
    expect(w.emitted('logout')).toHaveLength(1)
  })
})

describe('SettingsAccount · 管理员', () => {
  it('隐藏能力概览，展示只读账户表', () => {
    const w = mount(SettingsAccount, {
      props: {
        username: 'admin', displayName: '管理员', role: 'admin', roleName: '管理员',
        abilities: ABILITIES,
        users: [
          { id: 1, username: 'admin', display_name: '管理员', role: 'admin', status: 'active', last_login_at: '2026-11-26T10:00:00' },
          { id: 2, username: 'analyst', display_name: '小王', role: 'user', status: 'disabled', last_login_at: null },
        ],
      },
      global: { stubs },
    })

    expect(w.find('.ability-card').exists()).toBe(false)
    expect(w.find('.model-table').exists()).toBe(true)
    expect(w.findAll('tbody tr')).toHaveLength(2)
    expect(w.text()).toContain('2026-11-26 10:00:00')
    expect(w.text()).toContain('禁用')
  })

  it('账户为空时显示占位行', () => {
    const w = mount(SettingsAccount, {
      props: { username: 'admin', role: 'admin', roleName: '管理员' },
      global: { stubs },
    })
    expect(w.text()).toContain('暂无账户')
  })
})

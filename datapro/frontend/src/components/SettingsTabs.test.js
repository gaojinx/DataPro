// @vitest-environment happy-dom
import { describe, expect, it } from 'vitest'
import { mount } from '@vue/test-utils'
import { defineComponent, h } from 'vue'
import SettingsTabs from './SettingsTabs.vue'

const IconStub = defineComponent({
  name: 'ElIcon',
  setup(_, { slots }) { return () => h('span', { class: 'el-icon' }, slots.default?.()) },
})
const stubs = { 'el-icon': IconStub, ElIcon: IconStub }
const TABS = [
  { k: 'appearance', label: '外观风格' },
  { k: 'account', label: '账户与统计' },
  { k: 'model', label: '模型与AI', admin: true },
  { k: 'role', label: '权限与角色', admin: true },
]

describe('SettingsTabs', () => {
  it('个人组在前、管理组在分隔线后，选中态由 active 控制', () => {
    const w = mount(SettingsTabs, { props: { tabs: TABS, active: 'account' }, global: { stubs } })
    const btns = w.findAll('button')

    expect(btns.map(b => b.text())).toEqual(['外观风格', '账户与统计', '模型与AI', '权限与角色'])
    expect(w.find('.tabs-divider').exists()).toBe(true)
    expect(btns[1].classes()).toContain('active')
    expect(btns[1].attributes('aria-selected')).toBe('true')
    expect(btns[2].classes()).toContain('admin')
  })

  it('点击页签只派发 key，不自行修改状态', async () => {
    const w = mount(SettingsTabs, { props: { tabs: TABS, active: 'account' }, global: { stubs } })
    await w.findAll('button')[3].trigger('click')
    expect(w.emitted('select')?.[0]).toEqual(['role'])
    // 受控组件：active prop 不变时 DOM 选中态不自行变化
    expect(w.findAll('button')[1].classes()).toContain('active')
  })

  it('没有管理分组时不渲染分隔线', () => {
    const w = mount(SettingsTabs, {
      props: { tabs: TABS.filter(t => !t.admin), active: 'appearance' }, global: { stubs },
    })
    expect(w.findAll('button')).toHaveLength(2)
    expect(w.find('.tabs-divider').exists()).toBe(false)
  })
})

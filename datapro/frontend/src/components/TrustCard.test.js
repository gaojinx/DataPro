// @vitest-environment happy-dom
import { describe, expect, it } from 'vitest'
import { mount } from '@vue/test-utils'
import { defineComponent, h } from 'vue'
import TrustCard from './TrustCard.vue'

const IconStub = defineComponent({
  name: 'ElIcon',
  setup(_, { slots }) { return () => h('span', { class: 'el-icon' }, slots.default?.()) },
})
const stubs = { 'el-icon': IconStub, ElIcon: IconStub }
const RESULT = {
  row_count: 3, data_as_of: '2026-11-26', masked: 1,
  time_window: '最近 7 天',
  sources: [{ type: 'caliber', name: '支付订单数' }, { type: 'table', name: 'dwd_order' }],
}

describe('TrustCard', () => {
  it('无 result 时不渲染；有数据时渲染图标徽标', () => {
    expect(mount(TrustCard, { props: { result: null }, global: { stubs } }).find('.trust-card').exists()).toBe(false)
    const w = mount(TrustCard, { props: { result: RESULT }, global: { stubs } })
    const badges = w.findAll('.trust-badge')
    expect(badges).toHaveLength(6)
    expect(w.find('.trust-lead').text()).toContain('可信')
    expect(w.find('.trust-warn').exists()).toBe(true)
  })

  it('缺字段时只渲染存在的徽标', () => {
    const w = mount(TrustCard, { props: { result: { row_count: 0 } }, global: { stubs } })
    expect(w.findAll('.trust-badge')).toHaveLength(0)
    expect(w.find('.trust-card').exists()).toBe(false)
  })
})

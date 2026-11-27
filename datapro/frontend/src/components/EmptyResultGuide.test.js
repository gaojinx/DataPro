// @vitest-environment happy-dom
import { describe, expect, it } from 'vitest'
import { mount } from '@vue/test-utils'
import { defineComponent, h } from 'vue'
import EmptyResultGuide from './EmptyResultGuide.vue'

const IconStub = defineComponent({
  name: 'ElIcon',
  setup(_, { slots }) { return () => h('span', { class: 'el-icon' }, slots.default?.()) },
})
const stubs = { 'el-icon': IconStub, ElIcon: IconStub }

describe('EmptyResultGuide', () => {
  it('0 行且有真实字段时给出建议，点按钮派发 ask-boundary', async () => {
    const w = mount(EmptyResultGuide, {
      props: { result: { row_count: 0, data_as_of: '2026-11-26', time_window: '最近 7 天' } },
      global: { stubs },
    })
    expect(w.find('.empty-guide').exists()).toBe(true)
    expect(w.find('.eg-head').text()).toContain('没有查到数据')
    expect(w.findAll('.eg-tips li')).toHaveLength(2)
    await w.find('.eg-action').trigger('click')
    expect(w.emitted('ask-boundary')?.[0]).toEqual([])
  })

  it('有数据时完全不渲染引导', () => {
    const w = mount(EmptyResultGuide, { props: { result: { row_count: 5 } }, global: { stubs } })
    expect(w.find('.empty-guide').exists()).toBe(false)
  })
})

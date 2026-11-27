// @vitest-environment happy-dom
import { describe, expect, it } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { defineComponent, h, inject, provide } from 'vue'
import SettingsAppearance from './SettingsAppearance.vue'

const IconStub = defineComponent({
  name: 'ElIcon',
  setup(_, { slots }) { return () => h('span', { class: 'el-icon' }, slots.default?.()) },
})

// 用最小 radio stub 复现 Element Plus 的 group/button 联动，避免全量挂载
const RadioGroupStub = defineComponent({
  name: 'ElRadioGroup',
  props: ['modelValue'],
  emits: ['update:modelValue'],
  setup(props, { emit, slots }) {
    provide('radio-group', {
      choose: (v) => emit('update:modelValue', v),
      current: () => props.modelValue,
    })
    return () => h('div', { class: 'radio-group' }, slots.default?.())
  },
})
const RadioButtonStub = defineComponent({
  name: 'ElRadioButton',
  props: ['value'],
  setup(props, { slots }) {
    const group = inject('radio-group')
    return () => h('button', {
      type: 'button',
      'data-value': props.value,
      'aria-checked': group?.current() === props.value ? 'true' : 'false',
      onClick: () => group?.choose(props.value),
    }, slots.default?.())
  },
})

function mountAppearance(theme = 'light') {
  return mount(SettingsAppearance, {
    props: { theme },
    global: {
      stubs: {
        'el-icon': IconStub,
        ElIcon: IconStub,
        'el-radio-group': RadioGroupStub,
        ElRadioGroup: RadioGroupStub,
        'el-radio-button': RadioButtonStub,
        ElRadioButton: RadioButtonStub,
      },
    },
  })
}

describe('SettingsAppearance', () => {
  it('渲染亮色/暗色两档并标记当前主题', () => {
    const w = mountAppearance('dark')
    const opts = w.findAll('button')
    expect(opts.map(o => o.text())).toEqual(['亮色', '暗色'])
    expect(opts[1].attributes('aria-checked')).toBe('true')
  })

  it('点击暗色只上报 theme，不直接改 DOM 主题', async () => {
    const w = mountAppearance()
    await w.findAll('button')[1].trigger('click')
    await flushPromises()
    expect(w.emitted('update:theme')?.[0]).toEqual(['dark'])
    // 受控：prop 未变时选中态不变
    expect(w.findAll('button')[0].attributes('aria-checked')).toBe('true')
  })
})

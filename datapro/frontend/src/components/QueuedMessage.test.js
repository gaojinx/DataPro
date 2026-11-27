// @vitest-environment happy-dom
import { describe, expect, it } from 'vitest'
import { mount } from '@vue/test-utils'
import { defineComponent, h } from 'vue'
import QueuedMessage from './QueuedMessage.vue'

const IconStub = defineComponent({
  name: 'ElIcon',
  setup(_, { slots }) { return () => h('span', { class: 'el-icon' }, slots.default?.()) },
})
const stubs = { 'el-icon': IconStub, ElIcon: IconStub }
const mountCard = (text = '统计上月订单') =>
  mount(QueuedMessage, { props: { text }, global: { stubs } })

describe('QueuedMessage', () => {
  it('无待发送内容时不渲染；有内容时显示文案与三个操作', () => {
    expect(mountCard('').find('.queue-card').exists()).toBe(false)
    const w = mountCard()
    expect(w.find('.queue-text').text()).toBe('统计上月订单')
    expect(w.find('.queue-grip').exists()).toBe(true)
    const acts = w.findAll('.queue-act')
    expect(acts).toHaveLength(3)
    expect(acts.map(b => b.attributes('aria-label'))).toEqual([
      '插入到输入框', '修改待发送内容', '删除待发送内容',
    ])
  })

  it('删除与插入只派发事件，不自行改动内容（受控组件）', async () => {
    const w = mountCard()
    await w.findAll('.queue-act')[2].trigger('click')   // 删除
    expect(w.emitted('remove')?.[0]).toEqual([])
    await w.findAll('.queue-act')[0].trigger('click')   // 插入
    expect(w.emitted('insert')?.[0]).toEqual([])
    expect(w.find('.queue-text').text()).toBe('统计上月订单')
  })

  it('点击修改进入编辑态，Enter 提交新文本并退出', async () => {
    const w = mountCard()
    await w.findAll('.queue-act')[1].trigger('click')   // 修改
    const ta = w.find('textarea.queue-edit')
    expect(ta.exists()).toBe(true)
    expect(ta.element.value).toBe('统计上月订单')

    await ta.setValue('统计本月订单')
    await ta.trigger('keydown', { key: 'Enter' })
    expect(w.emitted('update:text')?.[0]).toEqual(['统计本月订单'])
    expect(w.find('textarea.queue-edit').exists()).toBe(false)
  })

  it('Shift+Enter 不提交，Esc 取消编辑，均不派发修改事件', async () => {
    const w = mountCard()
    await w.find('.queue-text').trigger('click')   // 点击正文同样进入编辑
    const ta = w.find('textarea.queue-edit')
    await ta.setValue('改成别的')
    await ta.trigger('keydown', { key: 'Enter', shiftKey: true })
    expect(w.emitted('update:text')).toBeUndefined()
    expect(w.find('textarea.queue-edit').exists()).toBe(true)

    await ta.trigger('keydown', { key: 'Escape' })
    expect(w.emitted('update:text')).toBeUndefined()
    expect(w.find('textarea.queue-edit').exists()).toBe(false)
  })

  it('失焦提交；清空内容视为删除', async () => {
    const w = mountCard()
    await w.findAll('.queue-act')[1].trigger('click')
    const ta = w.find('textarea.queue-edit')
    await ta.setValue('  新内容  ')
    await ta.trigger('blur')
    expect(w.emitted('update:text')?.[0]).toEqual(['新内容'])

    const w2 = mountCard()
    await w2.findAll('.queue-act')[1].trigger('click')
    await w2.find('textarea.queue-edit').setValue('  ')
    await w2.find('textarea.queue-edit').trigger('keydown', { key: 'Enter' })
    expect(w2.emitted('remove')?.[0]).toEqual([])
    expect(w2.emitted('update:text')).toBeUndefined()
  })

  it('外部清空内容时立即退出编辑态，不残留草稿', async () => {
    const w = mountCard()
    await w.findAll('.queue-act')[1].trigger('click')
    expect(w.find('textarea.queue-edit').exists()).toBe(true)
    await w.setProps({ text: '' })
    expect(w.find('textarea.queue-edit').exists()).toBe(false)
    expect(w.find('.queue-card').exists()).toBe(false)
  })
})

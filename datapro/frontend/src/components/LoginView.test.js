// @vitest-environment happy-dom
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { defineComponent, h } from 'vue'
import LoginView from './LoginView.vue'
import { login } from '../api/auth'
import { ElMessage } from 'element-plus'

vi.mock('../api/auth', () => ({ login: vi.fn() }))

// 按需引入后 el-input/el-icon 会直接从 element-plus 导入，mock 里需一并提供桩组件
vi.mock('element-plus', async () => {
  const { defineComponent, h } = await import('vue')
  const ElInput = defineComponent({
    name: 'ElInput',
    props: ['modelValue', 'type', 'placeholder', 'size', 'prefixIcon', 'autocomplete'],
    emits: ['update:modelValue', 'keyup'],
    setup(props, { emit, slots }) {
      return () => h('span', { class: 'stub-field' }, [
        h('input', {
          class: 'stub-input',
          value: props.modelValue ?? '',
          type: props.type === 'password' ? 'password' : 'text',
          onInput: (e) => emit('update:modelValue', e.target.value),
          onKeyup: (e) => emit('keyup', e),
        }),
        slots.suffix?.() || null,
      ])
    },
  })
  const ElIcon = defineComponent({
    name: 'ElIcon',
    setup(_, { slots }) { return () => h('span', { class: 'el-icon' }, slots.default?.()) },
  })
  return {
    ElMessage: { success: vi.fn(), error: vi.fn(), warning: vi.fn() },
    ElInput,
    ElIcon,
  }
})

// el-input 由主应用全局注册；这里用受控 input stub，保留 suffix 插槽给密码眼睛按钮
const InputStub = defineComponent({
  name: 'ElInput',
  props: ['modelValue', 'type', 'placeholder', 'size', 'prefixIcon', 'autocomplete'],
  emits: ['update:modelValue', 'keyup'],
  setup(props, { emit, slots }) {
    return () => h('span', { class: 'stub-field' }, [
      h('input', {
        class: 'stub-input',
        value: props.modelValue ?? '',
        type: props.type === 'password' ? 'password' : 'text',
        onInput: (e) => emit('update:modelValue', e.target.value),
        onKeyup: (e) => emit('keyup', e),
      }),
      slots.suffix?.() || null,
    ])
  },
})
const IconStub = defineComponent({
  name: 'ElIcon',
  setup(_, { slots }) { return () => h('span', { class: 'el-icon' }, slots.default?.()) },
})

function mountLogin() {
  return mount(LoginView, {
    global: { stubs: { 'el-input': InputStub, ElInput: InputStub, 'el-icon': IconStub, ElIcon: IconStub } },
  })
}

beforeEach(() => {
  vi.clearAllMocks()
  login.mockReset()
})

describe('LoginView · 表单校验', () => {
  it('默认预填 admin，空密码提交只提示不调接口', async () => {
    const w = mountLogin()
    expect(w.findAll('input')[0].element.value).toBe('admin')

    await w.find('form').trigger('submit')
    expect(w.find('.login-error').text()).toBe('请输入账号与密码')
    expect(login).not.toHaveBeenCalled()
  })

  it('演示账号一键填入后可提交成功并派发 success', async () => {
    const w = mountLogin()
    const user = { username: 'admin', display_name: '管理员' }
    login.mockResolvedValue(user)

    await w.findAll('.demo-chip')[0].trigger('click')
    expect(w.findAll('input')[0].element.value).toBe('admin')
    expect(w.findAll('input')[1].element.value).toBe('admin123')

    await w.find('form').trigger('submit')
    await flushPromises()

    expect(login).toHaveBeenCalledWith('admin', 'admin123')
    expect(w.emitted('success')?.[0][0]).toEqual(user)
    expect(ElMessage.success).toHaveBeenCalled()
    expect(w.find('.login-error').exists()).toBe(false)
  })

  it('登录失败显示后端错误文案且不派发 success', async () => {
    const w = mountLogin()
    login.mockRejectedValue(new Error('账号或密码错误 [E008]'))

    await w.findAll('.demo-chip')[1].trigger('click')
    await w.find('form').trigger('submit')
    await flushPromises()

    expect(w.find('.login-error').text()).toContain('账号或密码错误 [E008]')
    expect(w.emitted('success')).toBeUndefined()
  })
})

describe('LoginView · 交互细节', () => {
  it('密码眼睛切换明文/密文', async () => {
    const w = mountLogin()
    expect(w.findAll('input')[1].attributes('type')).toBe('password')

    await w.find('.pwd-eye').trigger('click')
    expect(w.findAll('input')[1].attributes('type')).toBe('text')

    await w.find('.pwd-eye').trigger('click')
    expect(w.findAll('input')[1].attributes('type')).toBe('password')
  })

  it('请求进行中按钮禁用并显示登录中，结束后恢复', async () => {
    let resolveLogin
    login.mockReturnValue(new Promise((r) => { resolveLogin = r }))
    const w = mountLogin()

    await w.findAll('.demo-chip')[0].trigger('click')
    await w.find('form').trigger('submit')
    expect(w.find('.login-submit').attributes('disabled')).toBeDefined()
    expect(w.find('.login-submit').text()).toContain('登录中')

    resolveLogin({ username: 'admin', display_name: '管理员' })
    await flushPromises()
    expect(w.find('.login-submit').text()).toContain('进入 DataPro')
    expect(w.find('.login-submit').attributes('disabled')).toBeUndefined()
  })
})

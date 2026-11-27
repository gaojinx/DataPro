import { describe, expect, it } from 'vitest'
import { draftKeyFor, readDraft, removeDraft, writeDraft } from './draft'

function memoryStorage() {
  const map = new Map()
  return {
    getItem: (k) => (map.has(k) ? map.get(k) : null),
    setItem: (k, v) => map.set(k, String(v)),
    removeItem: (k) => map.delete(k),
  }
}

describe('draftKeyFor', () => {
  it('按账号隔离，缺省归到 anon', () => {
    expect(draftKeyFor('analyst')).toBe('dp_draft_analyst')
    expect(draftKeyFor('admin')).not.toBe(draftKeyFor('analyst'))
    expect(draftKeyFor('')).toBe('dp_draft_anon')
    expect(draftKeyFor(undefined)).toBe('dp_draft_anon')
  })
})

describe('草稿读写', () => {
  it('写入后可读回，清空后为空', () => {
    const s = memoryStorage()
    const key = draftKeyFor('analyst')
    expect(writeDraft(s, key, '昨天的支付订单数')).toBe(true)
    expect(readDraft(s, key)).toBe('昨天的支付订单数')
    expect(removeDraft(s, key)).toBe(true)
    expect(readDraft(s, key)).toBe('')
  })

  it('空白草稿不落盘（避免残留空串）', () => {
    const s = memoryStorage()
    writeDraft(s, 'k', '   ')
    expect(s.getItem('k')).toBeNull()
  })

  it('账号切换不串草稿', () => {
    const s = memoryStorage()
    writeDraft(s, draftKeyFor('analyst'), 'analyst 的草稿')
    writeDraft(s, draftKeyFor('admin'), 'admin 的草稿')
    expect(readDraft(s, draftKeyFor('analyst'))).toBe('analyst 的草稿')
    expect(readDraft(s, draftKeyFor('admin'))).toBe('admin 的草稿')
    removeDraft(s, draftKeyFor('analyst'))
    expect(readDraft(s, draftKeyFor('admin'))).toBe('admin 的草稿')
  })

  it('storage 抛异常（配额/隐私模式）时静默降级', () => {
    const bad = {
      getItem() { throw new Error('quota') },
      setItem() { throw new Error('quota') },
      removeItem() { throw new Error('quota') },
    }
    expect(writeDraft(bad, 'k', 'x')).toBe(false)
    expect(readDraft(bad, 'k')).toBe('')
    expect(removeDraft(bad, 'k')).toBe(false)
  })
})

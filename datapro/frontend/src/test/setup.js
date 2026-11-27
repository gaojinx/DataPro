/**
 * 测试环境最小垫片：node 环境下没有 localStorage / window，
 * 这里提供内存实现，供 auth/errors/draft 等模块在单测中安全调用。
 */

class MemoryStorage {
  constructor() { this.map = new Map() }
  getItem(key) { return this.map.has(key) ? this.map.get(key) : null }
  setItem(key, value) { this.map.set(key, String(value)) }
  removeItem(key) { this.map.delete(key) }
  clear() { this.map.clear() }
  key(index) { return [...this.map.keys()][index] ?? null }
  get length() { return this.map.size }
}

if (!globalThis.localStorage) globalThis.localStorage = new MemoryStorage()

if (!globalThis.window) {
  const listeners = new Map()
  globalThis.window = {
    addEventListener(type, fn) {
      if (!listeners.has(type)) listeners.set(type, [])
      listeners.get(type).push(fn)
    },
    removeEventListener(type, fn) {
      listeners.set(type, (listeners.get(type) || []).filter(x => x !== fn))
    },
    dispatchEvent(event) {
      for (const fn of listeners.get(event.type) || []) fn(event)
      return true
    },
  }
}

if (!globalThis.CustomEvent) {
  globalThis.CustomEvent = class CustomEvent {
    constructor(type, init = {}) { this.type = type; Object.assign(this, init) }
  }
}

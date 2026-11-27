import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import Components from 'unplugin-vue-components/vite'
import { ElementPlusResolver } from 'unplugin-vue-components/resolvers'

export default defineConfig({
  plugins: [
    vue(),
    // Element Plus 按需引入：模板里的 el-* 组件自动注册并带对应样式；
    // 脚本里显式 import 的 ElMessage 等程序化 API 的样式在 main.js 手动引入。
    // 单测环境跳过样式注入：node 无法加载 theme-chalk 的 css，且测试里 el-* 均已 stub。
    Components({
      resolvers: [ElementPlusResolver({ importStyle: !process.env.VITEST })],
      dts: false,
    }),
  ],
  // 单测默认 node 环境：setup 里补最小 localStorage/window，避免为纯函数测试引入 jsdom
  test: {
    environment: 'node',
    include: ['src/**/*.test.js'],
    setupFiles: ['./src/test/setup.js'],
  },
  server: {
    host: true,
    port: 5175,
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8100',
        changeOrigin: true,
      },
      '/admin': {
        target: 'http://127.0.0.1:8100',
        changeOrigin: true,
      },
    },
  },
})

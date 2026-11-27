import { createApp, defineComponent, h } from 'vue'
import { ElConfigProvider } from 'element-plus'
import zhCn from 'element-plus/es/locale/lang/zh-cn'
import App from './App.vue'
import './style.css'

// Element Plus 按需引入（unplugin-vue-components 处理模板里的 el-*）；
// 程序化 API 不经过模板，需要手动补样式：
import 'element-plus/es/components/config-provider/style/css'
import 'element-plus/es/components/message/style/css'
import 'element-plus/es/components/icon/style/css'

// 全局配置（中文 locale / zIndex）由 ConfigProvider 提供，替代整包 app.use(ElementPlus)
const Root = defineComponent({
  render: () => h(ElConfigProvider, { locale: zhCn }, () => h(App)),
})

createApp(Root).mount('#app')

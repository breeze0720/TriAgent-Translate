import { createApp } from 'vue'
import App from './App.vue'

// 样式分层：设计变量 -> 基础组件 -> 工作台布局
import './styles/tokens.css'
import './styles/base.css'
import './styles/layout.css'

createApp(App).mount('#app')
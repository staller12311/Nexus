import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import App from './App'
import { ErrorBoundary } from './components/ErrorBoundary'
import './index.css'

const container = document.getElementById('root')
if (!container) throw new Error('找不到 #root 挂载点')

createRoot(container, {
  // React 19 的 root 级错误钩子：把完整组件栈打到 Console，方便用户一键反馈
  onUncaughtError: (error, errorInfo) => {
    console.error('[MyAi] React 未捕获异常：', error, '\n组件栈：', errorInfo.componentStack)
  },
  onCaughtError: (error, errorInfo) => {
    console.error('[MyAi] React 捕获异常：', error, '\n组件栈：', errorInfo.componentStack)
  },
}).render(
  <StrictMode>
    <ErrorBoundary>
      <App />
    </ErrorBoundary>
  </StrictMode>,
)
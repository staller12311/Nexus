import { Component } from 'react'
import type { ErrorInfo, ReactNode } from 'react'

type Props = { children: ReactNode }
type State = { error: Error | null; componentStack: string }

/**
 * 页面级错误边界：任何一个组件渲染抛错时，不把整棵 React 树卸载成白屏，
 * 而是显示可恢复的提示 + 完整堆栈（便于定位「Maximum update depth」这类问题）。
 *
 * 对话数据在服务端已落库，刷新页面即可恢复，不会丢消息。
 */
export class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null, componentStack: '' }

  static getDerivedStateFromError(error: Error): Partial<State> {
    return { error }
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    // 开发时这条日志会进浏览器 Console；用户若反馈，复制这一整块即可看到组件栈
    console.error('[MyAi] 页面渲染异常：', error, '\n组件栈：', info.componentStack)
    this.setState({ componentStack: info.componentStack ?? '' })
  }

  render() {
    const { error, componentStack } = this.state
    if (!error) return this.props.children

    return (
      <div className="flex h-full min-h-0 items-center justify-center bg-slate-50 p-6">
        <div className="w-full max-w-2xl rounded-2xl border border-slate-200 bg-white p-5 shadow-sm">
          <div className="text-base font-semibold text-slate-800">页面渲染出错了</div>
          <p className="mt-1 text-xs leading-relaxed text-slate-500">
            对话内容已经保存在服务端，刷新页面即可恢复。请把下方错误信息（尤其是「组件栈」）反馈给开发者。
          </p>
          <pre className="mt-3 max-h-48 overflow-auto whitespace-pre-wrap break-all rounded-lg bg-slate-50 p-3 font-mono text-[11px] text-rose-600">
            {error.message}
          </pre>
          {componentStack ? (
            <details className="mt-2 rounded-lg border border-slate-200 p-2">
              <summary className="cursor-pointer text-[11px] text-slate-500">组件栈（点击展开）</summary>
              <pre className="mt-1 max-h-56 overflow-auto whitespace-pre-wrap break-all font-mono text-[10px] text-slate-500">
                {componentStack}
              </pre>
            </details>
          ) : null}
          <div className="mt-4 flex gap-2">
            <button
              type="button"
              onClick={() => window.location.reload()}
              className="rounded-lg bg-gray-900 px-3 py-1.5 text-xs font-medium text-white hover:bg-gray-800"
            >
              重新加载页面
            </button>
            <button
              type="button"
              onClick={() => this.setState({ error: null, componentStack: '' })}
              className="rounded-lg border border-slate-200 px-3 py-1.5 text-xs text-slate-600 hover:bg-slate-50"
            >
              尝试继续
            </button>
          </div>
        </div>
      </div>
    )
  }
}
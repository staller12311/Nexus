import { apiBase } from './base'

/** 同一错误 5 秒内只上报一次，避免渲染循环时把后端日志刷爆 */
let lastKey = ''
let lastAt = 0

/**
 * 把前端运行时错误上报到本地后端（写入 data/agent.log）。
 * 只用于诊断：任何失败都必须静默，绝不能影响主流程。
 */
export function reportClientError(source: string, error: unknown, componentStack = ''): void {
  try {
    const message = error instanceof Error ? error.message : String(error)
    const stack = error instanceof Error ? (error.stack ?? '') : ''
    const key = `${source}|${message}|${stack.slice(0, 200)}`
    const now = Date.now()
    if (key === lastKey && now - lastAt < 5000) return
    lastKey = key
    lastAt = now

    void fetch(`${apiBase()}/api/client-error`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        source,
        message,
        stack,
        component_stack: componentStack,
        url: window.location.href,
      }),
      keepalive: true,
    }).catch(() => undefined)
  } catch {
    /* 诊断上报失败绝不影响主流程 */
  }
}
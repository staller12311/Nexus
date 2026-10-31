/**
 * 后端地址前缀。
 *
 * 浏览器/开发环境：默认空串 → `/api/xxx` 由 Vite proxy 转发。
 * Tauri 桌面端：Rust 在页面脚本执行前注入 `window.__NEXUS_API_BASE__`
 * （http://127.0.0.1:<动态端口>），这里优先读取它。
 */
declare global {
  interface Window {
    __NEXUS_API_BASE__?: string
  }
}

export function apiBase(): string {
  if (typeof window !== 'undefined' && window.__NEXUS_API_BASE__) {
    return window.__NEXUS_API_BASE__
  }
  return import.meta.env.VITE_API_BASE ?? ''
}

/** 把后端返回的相对路径（如 /api/uploads/x.png）补成当前 backend 的绝对地址。 */
export function apiUrl(path: string): string {
  if (/^(?:https?:|data:|blob:)/i.test(path)) return path
  return `${apiBase()}${path}`
}
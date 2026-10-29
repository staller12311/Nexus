/**
 * 后端地址前缀。
 *
 * 默认空串 → 请求 `/api/xxx`，由 Vite proxy 转发（浏览器里的常规路径，
 * 业务代码里**不出现** http://127.0.0.1:8000 这样的硬编码）。
 * 只有集成测试等 Node 环境才用 `VITE_API_BASE` 指到真实后端。
 */
export function apiBase(): string {
  return import.meta.env.VITE_API_BASE ?? ''
}
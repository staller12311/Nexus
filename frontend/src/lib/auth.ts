/**
 * 访问令牌的存放与读取。
 *
 * 为什么放在 localStorage 而不是构建期注入：同一个桌面安装包可能连本机后端（默认只允许回环、
 * 无需令牌），也可能连一台开了 `server.auth_token` 的远程后端，令牌不该被烧进产物里。
 *
 * 优先级：localStorage > 构建期 `VITE_NEXUS_AUTH_TOKEN`。
 */

const STORAGE_KEY = 'nexus_auth_token'

/** 构建期兜底（自建分发版可以预置一个令牌） */
const BUILD_TOKEN = (import.meta.env.VITE_NEXUS_AUTH_TOKEN ?? '').trim()

export function getAuthToken(): string {
  try {
    return (window.localStorage.getItem(STORAGE_KEY) ?? '').trim() || BUILD_TOKEN
  } catch {
    // 隐私模式 / 无 localStorage 的环境：退化为构建期令牌
    return BUILD_TOKEN
  }
}

export function setAuthToken(token: string): void {
  try {
    const value = token.trim()
    if (value) window.localStorage.setItem(STORAGE_KEY, value)
    else window.localStorage.removeItem(STORAGE_KEY)
  } catch {
    /* 存不进去就算了，不影响本次请求 */
  }
}

export function clearAuthToken(): void {
  setAuthToken('')
}

/** 非空时返回 Authorization 头，否则返回空对象（便于直接展开进 headers）。 */
export function authHeaders(): Record<string, string> {
  const token = getAuthToken()
  return token ? { Authorization: `Bearer ${token}` } : {}
}

/**
 * 收到 401 时向用户要一次令牌。
 *
 * WebView 里 `window.prompt` 未必可用（Tauri/WebView2 可能直接抛错），
 * 所以整段包在 try/catch 里：拿不到就返回 null，由调用方抛出可读的错误信息。
 * 只问一次（用模块级标记），避免每个并发请求都弹窗。
 */
let prompted = false

export function askAuthTokenOnce(): string | null {
  if (prompted) return null
  prompted = true
  try {
    const input = window.prompt('后端已开启访问令牌，请输入（或取消）') ?? ''
    const token = input.trim()
    if (token) {
      setAuthToken(token)
      return token
    }
  } catch {
    /* prompt 不可用 */
  }
  return null
}

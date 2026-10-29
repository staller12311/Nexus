import { X } from 'lucide-react'
import { useChatStore } from '../store/chatStore'

/** 任何失败（SSE error / HTTP 非 2xx / 网络断）都在这里可见，不静默失败。 */
export function ErrorBanner() {
  const error = useChatStore((state) => state.error)
  const setError = useChatStore((state) => state.setError)
  if (!error) return null

  return (
    <div className="border-b border-rose-200 bg-rose-50 px-4 py-2">
      <div className="mx-auto flex w-full max-w-3xl items-start gap-2 text-sm text-rose-700">
        <span className="font-medium">出错了：</span>
        <span className="min-w-0 flex-1 break-words">{error}</span>
        <button
          type="button"
          onClick={() => setError(null)}
          className="shrink-0 rounded p-0.5 hover:bg-rose-100"
          aria-label="关闭错误提示"
        >
          <X className="h-4 w-4" />
        </button>
      </div>
    </div>
  )
}
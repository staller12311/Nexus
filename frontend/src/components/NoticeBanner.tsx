import { Check } from 'lucide-react'
import { useChatStore } from '../store/chatStore'

/** 轻量成功提示（比如「已更新，下一句对话生效」），受 store.notice 控制。 */
export function NoticeBanner() {
  const notice = useChatStore((state) => state.notice)
  const clearNotice = useChatStore((state) => state.clearNotice)
  if (!notice) return null

  return (
    <div className="border-b border-emerald-200 bg-emerald-50 px-4 py-1.5">
      <div className="mx-auto flex w-full max-w-3xl items-center gap-2 text-xs text-emerald-700">
        <Check className="h-3.5 w-3.5" />
        <span className="flex-1">{notice}</span>
        <button
          type="button"
          onClick={clearNotice}
          className="rounded px-1 text-emerald-600 hover:bg-emerald-100"
        >
          知道了
        </button>
      </div>
    </div>
  )
}
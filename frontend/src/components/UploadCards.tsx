import { CheckCircle2, FileText, Loader2, X, XCircle } from 'lucide-react'
import { useChatStore } from '../store/chatStore'
import type { UploadCard } from '../store/chatStore'

function label(card: UploadCard): string {
  if (card.status === 'uploading') return `正在上传文档 ${card.name}`
  if (card.status === 'parsing') return `正在解析 ${card.name}`
  if (card.status === 'ready') return '文档已加入知识库'
  return `上传失败：${card.error ?? card.name}`
}

/** 聊天流里的上传卡片：上传中显示进度条，解析完成后变成「文档已加入知识库」。 */
export function UploadCards() {
  const cards = useChatStore((state) => state.kbUploads)
  const dismiss = useChatStore((state) => state.dismissKbUpload)
  if (cards.length === 0) return null

  return (
    <div className="flex flex-col items-start gap-2" data-testid="kb-upload-cards">
      {cards.map((card) => {
        const busy = card.status === 'uploading' || card.status === 'parsing'
        return (
          <div
            key={card.id}
            data-testid={`kb-upload-${card.id}`}
            className={`w-full max-w-md rounded-xl border px-3 py-2 text-xs shadow-sm ${
              card.status === 'failed'
                ? 'border-rose-200 bg-rose-50 text-rose-700'
                : card.status === 'ready'
                  ? 'border-emerald-200 bg-emerald-50 text-emerald-700'
                  : 'border-slate-200 bg-white text-slate-600'
            }`}
          >
            <div className="flex items-center gap-2">
              {card.status === 'ready' ? (
                <CheckCircle2 className="h-4 w-4 shrink-0" />
              ) : card.status === 'failed' ? (
                <XCircle className="h-4 w-4 shrink-0" />
              ) : busy ? (
                <Loader2 className="h-4 w-4 shrink-0 animate-spin text-sky-500" />
              ) : (
                <FileText className="h-4 w-4 shrink-0" />
              )}
              <span className="min-w-0 flex-1 truncate font-medium">{label(card)}</span>
              {!busy && (
                <button
                  type="button"
                  onClick={() => dismiss(card.id)}
                  aria-label="关闭上传卡片"
                  className="shrink-0 rounded p-0.5 text-current opacity-60 hover:opacity-100"
                >
                  <X className="h-3.5 w-3.5" />
                </button>
              )}
            </div>

            {busy && (
              <div className="mt-1.5 h-1 w-full overflow-hidden rounded-full bg-slate-100">
                <div
                  className="h-full rounded-full bg-sky-500 transition-all"
                  style={{ width: `${card.status === 'parsing' ? 100 : card.percent}%` }}
                  data-testid={`kb-upload-progress-${card.id}`}
                />
              </div>
            )}
            {card.status === 'parsing' && (
              <div className="mt-1 text-[10px] text-slate-400">已上传，正在切片与建索引…</div>
            )}
          </div>
        )
      })}
    </div>
  )
}
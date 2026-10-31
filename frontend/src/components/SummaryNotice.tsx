import { useState } from 'react'
import { ChevronDown, ChevronRight, Info } from 'lucide-react'
import type { SummaryNotice } from '../types'

/** 极淡的灰色系统提示：默认收起，点击查看本次压缩得到的摘要。 */
export function SummaryNoticeCard({ notice }: { notice: SummaryNotice }) {
  const [open, setOpen] = useState(false)
  const hasSummary = notice.summary.trim().length > 0

  return (
    <div
      data-testid="summary-notice"
      className="mx-auto w-full max-w-3xl px-4 text-[11px] leading-5 text-slate-400"
    >
      <div className="flex items-start gap-1.5">
        <Info className="mt-0.5 h-3 w-3 shrink-0" />
        <span className="min-w-0 flex-1">
          [系统提示：{notice.content}]
          {hasSummary && (
            <button
              type="button"
              onClick={() => setOpen((value) => !value)}
              className="ml-1 inline-flex items-center gap-0.5 text-slate-500 underline decoration-slate-300 underline-offset-2 hover:text-slate-700"
              aria-expanded={open}
            >
              {open ? (
                <ChevronDown className="h-3 w-3" />
              ) : (
                <ChevronRight className="h-3 w-3" />
              )}
              {open ? '收起摘要' : '点击查看摘要'}
            </button>
          )}
        </span>
      </div>
      {open && hasSummary && (
        <pre
          data-testid="summary-notice-detail"
          className="mt-1 max-h-48 overflow-auto whitespace-pre-wrap break-words rounded border border-slate-200 bg-white/70 px-2 py-1.5 font-sans text-[11px] leading-5 text-slate-500"
        >
          {notice.summary}
        </pre>
      )}
    </div>
  )
}
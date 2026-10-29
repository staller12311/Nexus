import { useState } from 'react'
import { CheckCircle2, ChevronDown, ChevronRight, Loader2, Wrench, XCircle } from 'lucide-react'
import type { ToolCallView } from '../types'

/** 参数摘要：解析成对象后压成一行；解析失败就原样截断显示。 */
function summarize(raw: string, limit = 56): string {
  try {
    const parsed: unknown = JSON.parse(raw)
    const text = JSON.stringify(parsed)
    return text.length > limit ? `${text.slice(0, limit)}…` : text
  } catch {
    const text = raw.replace(/\s+/g, ' ').trim()
    return text.length > limit ? `${text.slice(0, limit)}…` : text
  }
}

/** 展开后的参数：JSON.parse 后 pretty print；失败显示原始字符串。 */
function prettyArguments(raw: string): { text: string; parsed: boolean } {
  try {
    const parsed: unknown = JSON.parse(raw)
    return { text: JSON.stringify(parsed, null, 2), parsed: true }
  } catch {
    return { text: raw, parsed: false }
  }
}

const STATUS = {
  pending: {
    ring: 'border-amber-200',
    badge: 'bg-amber-50 text-amber-700',
    label: '执行中',
    icon: <Loader2 className="h-3.5 w-3.5 animate-spin" />,
  },
  success: {
    ring: 'border-emerald-200',
    badge: 'bg-emerald-50 text-emerald-700',
    label: '成功',
    icon: <CheckCircle2 className="h-3.5 w-3.5" />,
  },
  error: {
    ring: 'border-rose-300',
    badge: 'bg-rose-50 text-rose-700',
    label: '失败',
    icon: <XCircle className="h-3.5 w-3.5" />,
  },
} as const

export function ToolCard({ call }: { call: ToolCallView }) {
  const [expanded, setExpanded] = useState(false)
  const style = STATUS[call.status]
  const args = prettyArguments(call.arguments)
  const summary = summarize(call.arguments)

  return (
    <div className={`my-2 overflow-hidden rounded-lg border bg-white ${style.ring}`}>
      <button
        type="button"
        onClick={() => setExpanded((value) => !value)}
        className="flex w-full items-center gap-2 px-3 py-2 text-left text-sm hover:bg-slate-50"
        aria-expanded={expanded}
        data-testid={`tool-card-${call.id}`}
      >
        {expanded ? (
          <ChevronDown className="h-3.5 w-3.5 shrink-0 text-slate-400" />
        ) : (
          <ChevronRight className="h-3.5 w-3.5 shrink-0 text-slate-400" />
        )}
        <Wrench className="h-3.5 w-3.5 shrink-0 text-slate-500" />
        <span className="shrink-0 font-medium text-slate-700">{call.name}</span>
        <span className="truncate font-mono text-xs text-slate-500">{summary}</span>
        <span
          className={`ml-auto flex shrink-0 items-center gap-1 rounded-full px-2 py-0.5 text-[11px] ${style.badge}`}
        >
          {style.icon}
          {style.label}
        </span>
      </button>

      {expanded && (
        <div className="border-t border-slate-100 px-3 py-2 text-xs">
          <div className="mb-1 font-medium text-slate-500">
            参数{args.parsed ? '' : '（原始字符串，JSON 解析失败）'}
          </div>
          <pre className="mb-3 max-h-56 overflow-auto whitespace-pre-wrap break-all rounded bg-slate-50 p-2 font-mono text-[11px] text-slate-700">
            {args.text}
          </pre>
          <div className="mb-1 font-medium text-slate-500">
            {call.status === 'error' ? '错误信息' : '返回结果'}
          </div>
          <pre
            className={`max-h-56 overflow-auto whitespace-pre-wrap break-all rounded p-2 font-mono text-[11px] ${
              call.status === 'error' ? 'bg-rose-50 text-rose-700' : 'bg-slate-50 text-slate-700'
            }`}
          >
            {call.status === 'pending' ? '（执行中…）' : (call.content ?? '（没有返回内容）')}
          </pre>
        </div>
      )}
    </div>
  )
}
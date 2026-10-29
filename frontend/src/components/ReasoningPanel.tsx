import { useEffect, useRef, useState } from 'react'
import { Brain, ChevronDown, ChevronRight } from 'lucide-react'

/**
 * 思维链面板：模型输出 reasoning_content 时显示在气泡顶部。
 * - 思考中：自动展开、标题「深度思考中…」、内容随增量实时滚动
 * - 思考结束：收起为「已深度思考（N 字）」，点击可展开回看
 */
export function ReasoningPanel({
  reasoning,
  streaming,
}: {
  reasoning: string
  streaming?: boolean
}) {
  const [open, setOpen] = useState(true)
  const bodyRef = useRef<HTMLDivElement | null>(null)
  const finishedOnce = useRef(false)

  // 思考结束的那一刻自动收起（用户之后仍可手动展开）
  useEffect(() => {
    if (!streaming && !finishedOnce.current) {
      finishedOnce.current = true
      setOpen((value) => (value ? false : value))
    }
  }, [streaming])

  // 思考中自动滚到底（用 scrollTop，兼容 jsdom 与所有浏览器）
  useEffect(() => {
    const element = bodyRef.current
    if (streaming && open && element) {
      element.scrollTop = element.scrollHeight
    }
  }, [reasoning, streaming, open])

  const title = streaming ? '深度思考中…' : `已深度思考（${reasoning.length} 字）`

  return (
    <div className="mb-1 overflow-hidden rounded-xl border border-slate-200 bg-slate-50">
      <button
        type="button"
        onClick={() => setOpen((value) => !value)}
        aria-expanded={open}
        aria-label={streaming ? '深度思考中' : '已深度思考'}
        className="flex w-full items-center gap-1.5 px-3 py-1.5 text-left text-[11px] text-slate-500 hover:bg-slate-100"
      >
        {open ? (
          <ChevronDown className="h-3 w-3 shrink-0" />
        ) : (
          <ChevronRight className="h-3 w-3 shrink-0" />
        )}
        <Brain className="h-3.5 w-3.5 shrink-0 text-slate-400" />
        <span className={streaming ? 'animate-pulse font-medium' : 'font-medium'}>{title}</span>
        {streaming && (
          <span className="ml-1 inline-flex gap-0.5">
            <span className="h-1 w-1 animate-bounce rounded-full bg-slate-400" />
            <span className="h-1 w-1 animate-bounce rounded-full bg-slate-400 [animation-delay:120ms]" />
            <span className="h-1 w-1 animate-bounce rounded-full bg-slate-400 [animation-delay:240ms]" />
          </span>
        )}
      </button>

      {open && (
        <div
          ref={bodyRef}
          className="max-h-48 overflow-y-auto whitespace-pre-wrap break-words border-t border-slate-200 px-3 py-2 font-mono text-[11px] leading-relaxed text-slate-500"
        >
          {reasoning || '（等待模型思考…）'}
        </div>
      )}
    </div>
  )
}
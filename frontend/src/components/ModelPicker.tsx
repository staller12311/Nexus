import { useEffect, useMemo, useRef, useState } from 'react'
import { Check, Search } from 'lucide-react'
import { filterModels } from '../lib/modelFilter'

type Props = {
  models: string[]
  value: string
  onChange: (modelId: string) => void
  disabled?: boolean
}

/**
 * 可搜索的模型选择器（自己实现，不引 react-select）。
 *
 * - 输入框实时过滤（子串、大小写不敏感）
 * - 自动隐藏 audio / image / tts / vision / realtime / embedding / speech / ocr 这类非纯文本模型
 * - 主流模型（qwen-max / qwen-plus / deepseek / gpt / kimi …）排在最前
 * - ↑/↓ 移动、Enter 选中、Esc 收起；也可以直接点
 */
export function ModelPicker({ models, value, onChange, disabled = false }: Props) {
  const [query, setQuery] = useState('')
  const [open, setOpen] = useState(false)
  const [activeIndex, setActiveIndex] = useState(0)
  const boxRef = useRef<HTMLDivElement | null>(null)

  const { visible, hiddenCount } = useMemo(() => filterModels(models, query), [models, query])

  useEffect(() => {
    setActiveIndex(0)
  }, [query])

  // 点外部收起
  useEffect(() => {
    if (!open) return
    const onPointerDown = (event: MouseEvent) => {
      if (boxRef.current && !boxRef.current.contains(event.target as Node)) setOpen(false)
    }
    document.addEventListener('mousedown', onPointerDown)
    return () => document.removeEventListener('mousedown', onPointerDown)
  }, [open])

  const pick = (modelId: string) => {
    onChange(modelId)
    setQuery('')
    setOpen(false)
  }

  const onKeyDown = (event: React.KeyboardEvent<HTMLInputElement>) => {
    if (event.key === 'ArrowDown') {
      event.preventDefault()
      setOpen(true)
      setActiveIndex((index) => Math.min(index + 1, Math.max(visible.length - 1, 0)))
    } else if (event.key === 'ArrowUp') {
      event.preventDefault()
      setActiveIndex((index) => Math.max(index - 1, 0))
    } else if (event.key === 'Enter') {
      if (open && visible[activeIndex]) {
        event.preventDefault()
        pick(visible[activeIndex])
      }
    } else if (event.key === 'Escape') {
      setOpen(false)
      setQuery('')
    }
  }

  return (
    <div className="min-w-0 flex-1" ref={boxRef}>
      <div className="relative">
        <Search className="pointer-events-none absolute left-2 top-2 h-3.5 w-3.5 text-slate-400" />
        <input
          id="model-search"
          name="model_search"
          // 输入框**只承载搜索词**：不再显示已选模型，避免"看起来像自动填了一个值"
          value={query}
          onChange={(event) => {
            setQuery(event.target.value)
            setOpen(true)
          }}
          onFocus={() => setOpen(true)}
          onKeyDown={onKeyDown}
          disabled={disabled}
          placeholder={value ? `已选：${value}` : '请选择模型…'}
          aria-label="搜索模型"
          aria-expanded={open}
          role="combobox"
          className="w-full rounded-lg border border-slate-200 py-1.5 pl-7 pr-2 text-sm outline-none focus:border-sky-400"
        />
      </div>

      {!open && value && (
        <div className="mt-1 truncate font-mono text-[11px] text-slate-500">已选：{value}</div>
      )}
      {!open && !value && (
        <div className="mt-1 text-[11px] text-slate-400">点这里搜索并选择模型</div>
      )}

      {open && (
        <div className="mt-1 overflow-hidden rounded-lg border border-slate-200 bg-white shadow-sm">
          <div className="flex items-center justify-between border-b border-slate-100 px-2 py-1 text-[10px] text-slate-400">
            <span>
              {visible.length} 个可对话模型
              {query && ` · 匹配「${query}」`}
            </span>
            {hiddenCount > 0 && <span>已隐藏 {hiddenCount} 个音频/图像/向量等模型</span>}
          </div>
          <ul role="listbox" className="max-h-52 overflow-y-auto py-1">
            {visible.length === 0 && (
              <li className="px-2 py-2 text-xs text-slate-400">没有匹配的模型</li>
            )}
            {visible.map((id, index) => {
              const selected = id === value
              const active = index === activeIndex
              return (
                <li key={id}>
                  <button
                    type="button"
                    role="option"
                    aria-selected={selected}
                    onMouseEnter={() => setActiveIndex(index)}
                    onClick={() => pick(id)}
                    className={`flex w-full items-center gap-1 px-2 py-1 text-left font-mono text-[11px] ${
                      active ? 'bg-sky-50 text-sky-800' : 'text-slate-600 hover:bg-slate-50'
                    }`}
                  >
                    {selected ? (
                      <Check className="h-3 w-3 shrink-0 text-emerald-600" />
                    ) : (
                      <span className="h-3 w-3 shrink-0" />
                    )}
                    <span className="truncate">{id}</span>
                  </button>
                </li>
              )
            })}
          </ul>
        </div>
      )}
    </div>
  )
}
import { useEffect, useRef, useState } from 'react'
import { ChevronDown, Plus } from 'lucide-react'
import type { ModelInfo } from '../types'

type Props = {
  models: ModelInfo[]
  /** 当前选中的模型名（store 里的 model） */
  value: string | null
  onChange: (name: string) => void
  /** 点「添加 / 管理模型」：打开模型管理弹窗 */
  onManage: () => void
  disabled?: boolean
}

/**
 * 输入框右下角的极简模型选择器（对齐 Cursor 的样式）。
 *
 * - 收起态是一个小号灰字按钮 + 下拉箭头，不抢输入框的视觉重心
 * - 展开态**向上弹出**（`bottom-full` + `z-50`），避免被底部状态栏/屏幕边缘裁切
 * - 支持点外部关闭、Esc 关闭；正在生成时禁用切换
 * - 菜单底部是「添加 / 管理模型」，不额外占一行工具按钮
 */
export function ModelMenu({ models, value, onChange, onManage, disabled = false }: Props) {
  const [open, setOpen] = useState(false)
  const boxRef = useRef<HTMLDivElement | null>(null)

  // 点外部关闭；Esc 关闭
  useEffect(() => {
    if (!open) return
    const onPointerDown = (event: MouseEvent) => {
      if (boxRef.current && !boxRef.current.contains(event.target as Node)) setOpen(false)
    }
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setOpen(false)
    }
    document.addEventListener('mousedown', onPointerDown)
    document.addEventListener('keydown', onKeyDown)
    return () => {
      document.removeEventListener('mousedown', onPointerDown)
      document.removeEventListener('keydown', onKeyDown)
    }
  }, [open])

  // 开始生成时收起菜单，避免残留在画面上
  useEffect(() => {
    if (disabled) setOpen(false)
  }, [disabled])

  const current = models.find((item) => item.name === value)
  const label = current?.name ?? (models[0]?.name ?? '选择模型')

  return (
    <div className="relative" ref={boxRef}>
      <button
        type="button"
        onClick={() => setOpen((previous) => !previous)}
        disabled={disabled}
        aria-label="选择模型"
        aria-expanded={open}
        aria-haspopup="listbox"
        title={disabled ? 'AI 正在回答，稍后可切换模型' : `当前模型：${label}`}
        className="flex max-w-[9.5rem] items-center gap-1 rounded-md px-1.5 py-1 text-[11px] text-slate-500 transition-colors hover:bg-slate-100 hover:text-slate-800 disabled:cursor-not-allowed disabled:opacity-40"
      >
        <span className="truncate">{label}</span>
        <ChevronDown
          className={`h-3 w-3 shrink-0 text-slate-400 transition-transform ${open ? 'rotate-180' : ''}`}
        />
      </button>

      {open && (
        <div className="absolute bottom-full right-0 z-50 mb-2 w-64 overflow-hidden rounded-xl border border-slate-200 bg-white shadow-xl shadow-slate-900/10">
          <ul role="listbox" aria-label="可用模型" className="max-h-64 overflow-y-auto py-1">
            {models.length === 0 && (
              <li className="px-3 py-2 text-xs text-slate-400">还没有可用模型</li>
            )}
            {models.map((item) => {
              const selected = item.name === value
              return (
                <li key={item.name}>
                  <button
                    type="button"
                    role="option"
                    aria-selected={selected}
                    onClick={() => {
                      onChange(item.name)
                      setOpen(false)
                    }}
                    className={`flex w-full items-center justify-between gap-2 px-3 py-1.5 text-left text-xs transition-colors ${
                      selected
                        ? 'bg-slate-100 font-medium text-slate-900'
                        : 'text-slate-600 hover:bg-slate-50'
                    }`}
                  >
                    <span className="truncate">
                      {item.name}
                      {item.is_default ? ' · 默认' : ''}
                    </span>
                    <span className="shrink-0 text-[10px] font-normal text-slate-400">
                      {item.is_custom ? '自定义' : ''}
                      {item.is_custom && !item.has_key ? ' · ' : ''}
                      {item.has_key ? '' : '未配置密钥'}
                    </span>
                  </button>
                </li>
              )
            })}
          </ul>
          <div className="border-t border-slate-100 p-1">
            <button
              type="button"
              onClick={() => {
                setOpen(false)
                onManage()
              }}
              className="flex w-full items-center gap-1.5 rounded-lg px-2 py-1.5 text-left text-xs text-slate-500 transition-colors hover:bg-slate-50 hover:text-slate-800"
            >
              <Plus className="h-3.5 w-3.5" />
              添加 / 管理模型
            </button>
          </div>
        </div>
      )}
    </div>
  )
}
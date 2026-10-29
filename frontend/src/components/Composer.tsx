import { Paperclip, SendHorizonal, ShieldCheck, Square } from 'lucide-react'
import { WorkspaceDialog } from './WorkspaceDialog'
import { useEffect, useRef, useState } from 'react'
import type { KeyboardEvent } from 'react'
import { useChatStore } from '../store/chatStore'

/** 沙箱说明文案（徽章 + Tooltip 共用，也是测试断言的目标） */
const SANDBOX_HINT =
  '为保护您的数据安全，AI 文件读写工具已被严格限制在当前的 workspace 目录内，无法越权访问系统文件。'

export function Composer() {
  const draft = useChatStore((state) => state.draft)
  const setDraft = useChatStore((state) => state.setDraft)
  const streaming = useChatStore((state) => state.streaming)
  const sendMessage = useChatStore((state) => state.sendMessage)
  const stopStreaming = useChatStore((state) => state.stopStreaming)
  const focusSignal = useChatStore((state) => state.draftFocusSignal)
  const kbStatus = useChatStore((state) => state.kbStatus)
  const uploadKbDocs = useChatStore((state) => state.uploadKbDocs)
  const workspace = useChatStore((state) => state.workspace)
  const loadWorkspace = useChatStore((state) => state.loadWorkspace)
  const [workspaceOpen, setWorkspaceOpen] = useState(false)
  const textareaRef = useRef<HTMLTextAreaElement | null>(null)
  const fileRef = useRef<HTMLInputElement | null>(null)

  // 支持格式由后端 /api/kb/status 给出（缺 pypdf/python-docx 时就没有 .pdf/.docx）
  const accept = kbStatus?.supported?.length
    ? kbStatus.supported.join(',')
    : '.pdf,.docx,.txt,.md,.csv,.json,.log'

  // 徽章上要显示当前工作区；首次挂载拉一次
  useEffect(() => {
    void loadWorkspace()
  }, [loadWorkspace])

  // 点了消息上的「编辑」：把焦点移回输入框，光标放到末尾
  useEffect(() => {
    if (focusSignal === 0) return
    const element = textareaRef.current
    if (!element) return
    element.focus()
    const end = element.value.length
    element.setSelectionRange(end, end)
  }, [focusSignal])

  const submit = () => {
    if (streaming || draft.trim().length === 0) return
    void sendMessage(draft)
  }

  const onKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key === 'Enter' && !event.shiftKey) {
      event.preventDefault()
      submit()
    }
  }

  return (
    <div className="border-t border-slate-200 bg-white/80 backdrop-blur">
      <div className="mx-auto w-full max-w-3xl px-4 py-3">
        <div className="flex items-end gap-2 rounded-xl border border-slate-200 bg-white p-2 shadow-sm focus-within:border-sky-400">
          <button
            type="button"
            onClick={() => fileRef.current?.click()}
            aria-label="上传文档到知识库"
            title="上传文档到知识库（PDF / Word / 文本）"
            className="mb-0.5 shrink-0 rounded-lg p-2 text-slate-400 hover:bg-slate-100 hover:text-sky-600"
          >
            <Paperclip className="h-4 w-4" />
          </button>

          {/* 沙箱徽章：点击可切换工作区（校验在后端，见 README 第 58 条） */}
          <button
            type="button"
            data-testid="sandbox-badge"
            onClick={() => setWorkspaceOpen(true)}
            title={`${SANDBOX_HINT}（点击切换工作区）`}
            aria-label={`安全沙箱保护中。点击切换工作区。${SANDBOX_HINT}`}
            className="group relative mb-0.5 hidden shrink-0 cursor-pointer items-center gap-1 rounded-full bg-emerald-50 px-2 py-1 text-[10px] font-medium text-emerald-700 ring-1 ring-emerald-100 hover:bg-emerald-100 sm:flex"
          >
            <ShieldCheck className="h-3 w-3" />
            安全沙箱保护中
            {/* 悬浮提示（原生 title 之外再给一个样式化的 Tooltip） */}
            <span
              role="tooltip"
              className="pointer-events-none absolute bottom-full left-0 z-20 mb-2 w-64 rounded-lg bg-slate-800 px-2.5 py-1.5 text-[11px] font-normal leading-relaxed text-white opacity-0 shadow-lg transition-opacity duration-150 group-hover:opacity-100"
            >
              {SANDBOX_HINT}
              {workspace && (
                <span className="mt-1 block font-mono text-[10px] text-slate-300">
                  当前工作区：{workspace}
                </span>
              )}
            </span>
            {workspace && (
              <span className="hidden max-w-[7rem] truncate font-mono text-[10px] opacity-70 lg:inline">
                {workspace}
              </span>
            )}
          </button>
          <input
            ref={fileRef}
            type="file"
            multiple
            accept={accept}
            aria-label="选择要上传的文档"
            className="hidden"
            onChange={(event) => {
              const files = Array.from(event.target.files ?? [])
              if (files.length > 0) void uploadKbDocs(files)
              event.target.value = ''
            }}
          />
          <textarea
            ref={textareaRef}
            value={draft}
            onChange={(event) => setDraft(event.target.value)}
            onKeyDown={onKeyDown}
            disabled={streaming}
            rows={2}
            placeholder={streaming ? 'AI 正在回答……' : '输入问题，Enter 发送，Shift+Enter 换行'}
            className="max-h-40 min-h-[44px] flex-1 resize-none bg-transparent px-2 py-1 text-sm text-slate-800 outline-none disabled:cursor-not-allowed disabled:text-slate-400"
          />
          {streaming ? (
            <button
              type="button"
              onClick={stopStreaming}
              aria-label="停止生成"
              title="停止生成（已生成的内容会保留）"
              className="mb-0.5 flex items-center gap-1 rounded-lg bg-rose-600 px-3 py-2 text-sm font-medium text-white hover:bg-rose-700"
            >
              <Square className="h-3.5 w-3.5 fill-current" /> 停止
            </button>
          ) : (
            <button
              type="button"
              onClick={submit}
              disabled={draft.trim().length === 0}
              className="mb-0.5 flex items-center gap-1 rounded-lg bg-sky-600 px-3 py-2 text-sm font-medium text-white disabled:cursor-not-allowed disabled:bg-slate-300"
            >
              <SendHorizonal className="h-4 w-4" /> 发送
            </button>
          )}
        </div>
        <WorkspaceDialog open={workspaceOpen} onClose={() => setWorkspaceOpen(false)} />

        <div className="mt-1 flex justify-between px-1 text-[11px] text-slate-400">
          <span>{streaming ? 'AI 正在回答……' : 'Enter 发送 · Shift+Enter 换行'}</span>
          <span>内容由模型生成，请自行核对</span>
        </div>
      </div>
    </div>
  )
}
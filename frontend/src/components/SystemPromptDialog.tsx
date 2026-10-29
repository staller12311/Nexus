import { useEffect, useState } from 'react'
import { Settings, X } from 'lucide-react'
import { useChatStore } from '../store/chatStore'

/** 系统提示词设置弹窗：改完保存 → PATCH → 下一句对话立即生效。 */
export function SystemPromptDialog({ open, onClose }: { open: boolean; onClose: () => void }) {
  const sessionId = useChatStore((state) => state.sessionId)
  const systemPrompt = useChatStore((state) => state.systemPrompt)
  const saveSystemPrompt = useChatStore((state) => state.saveSystemPrompt)
  const [draft, setDraft] = useState(systemPrompt)
  const [saving, setSaving] = useState(false)

  // 每次打开都用当前会话的提示词重新初始化
  useEffect(() => {
    if (open) setDraft(systemPrompt)
  }, [open, systemPrompt])

  useEffect(() => {
    if (!open) return
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [open, onClose])

  if (!open) return null

  const onSave = async () => {
    setSaving(true)
    const ok = await saveSystemPrompt(draft)
    setSaving(false)
    if (ok) onClose()
  }

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-slate-900/40 p-4"
      onClick={onClose}
      role="presentation"
    >
      <div
        className="w-full max-w-lg rounded-xl bg-white p-5 shadow-xl"
        onClick={(event) => event.stopPropagation()}
        role="dialog"
        aria-modal="true"
        aria-label="系统提示词设置"
      >
        <div className="mb-1 flex items-center gap-2">
          <Settings className="h-4 w-4 text-slate-500" />
          <h2 className="text-sm font-semibold text-slate-800">系统提示词</h2>
          <button
            type="button"
            onClick={onClose}
            className="ml-auto rounded p-1 text-slate-400 hover:bg-slate-100"
            aria-label="关闭"
          >
            <X className="h-4 w-4" />
          </button>
        </div>
        <p className="mb-3 text-[11px] text-slate-500">
          {sessionId ? (
            <>
              当前会话：<span className="font-mono">{sessionId.slice(0, 8)}</span>。保存后
              <b>下一句对话立即生效</b>，无需新建会话。
            </>
          ) : (
            <>
              还没有打开的会话：保存时会<b>自动新建一个会话</b>并应用这条提示词。
            </>
          )}
        </p>

        <textarea
          id="system-prompt"
          name="system_prompt"
          value={draft}
          onChange={(event) => setDraft(event.target.value)}
          rows={6}
          autoFocus
          placeholder="例如：你是一个只讲冷笑话的助手。留空并保存 = 清空当前提示词。"
          className="w-full resize-y rounded-lg border border-slate-200 p-3 text-sm text-slate-800 outline-none focus:border-sky-400"
        />

        <div className="mt-2 text-[11px] text-slate-400">
          留空并保存 = 清空当前会话的提示词；新会话若从未设置过，则使用 `config.yaml` 里的默认提示词
          （默认提示词已要求「优先用 search_knowledge_base 检索上传的文档」，覆盖后请自行保留这条）。
        </div>

        <div className="mt-4 flex justify-end gap-2">
          <button
            type="button"
            onClick={onClose}
            className="rounded-lg border border-slate-200 px-3 py-1.5 text-sm text-slate-600 hover:bg-slate-50"
          >
            取消
          </button>
          <button
            type="button"
            onClick={() => void onSave()}
            disabled={saving}
            className="rounded-lg bg-sky-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-sky-700 disabled:bg-slate-300"
          >
            {saving ? '保存中…' : '保存'}
          </button>
        </div>
      </div>
    </div>
  )
}
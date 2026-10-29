import { useState } from 'react'
import { Check, Pencil, Trash2, X } from 'lucide-react'
import { useChatStore } from '../store/chatStore'
import type { SessionInfo } from '../types'

type Props = {
  sessions: SessionInfo[]
  sessionId: string | null
  onOpen: (id: string) => void
  displayTitle: (session: SessionInfo) => string
  formatTime: (iso: string) => string
}

/**
 * 历史会话列表：悬浮显示「重命名 / 删除」。
 * - 重命名：就地变输入框，Enter / 失焦保存，Esc 取消
 * - 删除：先弹二次确认，确认后才 DELETE
 */
export function SessionList({ sessions, sessionId, onOpen, displayTitle, formatTime }: Props) {
  const renameSession = useChatStore((state) => state.renameSession)
  const deleteSession = useChatStore((state) => state.deleteSession)
  const streaming = useChatStore((state) => state.streaming)

  const [editingId, setEditingId] = useState<string | null>(null)
  const [draft, setDraft] = useState('')
  const [pendingDelete, setPendingDelete] = useState<SessionInfo | null>(null)

  const startRename = (session: SessionInfo) => {
    setEditingId(session.id)
    setDraft(session.title || displayTitle(session))
  }

  const commitRename = async (id: string, value: string) => {
    setEditingId(null)
    const next = value.trim()
    if (next) await renameSession(id, next)
  }

  return (
    <>
      <div className="min-h-0 flex-1 overflow-y-auto px-2 pb-3">
        {sessions.length === 0 && (
          <div className="px-2 py-3 text-xs text-gray-400">还没有历史会话</div>
        )}

        {sessions.map((session) => {
          const active = session.id === sessionId
          const editing = editingId === session.id

          return (
            <div key={session.id} className="group relative mb-1">
              {editing ? (
                <div className="flex items-center gap-1 rounded-md border border-gray-300 bg-white px-1.5 py-1.5">
                  <input
                    id={`rename-${session.id}`}
                    name={`rename-${session.id}`}
                    autoFocus
                    value={draft}
                    onChange={(event) => setDraft(event.target.value)}
                    onKeyDown={(event) => {
                      if (event.key === 'Enter') void commitRename(session.id, draft)
                      if (event.key === 'Escape') setEditingId(null)
                    }}
                    onBlur={() => void commitRename(session.id, draft)}
                    aria-label="会话标题"
                    className="min-w-0 flex-1 bg-transparent text-xs text-gray-800 outline-none"
                  />
                  <button
                    type="button"
                    onMouseDown={(event) => event.preventDefault()}
                    onClick={() => void commitRename(session.id, draft)}
                    aria-label="保存标题"
                    className="rounded p-0.5 text-gray-700 hover:bg-gray-100"
                  >
                    <Check className="h-3.5 w-3.5" />
                  </button>
                  <button
                    type="button"
                    onMouseDown={(event) => event.preventDefault()}
                    onClick={() => setEditingId(null)}
                    aria-label="取消重命名"
                    className="rounded p-0.5 text-gray-400 hover:bg-gray-100"
                  >
                    <X className="h-3.5 w-3.5" />
                  </button>
                </div>
              ) : (
                <button
                  type="button"
                  onClick={() => onOpen(session.id)}
                  aria-current={active ? 'true' : undefined}
                  title={session.id}
                  className={`block w-full rounded-md border py-2 pl-2 pr-14 text-left text-xs transition ${
                    active
                      ? 'border-gray-200 bg-gray-100 shadow-[inset_2px_0_0_0_#111827]'
                      : 'border-transparent hover:bg-gray-100'
                  }`}
                >
                  <div
                    className={`truncate font-medium ${active ? 'text-gray-900' : 'text-gray-800'}`}
                  >
                    {displayTitle(session)}
                  </div>
                  <div className="mt-0.5 truncate text-[11px] text-gray-400">
                    {formatTime(session.updated_at)} · {session.message_count} 条 ·{' '}
                    {session.total_tokens} tokens
                  </div>
                </button>
              )}

              {!editing && (
                <div className="absolute right-1 top-1.5 flex gap-0.5 opacity-0 transition group-hover:opacity-100 group-focus-within:opacity-100">
                  <button
                    type="button"
                    onClick={(event) => {
                      event.stopPropagation()
                      startRename(session)
                    }}
                    aria-label={`重命名 ${displayTitle(session)}`}
                    title="重命名"
                    className="rounded bg-white/90 p-1 text-gray-400 shadow-sm hover:text-gray-900"
                  >
                    <Pencil className="h-3.5 w-3.5" />
                  </button>
                  <button
                    type="button"
                    onClick={(event) => {
                      event.stopPropagation()
                      setPendingDelete(session)
                    }}
                    aria-label={`删除 ${displayTitle(session)}`}
                    title="删除"
                    className="rounded bg-white/90 p-1 text-gray-400 shadow-sm hover:text-rose-600"
                  >
                    <Trash2 className="h-3.5 w-3.5" />
                  </button>
                </div>
              )}
            </div>
          )
        })}
      </div>

      {pendingDelete && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-gray-900/40 p-4"
          onClick={() => setPendingDelete(null)}
          role="presentation"
        >
          <div
            className="w-full max-w-sm rounded-xl bg-white p-5 shadow-xl"
            onClick={(event) => event.stopPropagation()}
            role="dialog"
            aria-modal="true"
            aria-label="确认删除会话"
          >
            <h2 className="mb-2 text-sm font-semibold text-gray-900">删除这个会话？</h2>
            <p className="mb-1 text-xs text-gray-500">
              「{displayTitle(pendingDelete)}」及其 {pendingDelete.message_count} 条消息会被一起删除，
              不可恢复。
            </p>
            {streaming && (
              <p className="mb-1 text-xs text-gray-500">AI 正在回答，删除可能丢失这一轮结果。</p>
            )}
            <div className="mt-4 flex justify-end gap-2">
              <button
                type="button"
                onClick={() => setPendingDelete(null)}
                className="rounded-lg border border-gray-200 px-3 py-1.5 text-sm text-gray-600 hover:bg-gray-50"
              >
                取消
              </button>
              <button
                type="button"
                onClick={async () => {
                  const target = pendingDelete
                  setPendingDelete(null)
                  await deleteSession(target.id)
                }}
                className="rounded-lg bg-rose-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-rose-700"
              >
                删除
              </button>
            </div>
          </div>
        </div>
      )}
    </>
  )
}
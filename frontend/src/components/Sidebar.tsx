import { useState } from 'react'
import { Box, Plus, Settings } from 'lucide-react'
import { useChatStore } from '../store/chatStore'
import { displayTitle, formatRelativeTime } from '../lib/session'
import { SystemPromptDialog } from './SystemPromptDialog'
import { KnowledgePanel } from './KnowledgePanel'
import { SessionList } from './SessionList'

/**
 * 极简侧边栏：只剩「品牌 / 新建会话 / 历史会话（+ 知识库）」。
 * 模型选择已迁移到输入框右下角（ModelMenu），这里不再出现任何蓝色。
 * 配色纪律：gray-50 / gray-100 / gray-200 / gray-800 / gray-900。
 */
export function Sidebar() {
  const sessions = useChatStore((state) => state.sessions)
  const sessionId = useChatStore((state) => state.sessionId)
  const startNewSession = useChatStore((state) => state.startNewSession)
  const openSession = useChatStore((state) => state.openSession)
  const streaming = useChatStore((state) => state.streaming)
  const [promptOpen, setPromptOpen] = useState(false)

  return (
    <aside className="hidden w-[268px] shrink-0 flex-col border-r border-gray-200 bg-gray-50 md:flex">
      <div className="flex items-center gap-2.5 px-4 pb-3.5 pt-4">
        {/* 极简线条 Logo：白底细边框 + 深灰图标，不使用渐变/品牌蓝 */}
        <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg border border-gray-300 bg-white">
          <Box className="h-4 w-4 text-gray-900" strokeWidth={1.75} />
        </div>
        <div className="min-w-0">
          <div className="flex items-center gap-1.5">
            <span className="truncate text-sm font-bold tracking-tight text-gray-900">
              Nexus 智枢
            </span>
            <span className="shrink-0 rounded bg-gray-900 px-1 py-0.5 text-[10px] font-medium leading-none text-white">
              PRO
            </span>
          </div>
          <div className="truncate text-[11px] text-gray-400">你的私有化 AI 调度中枢</div>
        </div>
        <button
          type="button"
          onClick={() => setPromptOpen(true)}
          title={sessionId ? '系统提示词设置' : '系统提示词设置（还没有会话，保存时会自动新建）'}
          aria-label="设置"
          className="ml-auto rounded p-1.5 text-gray-400 transition-colors hover:bg-gray-100 hover:text-gray-700"
        >
          <Settings className="h-4 w-4" strokeWidth={1.75} />
        </button>
      </div>

      <SystemPromptDialog open={promptOpen} onClose={() => setPromptOpen(false)} />

      <div className="px-4 pb-3">
        <button
          type="button"
          onClick={() => void startNewSession()}
          disabled={streaming}
          className="flex w-full items-center justify-center gap-1.5 rounded-lg border border-gray-200 bg-white px-3 py-2 text-sm font-medium text-gray-700 transition-colors hover:bg-gray-50 disabled:cursor-not-allowed disabled:text-gray-300"
        >
          <Plus className="h-4 w-4" strokeWidth={1.5} /> 新建会话
        </button>
      </div>

      <div className="px-4 pb-1 text-[11px] font-medium tracking-wide text-gray-400">
        历史会话
      </div>
      <SessionList
        sessions={sessions}
        sessionId={sessionId}
        onOpen={(id) => void openSession(id)}
        displayTitle={displayTitle}
        formatTime={formatRelativeTime}
      />

      <KnowledgePanel />
    </aside>
  )
}
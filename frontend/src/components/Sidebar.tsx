import { useState } from 'react'
import { Boxes, Plus, PlusCircle, Settings } from 'lucide-react'
import { useChatStore } from '../store/chatStore'
import { displayTitle, formatRelativeTime } from '../lib/session'
import { SystemPromptDialog } from './SystemPromptDialog'
import { ModelManagerDialog } from './ModelManagerDialog'
import { KnowledgePanel } from './KnowledgePanel'
import { SessionList } from './SessionList'

export function Sidebar() {
  const models = useChatStore((state) => state.models)
  const model = useChatStore((state) => state.model)
  const setModel = useChatStore((state) => state.setModel)
  const sessions = useChatStore((state) => state.sessions)
  const sessionId = useChatStore((state) => state.sessionId)
  const startNewSession = useChatStore((state) => state.startNewSession)
  const openSession = useChatStore((state) => state.openSession)
  const streaming = useChatStore((state) => state.streaming)
  const [promptOpen, setPromptOpen] = useState(false)
  const [modelsOpen, setModelsOpen] = useState(false)

  return (
    <aside className="hidden w-[268px] shrink-0 flex-col border-r border-slate-200 bg-white md:flex">
      <div className="flex items-center gap-2.5 px-4 py-4">
        {/* Logo：渐变蓝底 + 方块网络图标，标题「Nexus 智枢」 */}
        <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-xl bg-gradient-to-br from-sky-500 via-blue-600 to-indigo-600 shadow-sm shadow-blue-500/30">
          <Boxes className="h-[18px] w-[18px] text-white" />
        </div>
        <div className="min-w-0">
          <div className="truncate text-sm font-semibold tracking-tight text-slate-800">
            Nexus <span className="text-sky-600">智枢</span>
          </div>
          <div className="truncate text-[11px] text-slate-400">你的私有化 AI 调度中枢</div>
        </div>
        <button
          type="button"
          onClick={() => setPromptOpen(true)}
          title={sessionId ? '系统提示词设置' : '系统提示词设置（还没有会话，保存时会自动新建）'}
          aria-label="设置"
          className="ml-auto rounded p-1.5 text-slate-400 hover:bg-slate-100 hover:text-slate-600"
        >
          <Settings className="h-4 w-4" />
        </button>
      </div>

      <SystemPromptDialog open={promptOpen} onClose={() => setPromptOpen(false)} />
      <ModelManagerDialog open={modelsOpen} onClose={() => setModelsOpen(false)} />

      <div className="px-4 pb-3">
        <div className="mb-1 flex items-center justify-between">
          <label className="block text-[11px] font-medium text-slate-500" htmlFor="model-select">
            模型
          </label>
          <button
            type="button"
            onClick={() => setModelsOpen(true)}
            title={streaming ? 'AI 正在回答，请稍后管理模型' : '添加 / 管理模型'}
            aria-label="添加模型"
            className="rounded p-1 text-slate-400 hover:bg-slate-100 hover:text-sky-600"
          >
            <PlusCircle className="h-3.5 w-3.5" />
          </button>
        </div>
        <select
          id="model-select"
          className="w-full rounded-md border border-slate-200 bg-white px-2 py-1.5 text-sm text-slate-700 focus:border-sky-400 focus:outline-none disabled:cursor-not-allowed disabled:bg-slate-50 disabled:text-slate-400"
          value={model ?? ''}
          onChange={(event) => setModel(event.target.value)}
          disabled={streaming}
          title={streaming ? 'AI 正在回答，稍后可切换模型' : undefined}
        >
          {models.length === 0 && <option value="">（未加载到模型）</option>}
          {models.map((item) => (
            <option key={item.name} value={item.name}>
              {item.name}
              {item.is_default ? '（默认）' : ''}
              {item.is_custom ? ' · 自定义' : ''}
              {item.has_key ? '' : ' · 未配置密钥'}
            </option>
          ))}
        </select>
        {streaming && (
          <div className="mt-1 text-[10px] text-amber-600">正在回答，暂不能切换模型</div>
        )}
      </div>

      <div className="px-4 pb-3">
        <button
          type="button"
          onClick={() => void startNewSession()}
          disabled={streaming}
          className="flex w-full items-center justify-center gap-1 rounded-md bg-sky-600 px-3 py-2 text-sm font-medium text-white hover:bg-sky-700 disabled:cursor-not-allowed disabled:bg-slate-300"
        >
          <Plus className="h-4 w-4" /> 新建会话
        </button>
      </div>

      <div className="px-4 pb-1 text-[11px] font-medium text-slate-500">历史会话</div>
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
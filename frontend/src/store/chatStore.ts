import { create } from 'zustand'
import { api } from '../api/client'
import { apiBase } from '../lib/base'
import { reconstructMessages } from '../lib/reconstruct'
import { streamChat, streamRegenerate, type ServerEvent } from '../lib/sse'
import type {
  ChatMessage,
  CustomModelPayload,
  KnowledgeDoc,
  KnowledgeStatus,
  ModelInfo,
  SessionInfo,
  ToolCallStatus,
  Usage,
  UsageStats,
  WorkspaceExtra,
} from '../types'

let counter = 0
/** 正在进行的这一轮流式请求的中断句柄（不在响应式 state 里，避免无谓重渲染） */
let activeAbort: AbortController | null = null

function isAbortError(error: unknown): boolean {
  return error instanceof DOMException
    ? error.name === 'AbortError'
    : error instanceof Error && error.name === 'AbortError'
}

/** 把后端 SSE 事件翻译成 store 变更；chat 与 regenerate 共用。 */
function applyEvent(
  get: () => ChatState,
  event: ServerEvent,
): void {
  switch (event.type) {
    case 'message_start':
      get().beginMessage()
      break
    case 'tool_call_start':
      get().startToolCall(
        asString(event.tool_call_id),
        asString(event.name),
        asString(event.arguments, '{}'),
      )
      break
    case 'tool_call_end':
      get().endToolCall(
        asString(event.tool_call_id),
        event.ok === true,
        asString(event.content),
      )
      break
    case 'approval_required':
      // 危险工具挂起：把审批信息挂到对应工具卡片上，等待用户点同意/拒绝
      get().markApprovalRequired(
        asString(event.tool_call_id),
        asString(event.approval_id),
        typeof event.expires_in === 'number' ? event.expires_in : undefined,
      )
      break
    case 'text_delta':
      get().appendDelta(asString(event.delta))
      break
    case 'reasoning_delta':
      get().appendReasoning(asString(event.text))
      break
    case 'message_end':
      get().finishMessage(parseUsage(event.usage))
      break
    case 'error':
      get().setError(asString(event.message, '未知错误'))
      break
    default:
      break
  }
}

const nextId = () => `m${++counter}`

/** 输入框上传卡片的状态（渲染在聊天流里） */
export type UploadCard = {
  id: string
  name: string
  percent: number
  status: 'uploading' | 'parsing' | 'ready' | 'failed'
  error?: string
}

const asString = (value: unknown, fallback = ''): string =>
  typeof value === 'string' ? value : fallback
const asNumber = (value: unknown): number => (typeof value === 'number' ? value : 0)

function parseUsage(raw: unknown): Usage | undefined {
  if (!raw || typeof raw !== 'object') return undefined
  const usage = raw as Record<string, unknown>
  return {
    prompt_tokens: asNumber(usage.prompt_tokens),
    completion_tokens: asNumber(usage.completion_tokens),
    total_tokens: asNumber(usage.total_tokens),
    cache_hit_tokens: asNumber(usage.cache_hit_tokens),
  }
}

/** 把全库统计的响应规整成稳定形状（后端不可用/字段缺失时不产生 NaN）。 */
function normalizeStats(raw: Partial<UsageStats> | null | undefined): UsageStats {
  return {
    sessions: asNumber(raw?.sessions),
    prompt_tokens: asNumber(raw?.prompt_tokens),
    completion_tokens: asNumber(raw?.completion_tokens),
    total_tokens: asNumber(raw?.total_tokens),
    cache_hit_tokens: asNumber(raw?.cache_hit_tokens),
    cache_hit_rate: typeof raw?.cache_hit_rate === 'number' ? raw.cache_hit_rate : 0,
  }
}

type ChatState = {
  models: ModelInfo[]
  model: string | null
  sessions: SessionInfo[]
  sessionId: string | null
  messages: ChatMessage[]
  streaming: boolean
  error: string | null
  notice: string | null
  draft: string
  /** 「编辑」消息时递增，Composer 监听它把焦点移回输入框 */
  draftFocusSignal: number
  /** 当前会话生效的 system 消息（空串 = 用配置里的默认提示词） */
  systemPrompt: string

  // ---- 知识库（RAG） ----
  kbDocs: KnowledgeDoc[]
  kbStatus: KnowledgeStatus | null
  /** 正在上传的文件名与进度（null = 没在传） */
  kbUpload: { name: string; percent: number } | null
  /** 输入框上传产生的「聊天气泡卡片」（上传中 → 解析中 → 已加入/失败） */
  kbUploads: UploadCard[]
  kbError: string | null
  // ---- 工作区（Workspace Switcher） ----
  /** 当前生效的工作区绝对路径（null = 还没拉到） */
  workspace: string | null
  workspaceDefault: string | null
  /** 附加目录（@name 命名挂载） */
  workspaceExtras: WorkspaceExtra[]
  // ---- 用量统计（底部状态栏） ----
  /** 全库累计用量（GET /api/stats；null = 还没拉到 / 后端不可用） */
  stats: UsageStats | null
  /** 当前会话上下文占用率 0–1（最近一轮 prompt_tokens / 模型 context_window） */
  contextUsage: number | null
  loadWorkspace: () => Promise<void>
  /** 静默刷新全库 token 统计（失败不打扰） */
  loadStats: () => Promise<void>
  setWorkspace: (path: string, extras?: WorkspaceExtra[]) => Promise<void>
  resetWorkspace: () => Promise<void>
  loadKb: () => Promise<void>
  uploadKbDoc: (file: File) => Promise<void>
  /** 输入框回形针：支持多选，每个文件一张卡片 */
  uploadKbDocs: (files: File[]) => Promise<void>
  dismissKbUpload: (id: string) => void
  deleteKbDoc: (id: string) => Promise<void>

  setModels: (models: ModelInfo[]) => void
  setModel: (model: string) => void
  setSessions: (sessions: SessionInfo[]) => void
  setDraft: (draft: string) => void
  /** 编辑：把这条用户消息回填到底部输入框并请求聚焦 */
  editMessage: (messageId: string) => void
  setError: (error: string | null) => void
  clearNotice: () => void
  setNotice: (notice: string | null) => void

  /** 新建会话：POST /api/sessions → 清空主区 → 刷新列表（并高亮新会话） */
  startNewSession: () => Promise<void>
  /** 点击历史会话：拉详情并用 reconstructMessages 还原 */
  openSession: (sessionId: string) => Promise<void>
  /** 静默刷新侧边栏列表（失败不打扰用户） */
  refreshSessions: () => Promise<void>
  /** 保存系统提示词：PATCH 后下一句对话立即生效 */
  saveSystemPrompt: (prompt: string) => Promise<boolean>
  /** 重命名会话（PATCH），成功后更新左侧列表 */
  renameSession: (sessionId: string, title: string) => Promise<boolean>
  /** 删除会话（DELETE）；删的是当前会话则清空主区 */
  deleteSession: (sessionId: string) => Promise<boolean>
  /** 重新拉取模型列表（内置 + 自定义） */
  loadModels: () => Promise<void>
  /** 添加自定义模型；成功后刷新列表并切到它 */
  addCustomModel: (payload: CustomModelPayload) => Promise<boolean>
  /** 删除自定义模型；删的是当前选中模型则退回默认模型 */
  deleteCustomModel: (name: string) => Promise<boolean>
  /** 自动起标题：fire-and-forget，失败静默（保持列表里的截断标题） */
  generateTitle: (sessionId: string) => Promise<void>

  sendMessage: (text: string) => Promise<void>
  /** 点「停止」：中断 SSE，保留已生成内容并标记为已中断 */
  stopStreaming: () => void
  /** 把当前 assistant 消息标记为「已中断」（流被 abort 时调用） */
  markInterrupted: () => void
  /** 重新生成某条 assistant 回复（覆盖旧回答） */
  regenerate: (messageId: string) => Promise<void>
  /** 增量：缓冲 50ms 后一次性并入（避免每个 token 触发一次 React 渲染） */
  appendDelta: (delta: string) => void
  /** 立刻把缓冲区并入消息（结束/中断/重置前必须调用，否则会丢尾字） */
  flushDeltas: () => void
  /** 思维链增量：追加到当前 assistant 消息的 reasoning 字段 */
  appendReasoning: (text: string) => void
  beginMessage: () => void
  startToolCall: (id: string, name: string, args: string) => void
  endToolCall: (id: string, ok: boolean, content: string) => void
  /** SSE approval_required：给工具卡片挂上「待审批」状态 */
  markApprovalRequired: (toolCallId: string, approvalId: string, expiresIn?: number) => void
  /** 提交审批决定；404/409（过期/重复）只在本地说说明，不打断主流程 */
  approveToolCall: (approvalId: string, approved: boolean) => Promise<void>
  finishMessage: (usage?: Usage) => void
  reset: () => void
}

const initialState = {
  models: [] as ModelInfo[],
  model: null as string | null,
  sessions: [] as SessionInfo[],
  sessionId: null as string | null,
  messages: [] as ChatMessage[],
  streaming: false,
  error: null as string | null,
  notice: null as string | null,
  draft: '',
  draftFocusSignal: 0,
  systemPrompt: '',
  kbDocs: [] as KnowledgeDoc[],
  kbStatus: null as KnowledgeStatus | null,
  kbUpload: null as { name: string; percent: number } | null,
  kbUploads: [] as UploadCard[],
  kbError: null as string | null,
  workspace: null as string | null,
  workspaceDefault: null as string | null,
  workspaceExtras: [] as WorkspaceExtra[],
  stats: null as UsageStats | null,
  contextUsage: null as number | null,
}

// 流式文本的节流缓冲：每 FLUSH_INTERVAL_MS 才真正 set 一次 state，
// 避免「每个 token 一次 React 渲染」（README 第 54 条）。
// 工作区请求序号：用于丢弃「切换之后才返回」的过期 GET 响应
let workspaceRequestId = 0
// 统计请求序号：切会话后旧请求的结果不能覆盖新会话的数据
let statsRequestId = 0

const FLUSH_INTERVAL_MS = 50
let pendingDelta = ''
let flushTimer: ReturnType<typeof setTimeout> | null = null

export const useChatStore = create<ChatState>((set, get) => ({
  ...initialState,

  /** 把缓冲区里的文本一次性并入最后一条 assistant 消息。 */
  flushDeltas: () => {
    if (flushTimer) {
      clearTimeout(flushTimer)
      flushTimer = null
    }
    const buffered = pendingDelta
    pendingDelta = ''
    if (!buffered) return
    set((state) => {
      const messages = [...state.messages]
      const last = messages[messages.length - 1]
      if (!last || last.role !== 'assistant' || !last.streaming) {
        messages.push({ id: nextId(), role: 'assistant', content: buffered, streaming: true })
      } else {
        messages[messages.length - 1] = { ...last, content: (last.content ?? '') + buffered }
      }
      return { messages }
    })
  },


  setModels: (models) =>
    set((state) => ({
      models,
      model: state.model ?? models.find((item) => item.is_default)?.name ?? models[0]?.name ?? null,
    })),
  setModel: (model) => set({ model }),
  // 形状不对时保留旧列表，绝不把 undefined 塞进 state（会让 Sidebar 崩）
  setSessions: (sessions) => set({ sessions: Array.isArray(sessions) ? sessions : [] }),
  setDraft: (draft) => set({ draft }),

  editMessage: (messageId) =>
    set((state) => {
      const target = state.messages.find((item) => item.id === messageId)
      if (!target || target.role !== 'user') return {} // 只允许编辑用户消息
      return {
        draft: target.content ?? '',
        draftFocusSignal: state.draftFocusSignal + 1,
      }
    }),
  setError: (error) => set({ error }),
  clearNotice: () => set({ notice: null }),
  setNotice: (notice) => set({ notice }),

  startNewSession: async () => {
    if (get().streaming) {
      set({ error: '正在生成回答，请稍候再新建会话' })
      return
    }
    try {
      const created = await api.createSession(get().model ?? undefined)
      set({
        sessionId: created.id,
        messages: [],
        streaming: false,
        error: null,
        draft: '',
        systemPrompt: '', // 新会话还没有自定义提示词，走配置默认
        stats: null,
        contextUsage: null,
      })
      await get().refreshSessions()
      void get().loadStats()
    } catch (error) {
      set({ error: error instanceof Error ? error.message : '新建会话失败' })
    }
  },

  openSession: async (sessionId) => {
    if (get().streaming) {
      set({ error: '正在生成回答，请稍候再切换会话' })
      return
    }
    try {
      const detail = await api.session(sessionId)
      const messages = reconstructMessages(detail.messages, `s-${detail.session.id}`)
      const modelName = detail.session.model || get().model
      const contextWindow = get().models.find((item) => item.name === modelName)?.context_window
      const lastAssistant = [...messages].reverse().find((item) => item.role === 'assistant' && item.usage)
      const lastUsage = lastAssistant?.usage
      set({
        sessionId: detail.session.id,
        model: modelName,
        messages,
        streaming: false,
        error: null,
        systemPrompt: detail.system_prompt ?? '',
        // 用最近一轮的 prompt_tokens 估算上下文占用（历史消息可能没有 usage）
        contextUsage:
          lastUsage && contextWindow && lastUsage.prompt_tokens > 0
            ? Math.min(lastUsage.prompt_tokens / contextWindow, 1)
            : null,
      })
      // 会话级统计：打开会话后立刻拉一次（口径锁定这个会话）
      void get().loadStats()
    } catch (error) {
      set({ error: error instanceof Error ? error.message : '打开会话失败' })
    }
  },

  saveSystemPrompt: async (prompt) => {
    try {
      let sessionId = get().sessionId
      // 还没有会话（比如刚刷新过页面）→ 先建一个，这样齿轮随时可用
      if (!sessionId) {
        const created = await api.createSession(get().model ?? undefined)
        sessionId = created.id
        set({ sessionId, messages: [], streaming: false, error: null, draft: '' })
      }
      const result = await api.updateSystemPrompt(sessionId, prompt)
      set({
        systemPrompt: result.system_prompt,
        notice: '已更新，下一句对话生效',
        error: null,
      })
      void get().refreshSessions()
      // 3 秒后自动收起提示；期间若又弹了新提示就不清（防止把新提示误清）
      window.setTimeout(() => {
        if (get().notice === '已更新，下一句对话生效') set({ notice: null })
      }, 3000)
      return true
    } catch (error) {
      set({ error: error instanceof Error ? error.message : '保存系统提示词失败' })
      return false
    }
  },

  refreshSessions: async () => {
    try {
      const { sessions } = await api.sessions()
      if (Array.isArray(sessions)) set({ sessions })
    } catch {
      // 静默刷新：失败就保持旧列表，不弹错误
    }
  },

  // ---- 工作区（Workspace Switcher） ----
  loadWorkspace: async () => {
    const requestId = ++workspaceRequestId
    try {
      const info = await api.workspace()
      // 丢弃过期响应：挂载时的 GET 可能比用户刚做的切换更晚返回，不能覆盖新值
      if (requestId !== workspaceRequestId) return
      set({
        workspace: info.workspace,
        workspaceDefault: info.default_workspace,
        workspaceExtras: info.extras ?? [],
      })
    } catch {
      // 后端不可用时静默：徽章仍显示默认文案
    }
  },

  loadStats: async () => {
    // 统计口径锁定「当前会话」：跨会话混算会让单条消息的命中率与累计值对不上
    const sessionId = get().sessionId
    if (!sessionId) {
      set({ stats: null })
      return
    }
    const requestId = ++statsRequestId
    try {
      const stats = normalizeStats(await api.stats(sessionId))
      if (requestId !== statsRequestId) return // 切会话后才返回的旧响应直接丢弃
      set({ stats })
    } catch {
      // 静默：统计是锦上添花，后端不可用时不显示即可
    }
  },

  setWorkspace: async (path, extras = []) => {
    workspaceRequestId += 1 // 让在途的 loadWorkspace 作废
    const info = await api.setWorkspace(path, extras) // 失败抛错，由弹窗展示
    set({
      workspace: info.workspace,
      workspaceDefault: info.default_workspace,
      workspaceExtras: info.extras ?? [],
    })
  },

  resetWorkspace: async () => {
    workspaceRequestId += 1
    const info = await api.resetWorkspace()
    set({
      workspace: info.workspace,
      workspaceDefault: info.default_workspace,
      workspaceExtras: info.extras ?? [],
    })
  },

  // ---- 知识库（RAG） ----
  loadKb: async () => {
    try {
      const [status, { docs }] = await Promise.all([api.kbStatus(), api.kbDocs()])
      set({ kbStatus: status, kbDocs: Array.isArray(docs) ? docs : [] })
    } catch {
      // 后端不可用时静默：面板仍可让用户尝试上传
    }
  },

  uploadKbDoc: async (file) => {
    set({ kbError: null, kbUpload: { name: file.name, percent: 0 } })
    try {
      await api.uploadKbDoc(file, (percent) =>
        set((state) => (state.kbUpload ? { kbUpload: { ...state.kbUpload, percent } } : {})),
      )
      // 上传到 100% 后进入解析阶段：轮询到不再是 parsing 为止
      for (let attempt = 0; attempt < 120; attempt += 1) {
        const { docs } = await api.kbDocs()
        set({ kbDocs: Array.isArray(docs) ? docs : [] })
        const target = (docs ?? []).find((doc) => doc.name === file.name)
        if (!target || target.status !== 'parsing') break
        await new Promise((resolve) => setTimeout(resolve, 400))
      }
      set({ kbStatus: await api.kbStatus() })
    } catch (error) {
      set({ kbError: error instanceof Error ? error.message : '上传失败' })
      await get().loadKb()
    } finally {
      set({ kbUpload: null })
    }
  },

  deleteKbDoc: async (id) => {
    try {
      const deleted = await api.deleteKbDoc(id)
      set((state) => ({ kbDocs: state.kbDocs.filter((doc) => doc.id !== deleted.id) }))
      set({ kbStatus: await api.kbStatus() })
    } catch (error) {
      set({ kbError: error instanceof Error ? error.message : '删除失败' })
    }
  },

  uploadKbDocs: async (files) => {
    if (files.length === 0) return
    const cards: UploadCard[] = files.map((file, index) => ({
      id: `kb-${Date.now()}-${index}`,
      name: file.name,
      percent: 0,
      status: 'uploading',
    }))
    set((state) => ({ kbUploads: [...state.kbUploads, ...cards], kbError: null }))

    const patch = (id: string, next: Partial<UploadCard>) =>
      set((state) => ({
        kbUploads: state.kbUploads.map((card) => (card.id === id ? { ...card, ...next } : card)),
      }))

    for (let index = 0; index < files.length; index += 1) {
      const file = files[index]
      const card = cards[index]
      try {
        await api.uploadKbDoc(file, (percent) =>
          patch(card.id, { percent, status: percent >= 100 ? 'parsing' : 'uploading' }),
        )
        patch(card.id, { percent: 100, status: 'parsing' })
        // 复用面板那套轮询：解析完成就变成「文档已加入知识库」
        let finalStatus = 'ready'
        let misses = 0
        for (let attempt = 0; attempt < 120; attempt += 1) {
          const { docs } = await api.kbDocs()
          const target = (docs ?? []).find((doc) => doc.name === file.name)
          if (target) {
            misses = 0
            set({ kbDocs: Array.isArray(docs) ? docs : [] })
            if (target.status !== 'parsing') {
              finalStatus = target.status
              if (target.status === 'failed') patch(card.id, { error: target.error ?? '解析失败' })
              break
            }
          } else if (++misses > 10) {
            // 连续 4 秒都查不到这份文档：当作失败，别让卡片永远转圈
            patch(card.id, { error: '上传后未在知识库中找到该文档' })
            finalStatus = 'failed'
            break
          }
          await new Promise((resolve) => setTimeout(resolve, 400))
        }
        patch(card.id, {
          status: finalStatus === 'failed' ? 'failed' : 'ready',
        })
      } catch (error) {
        patch(card.id, {
          status: 'failed',
          error: error instanceof Error ? error.message : '上传失败',
        })
      }
    }
    await get().loadKb()
  },

  dismissKbUpload: (id) =>
    set((state) => ({ kbUploads: state.kbUploads.filter((card) => card.id !== id) })),

  renameSession: async (sessionId, title) => {
    const next = title.trim()
    try {
      const updated = await api.renameSession(sessionId, next)
      // 就地更新，避免整表重排；随后再静默拉一次保持与后端一致
      set((state) => ({
        sessions: state.sessions.map((item) =>
          item.id === updated.id ? { ...item, ...updated } : item,
        ),
        error: null,
        notice: '已重命名',
      }))
      void get().refreshSessions()
      return true
    } catch (error) {
      set({ error: error instanceof Error ? error.message : '重命名失败' })
      return false
    }
  },

  deleteSession: async (sessionId) => {
    try {
      await api.deleteSession(sessionId)
      const wasCurrent = get().sessionId === sessionId
      set((state) => ({
        sessions: state.sessions.filter((item) => item.id !== sessionId),
        error: null,
        notice: '已删除会话',
        ...(wasCurrent
          ? { sessionId: null, messages: [], systemPrompt: '', streaming: false }
          : {}),
      }))
      void get().refreshSessions()
      return true
    } catch (error) {
      set({ error: error instanceof Error ? error.message : '删除失败' })
      return false
    }
  },

  loadModels: async () => {
    try {
      const models = await api.models()
      get().setModels(Array.isArray(models) ? models : [])
    } catch (error) {
      set({ error: error instanceof Error ? error.message : '加载模型列表失败' })
    }
  },

  generateTitle: async (sessionId) => {
    try {
      const result = await api.generateTitle(sessionId)
      if (result.generated && result.title) {
        // 就地更新标题，随后静默刷新保持与后端一致
        set((state) => ({
          sessions: state.sessions.map((item) =>
            item.id === sessionId ? { ...item, title: result.title } : item,
          ),
        }))
        void get().refreshSessions()
      }
    } catch {
      // 降级：保持原来的截断标题，不弹错误、不影响任何后续操作
    }
  },

  addCustomModel: async (payload) => {
    try {
      const created = await api.createCustomModel({
        ...payload,
        // 从别处粘贴常带前后空格，别把空格当 Key 存进库
        api_key: payload.api_key.trim(),
        base_url: payload.base_url.trim(),
        name: payload.name.trim(),
        model: payload.model.trim(),
      })
      await get().loadModels()
      set((state) => ({
        model: created.name, // 添加后直接切过去
        error: null,
        notice: `已添加模型 ${created.name}，已切换`,
        ...(state.model === created.name ? {} : {}),
      }))
      return true
    } catch (error) {
      set({ error: error instanceof Error ? error.message : '添加模型失败' })
      return false
    }
  },

  deleteCustomModel: async (name) => {
    if (get().streaming) {
      set({ notice: null, error: '请等待当前回答结束再删除模型' })
      return false
    }
    try {
      const result = await api.deleteCustomModel(name)
      await get().loadModels()
      const wasSelected = get().model === name
      const fallback =
        get().models.find((item) => item.is_default)?.name ?? get().models[0]?.name ?? null
      set({
        error: null,
        notice:
          result.referenced_sessions > 0
            ? `已删除模型 ${name}（有 ${result.referenced_sessions} 个会话仍指向它，下次发送会提示重新选择）`
            : `已删除模型 ${name}`,
        ...(wasSelected ? { model: fallback } : {}),
      })
      return true
    } catch (error) {
      set({ error: error instanceof Error ? error.message : '删除模型失败' })
      return false
    }
  },

  reset: () => {
    pendingDelta = ''
    if (flushTimer) {
      clearTimeout(flushTimer)
      flushTimer = null
    }
    set({ ...initialState })
  },

  beginMessage: () =>
    set((state) => {
      const messages = [...state.messages]
      const last = messages[messages.length - 1]
      if (last && last.role === 'assistant' && last.streaming) return { messages }
      messages.push({ id: nextId(), role: 'assistant', content: '', streaming: true })
      return { messages }
    }),

  sendMessage: async (text) => {
    const trimmed = text.trim()
    if (!trimmed || get().streaming) return

    const { model } = get()
    // 记录本轮之前是否空会话：只有第一次对话结束才尝试自动起标题
    const isFirstExchange = get().messages.length === 0
    set((state) => ({
      draft: '',
      error: null,
      streaming: true,
      messages: [
        ...state.messages,
        { id: nextId(), role: 'user', content: trimmed },
        { id: nextId(), role: 'assistant', content: '', streaming: true },
      ],
    }))

    let sessionId = get().sessionId
    const controller = new AbortController()
    activeAbort = controller
    try {
      if (!sessionId) {
        // 新会话第一次发言：先建会话（模块 5 会把历史列表刷新接进来）
        const created = await api.createSession(model ?? undefined)
        sessionId = created.id
        set({ sessionId })
      }
      await streamChat({
        sessionId,
        message: trimmed,
        model,
        baseUrl: apiBase(),
        onEvent: (event) => applyEvent(get, event),
        signal: controller.signal,
      })
    } catch (error) {
      // 用户主动点「停止」：不是错误，保留已生成内容并标记为已中断
      if (isAbortError(error)) {
        get().markInterrupted()
      } else {
        set({
          error: error instanceof Error ? error.message : '请求失败，请检查后端是否在运行',
        })
      }
    } finally {
      activeAbort = null
      // 兜底：无论成功、出错还是流中断，都不能让输入框永久禁用
      get().finishMessage()
      if (get().streaming) set({ streaming: false })
      // 一轮结束后静默刷新侧边栏（消息数 / tokens / 标题兜底数据都会更新）
      void get().refreshSessions()
      // 首轮对话结束后**不 await** 地起标题：绝不阻塞主对话，失败也不打扰用户
      if (isFirstExchange && sessionId) void get().generateTitle(sessionId)
    }
  },

  stopStreaming: () => {
    activeAbort?.abort()
    activeAbort = null
    get().markInterrupted()
    set({ streaming: false })
  },

  markInterrupted: () => {
    get().flushDeltas()
    set((state) => {
      const messages = [...state.messages]
      const last = messages[messages.length - 1]
      if (last && last.role === 'assistant') {
        messages[messages.length - 1] = {
          ...last,
          streaming: false,
          reasoningStreaming: false,
          interrupted: true,
        }
      }
      return { messages, streaming: false }
    })
  },

  regenerate: async (messageId) => {
    if (get().streaming) return // 同一时间只允许一条流
    const sessionId = get().sessionId
    const messages = get().messages
    const index = messages.findIndex((message) => message.id === messageId)
    if (!sessionId || index < 0) return

    // 找到这条 AI 回复之前的最后一条用户消息；UI 保留到它为止
    let userIndex = -1
    for (let cursor = index - 1; cursor >= 0; cursor -= 1) {
      if (messages[cursor].role === 'user') {
        userIndex = cursor
        break
      }
    }
    if (userIndex < 0) return

    set({
      messages: messages.slice(0, userIndex + 1),
      streaming: true,
      error: null,
      notice: null,
    })

    const controller = new AbortController()
    activeAbort = controller
    try {
      await streamRegenerate({
        sessionId,
        model: get().model,
        baseUrl: apiBase(),
        onEvent: (event) => applyEvent(get, event),
        signal: controller.signal,
      })
    } catch (error) {
      if (isAbortError(error)) {
        get().markInterrupted()
      } else {
        set({ error: error instanceof Error ? error.message : '重新生成失败' })
      }
    } finally {
      activeAbort = null
      get().finishMessage()
      if (get().streaming) set({ streaming: false })
      void get().refreshSessions()
    }
  },

  appendDelta: (delta) => {
    pendingDelta += delta
    // 缓冲攒得多就立刻冲刷：长文本优先吞吐，避免 50ms 内积压出一次长任务
    if (pendingDelta.length >= 2048) {
      get().flushDeltas()
      return
    }
    if (flushTimer !== null) return
    flushTimer = setTimeout(() => {
      flushTimer = null
      get().flushDeltas()
    }, FLUSH_INTERVAL_MS)
  },

  appendReasoning: (text) =>
    set((state) => {
      const messages = [...state.messages]
      const last = messages[messages.length - 1]
      if (!last || last.role !== 'assistant') return { messages }
      messages[messages.length - 1] = {
        ...last,
        reasoning: (last.reasoning ?? '') + text,
        reasoningStreaming: true,
      }
      return { messages }
    }),

  startToolCall: (id, name, args) =>
    set((state) => {
      const messages = [...state.messages]
      const last = messages[messages.length - 1]
      if (!last || last.role !== 'assistant') return { messages }
      const toolCalls = [
        ...(last.toolCalls ?? []),
        { id, name, arguments: args, status: 'pending' as ToolCallStatus },
      ]
      messages[messages.length - 1] = { ...last, toolCalls }
      return { messages }
    }),

  endToolCall: (id, ok, content) =>
    set((state) => {
      const messages = state.messages.map((message) => {
        if (!message.toolCalls?.some((call) => call.id === id)) return message
        return {
          ...message,
          toolCalls: message.toolCalls.map((call) =>
            call.id === id
              ? { ...call, status: ok ? ('success' as const) : ('error' as const), content }
              : call,
          ),
        }
      })
      return { messages }
    }),

  markApprovalRequired: (toolCallId, approvalId, expiresIn) =>
    set((state) => ({
      messages: state.messages.map((message) => {
        if (!message.toolCalls?.some((call) => call.id === toolCallId)) return message
        return {
          ...message,
          toolCalls: message.toolCalls.map((call) =>
            call.id === toolCallId
              ? {
                  ...call,
                  approval: {
                    id: approvalId,
                    status: 'pending' as const,
                    ...(expiresIn !== undefined ? { expiresIn } : {}),
                  },
                }
              : call,
          ),
        }
      }),
    })),

  approveToolCall: async (approvalId, approved) => {
    const setApprovalStatus = (status: 'approved' | 'rejected' | 'expired') =>
      set((state) => ({
        messages: state.messages.map((message) => {
          if (!message.toolCalls?.some((call) => call.approval?.id === approvalId)) return message
          return {
            ...message,
            toolCalls: message.toolCalls.map((call) =>
              call.approval?.id === approvalId
                ? { ...call, approval: { ...call.approval, status } }
                : call,
            ),
          }
        }),
      }))

    try {
      await api.approveToolCall(approvalId, approved)
      setApprovalStatus(approved ? 'approved' : 'rejected')
    } catch (error) {
      // 404/409：审批已过期/已被处理（重复点击、流已结束）；提示一下即可，不打断对话
      setApprovalStatus('expired')
      set({
        notice:
          error instanceof Error ? `审批未生效：${error.message}` : '审批未生效，可能已过期',
      })
    }
  },

  finishMessage: (usage) => {
    get().flushDeltas()
    set((state) => {
      const messages = [...state.messages]
      const last = messages[messages.length - 1]
      if (last && last.role === 'assistant') {
        messages[messages.length - 1] = {
          ...last,
          streaming: false,
          reasoningStreaming: false,
          ...(usage ? { usage } : {}),
        }
      }
      // 上下文占用率：本轮 prompt_tokens / 当前模型 context_window（单轮场景是精确值，
      // 多轮工具调用时是近似值 —— 只做「静默提示」，不参与逻辑判断）
      const contextWindow = state.models.find((item) => item.name === state.model)?.context_window
      const contextUsage =
        usage && contextWindow && usage.prompt_tokens > 0
          ? Math.min(usage.prompt_tokens / contextWindow, 1)
          : state.contextUsage
      return { messages, streaming: false, contextUsage }
    })
    // 一轮结束后静默刷新全库统计（失败不打扰用户）
    void get().loadStats()
  },
}))
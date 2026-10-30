/** 与后端 /api/* 对齐的类型定义。 */

export type ModelInfo = {
  name: string
  model: string
  base_url: string
  api_key_env: string
  key_set: boolean
  is_default: boolean
  /** 上下文窗口大小（前端算「上下文占用率」用） */
  context_window: number
  /** 是否是 Web 端添加的自定义模型（config.yaml 里的为 false） */
  is_custom: boolean
  has_key: boolean
  /** 脱敏后的密钥，如 sk-****abcd；内置模型为空 */
  api_key_masked: string
  /** 是否支持图片输入 */
  supports_vision?: boolean
}

/** 图片附件（POST /api/uploads 的返回值） */
export type Attachment = {
  id: string
  url: string
  mime: string
  name: string
  size: number
}

export type CustomModelPayload = {
  name: string
  base_url: string
  model: string
  api_key: string
}

export type CustomModel = {
  name: string
  base_url: string
  model: string
  has_key: boolean
  api_key_masked: string
  created_at: string
  updated_at: string
}

export type SessionInfo = {
  id: string
  title: string
  model: string
  created_at: string
  updated_at: string
  message_count: number
  total_tokens: number
  /** title 为空时，后端给的第一条 user 消息（供前端兜底显示标题） */
  first_user_message?: string
}

export type Usage = {
  prompt_tokens: number
  completion_tokens: number
  total_tokens: number
  /** 缓存命中的 prompt token 数（DeepSeek / OpenAI 等；取不到为 0） */
  cache_hit_tokens: number
}

/** token 用量统计（GET /api/stats），底部状态栏静默展示 */
export type UsageStats = {
  sessions: number
  prompt_tokens: number
  completion_tokens: number
  total_tokens: number
  cache_hit_tokens: number
  /** 0–1 的缓存命中率 = cache_hit_tokens / prompt_tokens（分母不含 completion） */
  cache_hit_rate: number
  /** 按会话统计时的会话 id；全库统计为 null */
  session_id?: string | null
}

export type ToolCallStatus = 'pending' | 'success' | 'error'

/** 人工审批状态：pending 时展示审批卡片；决定后展示结果 */
export type ToolApprovalStatus = 'pending' | 'approved' | 'rejected' | 'expired'

export type ToolApproval = {
  id: string
  status: ToolApprovalStatus
  /** 后端给的等待上限（秒），用于提示 */
  expiresIn?: number
}

export type ToolCallView = {
  id: string
  name: string
  /** 模型返回的原始 JSON 字符串，前端自己解析/格式化 */
  arguments: string
  status: ToolCallStatus
  content?: string
  /** 需要人工审批的工具才会带这个字段（来自 SSE approval_required） */
  approval?: ToolApproval
}

export type ChatMessage = {
  id: string
  role: 'user' | 'assistant'
  /** 可能为 null（模型只返回 tool_calls 时） */
  content: string | null
  toolCalls?: ToolCallView[]
  usage?: Usage
  streaming?: boolean
  /** 思维链（reasoning_content）增量拼接结果；不落库，刷新后消失 */
  reasoning?: string
  /** 思考是否仍在流式输出 */
  reasoningStreaming?: boolean
  /** 用户点了「停止」：内容是已收到的部分，不是完整回答 */
  interrupted?: boolean
  /** 用户消息附带的图片（旧消息没有该字段） */
  attachments?: Attachment[]
}

/** GET /api/sessions/{id} 里的消息形态（OpenAI 形状 + 思维链） */
export type ApiMessage = {
  role: string
  content?: string | null
  tool_calls?: Array<{
    id: string
    type: string
    function: { name: string; arguments: string }
  }> | null
  tool_call_id?: string | null
  name?: string | null
  /** 已落库的思维链（旧消息没有这一项） */
  reasoning_content?: string | null
  /** 当轮 token 用量（旧消息没有这一项）；只用于展示，不会回传给模型 */
  usage?: Usage | null
  /** 用户消息的图片附件（旧消息没有该字段） */
  attachments?: Attachment[] | null
}

/** 知识库文档（GET /api/kb/docs） */
export type KnowledgeDoc = {
  id: string
  name: string
  size: number
  pages: number
  chunk_count: number
  /** parsing 解析中 / ready 就绪 / failed 失败 */
  status: string
  mode: string
  error: string | null
  created_at: string
}

/** 知识库状态（GET /api/kb/status） */
export type KnowledgeStatus = {
  docs: number
  chunks: number
  mode: string
  supported: string[]
  max_file_mb: number
  pipeline: string
}

/** 附加目录（@name 命名挂载） */
export type WorkspaceExtra = {
  /** 挂载名，对应 @name/ 前缀 */
  name: string
  /** 附加目录绝对路径 */
  path: string
  /** 是否允许写入；默认只读 */
  writable: boolean
}

/** 工作区（GET/POST /api/config/workspace） */
export type WorkspaceInfo = {
  /** 当前生效的绝对路径 */
  workspace: string
  /** config.yaml 里的默认工作区 */
  default_workspace: string
  is_default: boolean
  /** 附加目录（命名挂载） */
  extras: WorkspaceExtra[]
}

/** 目录选择器：GET /api/fs/dirs */
export type FsEntry = {
  name: string
  path: string
}

export type FsListing = {
  /** 当前目录；空字符串表示「此电脑」（只显示盘符） */
  path: string
  parent: string | null
  entries: FsEntry[]
  drives: string[]
  truncated: boolean
}

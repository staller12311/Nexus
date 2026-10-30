import type {
  ApiMessage,
  Attachment,
  CustomModel,
  CustomModelPayload,
  KnowledgeDoc,
  KnowledgeStatus,
  ModelInfo,
  FsEntry,
  FsListing,
  SessionInfo,
  UsageStats,
  WorkspaceExtra,
  WorkspaceInfo,
} from '../types'
import { apiBase } from '../lib/base'

/**
 * 所有请求都走 /api/*（默认相对路径，由 vite.config.ts 的 proxy 转发到后端），
 * 这样前端代码里不会出现 http://127.0.0.1:8000 这样的硬编码地址。
 */
async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${apiBase()}/api${path}`, {
    headers: { 'Content-Type': 'application/json' },
    ...init,
  })
  if (!response.ok) {
    let detail = `HTTP ${response.status}`
    try {
      const body = (await response.json()) as { detail?: unknown }
      if (body?.detail) detail = String(body.detail)
    } catch {
      /* 响应不是 JSON 就用状态码 */
    }
    throw new Error(detail)
  }
  return (await response.json()) as T
}

/**
 * 上传用 XHR：只有 XHR 的 `upload.onprogress` 能拿到**上传百分比**
 * （fetch 无法上报上传进度），所以这里单独实现，其余接口仍走 request()。
 */
function uploadFile<T>(path: string, file: File, onProgress?: (percent: number) => void): Promise<T> {
  return new Promise<T>((resolve, reject) => {
    const xhr = new XMLHttpRequest()
    xhr.open('POST', `${apiBase()}/api${path}`)
    xhr.upload.onprogress = (event) => {
      if (event.lengthComputable && onProgress) {
        onProgress(Math.round((event.loaded / event.total) * 100))
      }
    }
    xhr.onload = () => {
      if (xhr.status >= 200 && xhr.status < 300) {
        try {
          resolve(JSON.parse(xhr.responseText) as T)
        } catch {
          reject(new Error('上传响应不是 JSON'))
        }
        return
      }
      let detail = `HTTP ${xhr.status}`
      try {
        const body = JSON.parse(xhr.responseText) as { detail?: unknown }
        if (body?.detail) detail = String(body.detail)
      } catch {
        /* 非 JSON 用状态码 */
      }
      reject(new Error(detail))
    }
    xhr.onerror = () => reject(new Error('网络错误，上传失败'))
    const form = new FormData()
    form.append('file', file)
    xhr.send(form)
  })
}

export const api = {
  models: () => request<ModelInfo[]>('/models'),
  /** 上传一张对话图片（先压缩再传；≤5MB） */
  uploadImage: (file: File, onProgress?: (percent: number) => void) =>
    uploadFile<Attachment>('/uploads', file, onProgress),
  /** token 用量统计：默认按当前会话（传 sessionId）；不传为全库 */
  stats: (sessionId?: string | null) =>
    request<UsageStats>(
      `/stats${sessionId ? `?session_id=${encodeURIComponent(sessionId)}` : ''}`,
    ),
  /** 人工审批：同意/拒绝一次危险工具调用 */
  approveToolCall: (approvalId: string, approved: boolean, reason = '') =>
    request<{ ok: boolean; approval_id: string; approved: boolean }>('/chat/approve', {
      method: 'POST',
      body: JSON.stringify({ approval_id: approvalId, approved, reason }),
    }),
  sessions: (limit = 20) => request<{ sessions: SessionInfo[] }>(`/sessions?limit=${limit}`),
  session: (id: string) =>
    request<{ session: SessionInfo; messages: ApiMessage[]; system_prompt: string }>(
      `/sessions/${encodeURIComponent(id)}`,
    ),
  createSession: (model?: string) =>
    request<SessionInfo>('/sessions', {
      method: 'POST',
      body: JSON.stringify(model ? { model } : {}),
    }),
  updateSystemPrompt: (id: string, systemPrompt: string) =>
    request<{ session_id: string; system_prompt: string; created: boolean; seq: number }>(
      `/sessions/${encodeURIComponent(id)}/system_prompt`,
      { method: 'PATCH', body: JSON.stringify({ system_prompt: systemPrompt }) },
    ),
  renameSession: (id: string, title: string) =>
    request<SessionInfo>(`/sessions/${encodeURIComponent(id)}`, {
      method: 'PATCH',
      body: JSON.stringify({ title }),
    }),
  deleteSession: (id: string) =>
    request<{ deleted: boolean; session: SessionInfo | null }>(
      `/sessions/${encodeURIComponent(id)}`,
      { method: 'DELETE' },
    ),
  /** 用一次短超时的模型调用给会话起标题（前端 fire-and-forget，失败静默降级） */
  generateTitle: (id: string) =>
    request<{ session_id: string; title: string; generated: boolean; model: string }>(
      `/sessions/${encodeURIComponent(id)}/generate_title`,
      { method: 'POST' },
    ),
  /** 让后端代发一次 GET {base_url}/models，拿回模型 id 列表（Key 只在前端内存里传一次） */
  fetchModels: (baseUrl: string, apiKey: string) =>
    request<{ base_url: string; models: string[]; count: number }>('/models/fetch', {
      method: 'POST',
      body: JSON.stringify({ base_url: baseUrl, api_key: apiKey }),
    }),
  customModels: () => request<{ models: CustomModel[] }>('/models/custom'),
  createCustomModel: (payload: CustomModelPayload) =>
    request<CustomModel>('/models/custom', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),
  deleteCustomModel: (name: string) =>
    request<{ deleted: boolean; name: string; referenced_sessions: number }>(
      `/models/custom/${encodeURIComponent(name)}`,
      { method: 'DELETE' },
    ),

  // ---- 知识库（RAG） ----
  kbStatus: () => request<KnowledgeStatus>('/kb/status'),
  kbDocs: () => request<{ docs: KnowledgeDoc[] }>('/kb/docs'),
  uploadKbDoc: (file: File, onProgress?: (percent: number) => void) =>
    uploadFile<KnowledgeDoc>('/kb/docs', file, onProgress),
  deleteKbDoc: (id: string) =>
    request<KnowledgeDoc>(`/kb/docs/${encodeURIComponent(id)}`, { method: 'DELETE' }),

  // ---- 目录选择器 ----
  listDirs: (path = '') =>
    request<FsListing>(`/fs/dirs${path ? `?path=${encodeURIComponent(path)}` : ''}`),
  makeDir: (parent: string, name: string) =>
    request<FsEntry>('/fs/mkdir', {
      method: 'POST',
      body: JSON.stringify({ parent, name }),
    }),

  // ---- 工作区切换（Workspace Switcher） ----
  workspace: () => request<WorkspaceInfo>('/config/workspace'),
  setWorkspace: (path: string, extras: WorkspaceExtra[] = []) =>
    request<WorkspaceInfo>('/config/workspace', {
      method: 'POST',
      body: JSON.stringify({ path, extras }),
    }),
  resetWorkspace: () => request<WorkspaceInfo>('/config/workspace', { method: 'DELETE' }),
}
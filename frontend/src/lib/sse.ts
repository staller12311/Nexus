/**
 * SSE 解析与客户端（模块 4）。
 *
 * 后端每个事件是 `data: <JSON>\n\n`。用 fetch + ReadableStream 手动解析：
 * 必须用 buffer 做字符串拼接 —— 一个 JSON 很可能被切在两次 `reader.read()` 之间，
 * 直接对半截字符串 `JSON.parse` 会抛错。
 */

export type ServerEvent = { type: string; [key: string]: unknown }

/** 从一帧（已去掉尾部空行）里取出 `data:` 后面的负载；不是 data 帧则返回 null。 */
function extractData(frame: string): string | null {
  const lines = frame.split('\n')
  const dataLines: string[] = []
  for (const line of lines) {
    if (line.startsWith('data:')) {
      // 规范允许 `data:foo` 与 `data: foo` 两种写法
      dataLines.push(line.slice(5).replace(/^ /, ''))
    }
  }
  if (dataLines.length === 0) return null
  return dataLines.join('\n')
}

/**
 * 把 SSE 字节流解析成事件对象。
 *
 * - 只按 `\n\n` 切帧，跨 chunk 的半截内容留在 buffer 里等下一次；
 * - 单帧 JSON 解析失败只丢弃该帧（不中断整条流）；
 * - 流结束时 buffer 里剩下的不完整帧直接丢弃（后端每帧都以 `\n\n` 收尾）。
 */
export async function* parseSSEStream(
  stream: ReadableStream<Uint8Array>,
): AsyncGenerator<ServerEvent> {
  const reader = stream.getReader()
  const decoder = new TextDecoder()
  let buffer = ''

  try {
    for (;;) {
      const { done, value } = await reader.read()
      if (done) break
      buffer += decoder.decode(value, { stream: true })

      let boundary = buffer.indexOf('\n\n')
      while (boundary !== -1) {
        const frame = buffer.slice(0, boundary)
        buffer = buffer.slice(boundary + 2)
        const payload = extractData(frame)
        if (payload !== null) {
          try {
            const parsed: unknown = JSON.parse(payload)
            if (parsed && typeof parsed === 'object' && typeof (parsed as ServerEvent).type === 'string') {
              yield parsed as ServerEvent
            }
          } catch {
            // 坏帧（半截/非法 JSON）：丢弃，继续消费后续帧
          }
        }
        boundary = buffer.indexOf('\n\n')
      }
    }
  } finally {
    reader.releaseLock()
  }
}

export type StreamChatOptions = {
  sessionId: string
  message: string
  attachments?: Array<{ id: string; name?: string }>
  model?: string | null
  /** 默认空字符串：走 Vite proxy；测试里可传 http://127.0.0.1:8000 */
  baseUrl?: string
  onEvent: (event: ServerEvent) => void
  signal?: AbortSignal
}

export type StreamRegenerateOptions = {
  sessionId: string
  model?: string | null
  baseUrl?: string
  onEvent: (event: ServerEvent) => void
  signal?: AbortSignal
}

/** 发一个 POST 并逐事件回调；两条流式接口共用这段。 */
async function postStream(
  path: string,
  body: unknown,
  onEvent: (event: ServerEvent) => void,
  options: { baseUrl?: string; signal?: AbortSignal },
): Promise<void> {
  const { baseUrl = '', signal } = options
  const response = await fetch(`${baseUrl}${path}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Accept: 'text/event-stream' },
    body: JSON.stringify(body),
    ...(signal ? { signal } : {}),
  })

  if (!response.ok) {
    let detail = `HTTP ${response.status}`
    try {
      const payload = (await response.json()) as { detail?: unknown }
      if (payload?.detail) detail = String(payload.detail)
    } catch {
      /* 非 JSON 错误体就用状态码 */
    }
    throw new Error(detail)
  }
  if (!response.body) throw new Error('后端没有返回事件流')

  for await (const event of parseSSEStream(response.body)) {
    onEvent(event)
  }
}

/** 发起一次流式对话，逐个把事件交给 onEvent。HTTP 非 2xx 直接抛错。 */
export async function streamChat(options: StreamChatOptions): Promise<void> {
  const { sessionId, message, attachments, model, baseUrl, onEvent, signal } = options
  await postStream(
    '/api/chat/stream',
    {
      session_id: sessionId,
      message,
      ...(attachments?.length
        ? { attachments: attachments.map((item) => ({ id: item.id, name: item.name })) }
        : {}),
      ...(model ? { model } : {}),
    },
    onEvent,
    { baseUrl, signal },
  )
}

/** 重新生成上一轮回答（后端会先删掉旧的 assistant 消息）。 */
export async function streamRegenerate(options: StreamRegenerateOptions): Promise<void> {
  const { sessionId, model, baseUrl, onEvent, signal } = options
  const query = model ? `?model=${encodeURIComponent(model)}` : ''
  await postStream(
    `/api/sessions/${encodeURIComponent(sessionId)}/regenerate${query}`,
    {},
    onEvent,
    { baseUrl, signal },
  )
}
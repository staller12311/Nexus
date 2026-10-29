import { beforeEach, describe, expect, it, vi } from 'vitest'
import { useChatStore } from '../store/chatStore'
import type { SessionInfo } from '../types'

function session(id = 'sess-1', title = ''): SessionInfo {
  return {
    id,
    title,
    model: 'webmock',
    created_at: '',
    updated_at: '',
    message_count: 0,
    total_tokens: 0,
    first_user_message: '用 Python 写个快排',
  }
}

function sseStream(frames: string[]): ReadableStream<Uint8Array> {
  const encoder = new TextEncoder()
  return new ReadableStream<Uint8Array>({
    start(controller) {
      for (const frame of frames) controller.enqueue(encoder.encode(frame))
      controller.close()
    },
  })
}

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

const TURN_FRAMES = [
  'data: {"type":"message_start","session_id":"sess-1"}\n\n',
  'data: {"type":"text_delta","delta":"好的，这是快排"}\n\n',
  'data: {"type":"message_end","content":"好的，这是快排","usage":{"prompt_tokens":5,"completion_tokens":6,"total_tokens":11}}\n\n',
]

type Options = {
  titleStatus?: number
  titleBody?: unknown
  titleDelay?: number
  initialTitle?: string
}

function stubFetch(options: Options = {}) {
  const calls: Array<{ url: string; method?: string }> = []
  // 有状态的"后端库"：generate_title 成功后，列表接口也要能读到新标题
  let currentTitle = options.initialTitle ?? ''

  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      const method = init?.method ?? 'GET'
      calls.push({ url, method })

      if (url.includes('/generate_title')) {
        if (options.titleDelay) await new Promise((r) => setTimeout(r, options.titleDelay))
        if (options.titleStatus && options.titleStatus >= 400) {
          return jsonResponse({ detail: '标题生成失败，已保留原标题' }, options.titleStatus)
        }
        const body = (options.titleBody ?? {
          session_id: 'sess-1',
          title: 'Python快排实现',
          generated: true,
          model: 'webmock',
        }) as { title: string; generated: boolean }
        if (body.generated) currentTitle = body.title
        return jsonResponse(body)
      }
      if (url.includes('/api/sessions') && method === 'POST') {
        return jsonResponse(session(), 201)
      }
      if (url.startsWith('/api/chat/stream')) {
        return new Response(sseStream(TURN_FRAMES), {
          status: 200,
          headers: { 'Content-Type': 'text/event-stream' },
        })
      }
      if (url.startsWith('/api/sessions')) {
        return jsonResponse({ sessions: [{ ...session(), title: currentTitle }] })
      }
      return jsonResponse({}, 404)
    }),
  )
  return calls
}

describe('自动起标题', () => {
  beforeEach(() => {
    useChatStore.getState().reset()
    vi.unstubAllGlobals()
  })

  it('首轮对话结束后会调用 generate_title，并把标题写回列表', async () => {
    useChatStore.getState().setSessions([session()])
    const calls = stubFetch()

    await useChatStore.getState().sendMessage('用 Python 写个快排')

    // 标题请求确实发出去了
    await vi.waitFor(() =>
      expect(calls.some((call) => call.url.endsWith('/generate_title'))).toBe(true),
    )
    // 列表标题被更新
    await vi.waitFor(() =>
      expect(useChatStore.getState().sessions[0].title).toBe('Python快排实现'),
    )
    expect(useChatStore.getState().error).toBeNull()
  })

  it('起标题失败（503）时静默降级：不弹错误、对话内容不受影响', async () => {
    useChatStore.getState().setSessions([session()])
    stubFetch({ titleStatus: 503 })

    await useChatStore.getState().sendMessage('用 Python 写个快排')
    await new Promise((resolve) => setTimeout(resolve, 50))

    const state = useChatStore.getState()
    expect(state.error).toBeNull() // 不弹 ErrorBanner
    expect(state.streaming).toBe(false) // 输入框可用
    expect(state.messages[1].content).toContain('快排') // 主对话完好
    expect(state.sessions[0].title).toBe('') // 保持截断兜底（title 仍为空）
  })

  it('非首轮对话不会重复起标题', async () => {
    useChatStore.getState().setSessions([session()])
    useChatStore.setState({
      sessionId: 'sess-1',
      messages: [
        { id: 'a', role: 'user', content: '上一轮' },
        { id: 'b', role: 'assistant', content: '上一轮回答' },
      ],
    })
    const calls = stubFetch()

    await useChatStore.getState().sendMessage('第二轮追问')
    await new Promise((resolve) => setTimeout(resolve, 50))

    expect(calls.some((call) => call.url.endsWith('/generate_title'))).toBe(false)
  })

  it('不阻塞主对话：标题请求慢时，message_end 已经先把 streaming 置回 false', async () => {
    useChatStore.getState().setSessions([session()])
    stubFetch({ titleDelay: 500 })

    await useChatStore.getState().sendMessage('用 Python 写个快排')

    // sendMessage 返回时标题还在路上，但主对话已经结束
    const state = useChatStore.getState()
    expect(state.streaming).toBe(false)
    expect(state.messages[1].streaming).toBe(false)
    expect(state.messages[1].usage?.total_tokens).toBe(11)
    await vi.waitFor(() => expect(useChatStore.getState().sessions[0].title).toBe('Python快排实现'))
  })

  it('后端说 generated=false（已有标题）时不覆盖', async () => {
    useChatStore.getState().setSessions([session('sess-1', '我手动起的名字')])
    stubFetch({
      initialTitle: '我手动起的名字',
      titleBody: {
        session_id: 'sess-1',
        title: '我手动起的名字',
        generated: false,
        model: 'webmock',
      },
    })

    await useChatStore.getState().sendMessage('用 Python 写个快排')
    await new Promise((resolve) => setTimeout(resolve, 50))

    expect(useChatStore.getState().sessions[0].title).toBe('我手动起的名字')
  })
})
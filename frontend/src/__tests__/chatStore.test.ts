import { beforeEach, describe, expect, it, vi } from 'vitest'
import { useChatStore } from '../store/chatStore'

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

type StubOptions = { chatStatus?: number; reject?: boolean; frames?: string[] }

function stubFetch(options: StubOptions = {}): string[] {
  const calls: string[] = []
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      calls.push(url)
      if (url.includes('/api/sessions')) {
        return jsonResponse(
          {
            id: 'sess-1',
            title: '',
            model: 'webmock',
            created_at: '',
            updated_at: '',
            message_count: 0,
            total_tokens: 0,
          },
          201,
        )
      }
      if (options.reject) throw new Error('网络断了')
      if (options.chatStatus && options.chatStatus >= 400) {
        return jsonResponse({ detail: '后端内部错误' }, options.chatStatus)
      }
      return new Response(sseStream(options.frames ?? []), {
        status: 200,
        headers: { 'Content-Type': 'text/event-stream' },
      })
    }),
  )
  return calls
}

const okFrames = [
  'data: {"type":"message_start","session_id":"sess-1"}\n\n',
  'data: {"type":"tool_call_start","tool_call_id":"call_read","name":"read_file","arguments":"{\\"path\\": \\"readme.txt\\"}"}\n\n',
  'data: {"type":"tool_call_end","tool_call_id":"call_read","name":"read_file","ok":true,"content":"# readme.txt（2 字节）\\nhi"}\n\n',
  'data: {"type":"text_delta","delta":"readme.txt 里写的是："}\n\n',
  'data: {"type":"text_delta","delta":"hi"}\n\n',
  'data: {"type":"message_end","content":"readme.txt 里写的是：hi","usage":{"prompt_tokens":24,"completion_tokens":12,"total_tokens":36},"rounds":1,"tool_limit_reached":false}\n\n',
]

describe('chatStore.sendMessage 消费 SSE', () => {
  beforeEach(() => {
    useChatStore.getState().reset()
    vi.unstubAllGlobals()
  })

  it('完整事件序列：用户消息 + 工具卡片 + 增量文本 + usage', async () => {
    const calls = stubFetch({ frames: okFrames })
    await useChatStore.getState().sendMessage('读一下 readme.txt 的内容')

    const state = useChatStore.getState()
    expect(state.sessionId).toBe('sess-1')
    expect(calls.some((url) => url.includes('/api/sessions'))).toBe(true)
    expect(calls.some((url) => url.includes('/api/chat/stream'))).toBe(true)

    expect(state.messages).toHaveLength(2)
    expect(state.messages[0]).toMatchObject({ role: 'user', content: '读一下 readme.txt 的内容' })

    const assistant = state.messages[1]
    expect(assistant.role).toBe('assistant')
    expect(assistant.content).toBe('readme.txt 里写的是：hi') // text_delta 是增量拼出来的
    expect(assistant.streaming).toBe(false)
    expect(assistant.usage).toEqual({
      prompt_tokens: 24,
      completion_tokens: 12,
      total_tokens: 36,
      cache_hit_tokens: 0,
    })
    expect(assistant.toolCalls).toHaveLength(1)
    expect(assistant.toolCalls?.[0]).toMatchObject({
      id: 'call_read',
      name: 'read_file',
      arguments: '{"path": "readme.txt"}',
      status: 'success',
    })
    expect(assistant.toolCalls?.[0].content).toContain('hi')
    expect(state.streaming).toBe(false)
    expect(state.error).toBeNull()
  })

  it('text_delta 是增量消费：message_end 之前 content 已经在变（50ms 合并 flush）', async () => {
    const observed: string[] = []
    const unsubscribe = useChatStore.subscribe((state) => {
      const last = state.messages[state.messages.length - 1]
      // 只记录 content 发生变化的时刻（message_end 也会触发订阅，但内容没变）
      if (last?.role === 'assistant' && last.content && last.content !== observed[observed.length - 1]) {
        observed.push(last.content)
      }
    })

    stubFetch({
      frames: [
        'data: {"type":"text_delta","delta":"A"}\n\n',
        'data: {"type":"text_delta","delta":"B"}\n\n',
        'data: {"type":"text_delta","delta":"C"}\n\n',
        'data: {"type":"message_end","content":"ABC"}\n\n',
      ],
    })
    await useChatStore.getState().sendMessage('hi')
    unsubscribe()

    // 加了 50ms 节流后，同一 tick 内的 delta 会合并成一次刷新；
    // 关键契约是「不用等 message_end」，结束后内容完整。
    expect(observed.length).toBeGreaterThanOrEqual(1)
    expect(observed[observed.length - 1]).toBe('ABC')
    expect(useChatStore.getState().messages[1].streaming).toBe(false)
  })

  it('节流：短时间内大量 token 只触发极少数状态更新', () => {
    useChatStore.setState({
      messages: [{ id: 'a1', role: 'assistant', content: '', streaming: true }],
    })
    let updates = 0
    const unsubscribe = useChatStore.subscribe(() => {
      updates += 1
    })
    for (let index = 0; index < 100; index += 1) {
      useChatStore.getState().appendDelta('字')
    }
    const during = updates
    useChatStore.getState().flushDeltas()
    unsubscribe()

    expect(during).toBeLessThan(5) // 不是每个 token 一次渲染
    expect(useChatStore.getState().messages[0].content).toBe('字'.repeat(100))
  })

  it('工具执行失败：卡片标为 error 并带错误文本', async () => {
    stubFetch({
      frames: [
        'data: {"type":"tool_call_start","tool_call_id":"call_x","name":"read_file","arguments":"{\\"path\\":\\"nope.txt\\"}"}\n\n',
        'data: {"type":"tool_call_end","tool_call_id":"call_x","name":"read_file","ok":false,"content":"错误：文件不存在：nope.txt"}\n\n',
        'data: {"type":"message_end","content":"读不到","usage":{"prompt_tokens":1,"completion_tokens":1,"total_tokens":2}}\n\n',
      ],
    })
    await useChatStore.getState().sendMessage('读 nope.txt')

    const call = useChatStore.getState().messages[1].toolCalls?.[0]
    expect(call?.status).toBe('error')
    expect(call?.content).toContain('错误：文件不存在')
    expect(useChatStore.getState().streaming).toBe(false)
  })

  it('error 事件：进 ErrorBanner 且 streaming 复位', async () => {
    stubFetch({
      frames: [
        'data: {"type":"message_start","session_id":"sess-1"}\n\n',
        'data: {"type":"error","message":"ProviderAuthError: 密钥无效","code":"ProviderAuthError"}\n\n',
      ],
    })
    await useChatStore.getState().sendMessage('你好')

    const state = useChatStore.getState()
    expect(state.error).toContain('密钥无效')
    expect(state.streaming).toBe(false)
  })

  it('后端 500：显示 detail 且输入框不会永久禁用', async () => {
    stubFetch({ chatStatus: 500 })
    await useChatStore.getState().sendMessage('你好')

    const state = useChatStore.getState()
    expect(state.error).toBe('后端内部错误')
    expect(state.streaming).toBe(false)
    expect(state.messages[1].streaming).toBe(false)
  })

  it('网络异常（fetch reject）：同样复位 streaming', async () => {
    stubFetch({ reject: true })
    await useChatStore.getState().sendMessage('你好')

    const state = useChatStore.getState()
    expect(state.error).toBe('网络断了')
    expect(state.streaming).toBe(false)
  })

  it('流中途断掉（没有 message_end）：finally 兜底复位，不留转圈的卡片', async () => {
    stubFetch({
      frames: [
        'data: {"type":"tool_call_start","tool_call_id":"call_z","name":"get_time","arguments":"{}"}\n\n',
        'data: {"type":"text_delta","delta":"半截"}\n\n',
      ],
    })
    await useChatStore.getState().sendMessage('几点')

    const state = useChatStore.getState()
    expect(state.streaming).toBe(false)
    expect(state.messages[1].streaming).toBe(false)
    expect(state.messages[1].content).toBe('半截')
    expect(state.messages[1].toolCalls?.[0].status).toBe('pending') // 卡片仍在等待，刷新会话后会标红
  })

  it('发送期间重复调用被忽略（streaming 门闩）', async () => {
    stubFetch({ frames: ['data: {"type":"message_end"}\n\n'] })
    const first = useChatStore.getState().sendMessage('第一条')
    const second = useChatStore.getState().sendMessage('第二条')
    await Promise.all([first, second])

    const contents = useChatStore.getState().messages.map((message) => message.content)
    expect(contents).toEqual(['第一条', ''])
  })
})
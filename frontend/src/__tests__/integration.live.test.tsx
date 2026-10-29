import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeAll, describe, expect, it, vi } from 'vitest'
import { MessageBubble } from '../components/MessageBubble'
import { useChatStore } from '../store/chatStore'
import { streamChat } from '../lib/sse'
import { displayTitle } from '../lib/session'

// 默认打 8000；可用 VITE_TEST_BACKEND 指到别的端口（比如起了第二个指向假上游的后端）
const BACKEND = import.meta.env.VITE_TEST_BACKEND ?? 'http://127.0.0.1:8000'
const FAKE = 'http://127.0.0.1:8128'
const FAKE_HOST = '127.0.0.1:8128'

let backendUp = false
let fakeUp = false
/** 后端确认指向假上游时才跑，避免误花真实 API 的钱 */
let backendReady = false

async function control(mode: string, delay = 0): Promise<void> {
  await fetch(`${FAKE}/control`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ mode, delay }),
  })
}

async function reachable(url: string, init?: RequestInit): Promise<boolean> {
  try {
    const response = await fetch(url, { signal: AbortSignal.timeout(2000), ...init })
    return response.ok
  } catch {
    return false
  }
}

beforeAll(async () => {
  backendUp = await reachable(`${BACKEND}/api/health`)
  // 假上游必须也在
  fakeUp = await reachable(`${FAKE}/control`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: '{}',
  })

  // 关键：还要确认**后端连的就是假上游**。
  // 否则一旦 8000 上跑着真实配置（真 API），这些用例会花真钱、还会因为假上游的开关无效而误报。
  let targetsFake = false
  if (backendUp) {
    try {
      const response = await fetch(`${BACKEND}/api/models`, { signal: AbortSignal.timeout(2000) })
      const models = (await response.json()) as Array<{ base_url?: string }>
      targetsFake = models.some((item) => (item.base_url ?? '').includes(FAKE_HOST))
    } catch {
      targetsFake = false
    }
  }

  if (!backendUp || !fakeUp || !targetsFake) {
    console.warn(
      `[集成测试跳过] backend=${backendUp ? 'up' : 'down'} fakeUpstream=${fakeUp ? 'up' : 'down'}` +
        ` backendTargetsFake=${targetsFake}（需要 8000 与 8128 都在，且后端配置指向假上游，见 README）`,
    )
  }
  backendReady = backendUp && fakeUp && targetsFake
})

afterEach(() => {
  useChatStore.getState().reset()
})

/**
 * 打真后端：真 FastAPI + 真 SSE + 真 parseSSEStream + 真 store + 真 DOM 渲染。
 * 这条用例覆盖的就是浏览器里跑的那条代码路径（差别只有 URL 前缀）。
 */
describe('真后端端到端（需要 uvicorn + 可控假端点）', () => {
  it('工具调用成功：卡片绿、文本含 hi、usage 显示在气泡底部', async (ctx) => {
    if (!backendReady) ctx.skip() // 后端/假上游没齐就显式跳过，不伪装成通过
    await control('simple')
    vi.stubEnv('VITE_API_BASE', BACKEND)

    await useChatStore.getState().sendMessage('读一下 readme.txt 的内容')

    const state = useChatStore.getState()
    expect(state.error).toBeNull()
    expect(state.streaming).toBe(false)
    expect(state.sessionId).toBeTruthy()

    const assistant = state.messages[1]
    expect(assistant.toolCalls?.[0].status).toBe('success')
    expect(assistant.toolCalls?.[0].arguments).toBe('{"path": "readme.txt"}')
    expect(assistant.toolCalls?.[0].content).toContain('hi')
    expect(assistant.content).toContain('hi')
    expect(assistant.usage?.total_tokens).toBeGreaterThan(0)

    // 真的渲染成 DOM 再断言，等价于浏览器里看到的文本
    const { container } = render(
      <>
        {state.messages.map((message) => (
          <MessageBubble key={message.id} message={message} />
        ))}
      </>,
    )
    const collapsed = (container.textContent ?? '').replace(/\s+/g, ' ')
    console.log('[真实对话-折叠态 DOM]', collapsed)

    expect(collapsed).toContain('读一下 readme.txt 的内容')
    expect(collapsed).toContain('read_file')
    expect(collapsed).toContain('成功')
    expect(collapsed).toMatch(/prompt \d+ \/ completion \d+ \/ total \d+/)

    // 展开工具卡片，看到完整参数与返回结果
    const user = userEvent.setup()
    await user.click(screen.getByTestId(`tool-card-${assistant.toolCalls?.[0].id}`))
    const expanded = (container.textContent ?? '').replace(/\s+/g, ' ')
    console.log('[真实对话-展开态 DOM]', expanded)
    expect(expanded).toContain('参数')
    expect(expanded).toContain('返回结果')
    expect(expanded).toContain('"path": "readme.txt"')
    expect(expanded).toContain('hi')
  })

  it('工具执行失败（读不存在的文件）：卡片标红、错误文本完整、不崩', async (ctx) => {
    if (!backendReady) ctx.skip() // 后端/假上游没齐就显式跳过，不伪装成通过
    await control('missing')
    vi.stubEnv('VITE_API_BASE', BACKEND)

    await useChatStore.getState().sendMessage('读一下 nope.txt')

    const state = useChatStore.getState()
    expect(state.streaming).toBe(false)

    const assistant = state.messages[1]
    expect(assistant.toolCalls?.[0].status).toBe('error')
    expect(assistant.toolCalls?.[0].content).toContain('错误：文件不存在')
    // 模型仍然给出了最终回答（错误文本回灌给模型，这是 agent 的设计）
    expect(assistant.content).toBeTruthy()

    render(
      <>
        {state.messages.map((message) => (
          <MessageBubble key={message.id} message={message} />
        ))}
      </>,
    )
    expect(screen.getByText('失败')).toBeInTheDocument()
  })

  it('上游持续 5xx：收到 error 事件 → ErrorBanner 有内容、输入框可用', async (ctx) => {
    if (!backendReady) ctx.skip() // 后端/假上游没齐就显式跳过，不伪装成通过
    await control('boom')
    vi.stubEnv('VITE_API_BASE', BACKEND)

    await useChatStore.getState().sendMessage('会失败的一问')

    const state = useChatStore.getState()
    expect(state.error).toBeTruthy()
    console.log('[error 事件文本]', state.error)
    expect(state.streaming).toBe(false) // 输入框恢复可用
    expect(state.messages[1].streaming).toBe(false)
  })

  it('会话管理（真后端）：列表带 first_user_message，切换历史会话能还原出工具卡片', async (ctx) => {
    if (!backendReady) ctx.skip() // 后端/假上游没齐就显式跳过，不伪装成通过
    await control('simple')
    vi.stubEnv('VITE_API_BASE', BACKEND)

    // 1) 真聊一轮（带工具调用），拿到 sessionId
    await useChatStore.getState().sendMessage('读一下 readme.txt 的内容')
    const sessionId = useChatStore.getState().sessionId
    expect(sessionId).toBeTruthy()

    // 2) 列表接口应带回 first_user_message（前端标题兜底的数据来源）
    await useChatStore.getState().refreshSessions()
    const listed = useChatStore.getState().sessions.find((item) => item.id === sessionId)
    expect(listed?.first_user_message).toContain('读一下 readme.txt')
    expect(listed?.message_count).toBeGreaterThanOrEqual(5)

    const title = displayTitle(listed!)
    console.log('[侧边栏标题]', title)
    expect(title).toBe('读一下 readme.txt…') // 不是 UUID，也不是空

    // 3) 清空后点历史会话，还原上下文
    useChatStore.getState().reset()
    await useChatStore.getState().openSession(sessionId!)

    const state = useChatStore.getState()
    expect(state.sessionId).toBe(sessionId)
    expect(state.messages).toHaveLength(3) // user + assistant(卡片) + assistant(回答)
    expect(state.messages[0].content).toBe('读一下 readme.txt 的内容')
    expect(state.messages[1].toolCalls?.[0]).toMatchObject({
      name: 'read_file',
      arguments: '{"path": "readme.txt"}',
      status: 'success',
    })
    expect(state.messages[1].toolCalls?.[0].content).toContain('hi')
    expect(state.messages[2].content).toContain('hi')

    // 4) DOM 渲染：卡片与文本都在，且没有空气泡
    const { container } = render(
      <>
        {state.messages.map((message) => (
          <MessageBubble key={message.id} message={message} />
        ))}
      </>,
    )
    const text = (container.textContent ?? '').replace(/\s+/g, ' ')
    console.log('[历史会话恢复后的 DOM]', text)
    expect(text).toContain('read_file')
    expect(text).toContain('成功')
    expect(text).toContain('hi')
  })

  it('SSE 解析器直接对接真后端：事件顺序与字段正确', async (ctx) => {
    if (!backendReady) ctx.skip() // 后端/假上游没齐就显式跳过，不伪装成通过
    await control('simple')
    const created = await fetch(`${BACKEND}/api/sessions`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: '{}',
    })
    const session = (await created.json()) as { id: string }

    const types: string[] = []
    let toolContent = ''
    let usageTotal = 0
    await streamChat({
      sessionId: session.id,
      message: '读一下 readme.txt 的内容',
      baseUrl: BACKEND,
      onEvent: (event) => {
        types.push(event.type)
        if (event.type === 'tool_call_end') toolContent = String(event.content)
        if (event.type === 'message_end') {
          usageTotal = Number((event.usage as { total_tokens?: number })?.total_tokens ?? 0)
        }
      },
    })

    // 真流式后：text_delta 不再只有一条，而是逐 token 多条
    expect(types[0]).toBe('message_start')
    const toolStart = types.indexOf('tool_call_start')
    const toolEnd = types.indexOf('tool_call_end')
    expect(toolStart).toBeGreaterThan(0)
    expect(toolEnd).toBeGreaterThan(toolStart)
    expect(types[types.length - 1]).toBe('message_end')
    const deltas = types.filter((type) => type === 'text_delta')
    expect(deltas.length).toBeGreaterThan(1) // 逐 token
    // 工具卡之后到 message_end 之前只有正文增量
    expect(new Set(types.slice(toolEnd + 1, -1))).toEqual(new Set(['text_delta']))
    expect(toolContent).toContain('hi')
    expect(usageTotal).toBeGreaterThan(0)
  })

  it('流式思维链：reasoning_delta 先于 text_delta，且正文仍逐 token', async (ctx) => {
    if (!backendReady) ctx.skip()
    await control('reason')
    const created = await fetch(`${BACKEND}/api/sessions`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: '{}',
    })
    const session = (await created.json()) as { id: string }

    const types: string[] = []
    let reasoning = ''
    await streamChat({
      sessionId: session.id,
      message: '9.11 和 9.9 哪个大？',
      baseUrl: BACKEND,
      onEvent: (event) => {
        types.push(event.type)
        if (event.type === 'reasoning_delta') reasoning += String(event.text ?? '')
      },
    })

    const reasoningFrames = types.filter((type) => type === 'reasoning_delta')
    const textFrames = types.filter((type) => type === 'text_delta')
    expect(reasoningFrames.length).toBeGreaterThan(1)
    expect(textFrames.length).toBeGreaterThan(1)
    // 思考先于正文
    expect(types.indexOf('reasoning_delta')).toBeLessThan(types.indexOf('text_delta'))
    expect(reasoning).toContain('9.90')
    expect(types[types.length - 1]).toBe('message_end')
  })
})
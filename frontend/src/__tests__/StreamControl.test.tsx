import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { Composer } from '../components/Composer'
import { MessageBubble } from '../components/MessageBubble'
import { useChatStore } from '../store/chatStore'
import type { ChatMessage } from '../types'

function sseStream(frames: string[], { hang = false } = {}): ReadableStream<Uint8Array> {
  const encoder = new TextEncoder()
  return new ReadableStream<Uint8Array>({
    start(controller) {
      for (const frame of frames) controller.enqueue(encoder.encode(frame))
      if (!hang) controller.close()
    },
  })
}

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

const PARTIAL = [
  'data: {"type":"message_start","session_id":"s1"}\n\n',
  'data: {"type":"text_delta","delta":"这是前半"}\n\n',
  'data: {"type":"text_delta","delta":"段回答"}\n\n',
]

describe('中断生成', () => {
  beforeEach(() => {
    useChatStore.getState().reset()
    vi.unstubAllGlobals()
  })

  it('流式期间发送按钮变成红色「停止」按钮', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input)
        if (url.includes('/api/sessions') && url.endsWith('/api/sessions')) {
          return jsonResponse({ id: 's1' }, 201)
        }
        return new Response(sseStream(PARTIAL, { hang: true }), {
          status: 200,
          headers: { 'Content-Type': 'text/event-stream' },
        })
      }),
    )

    render(<Composer />)
    // 手动置为 streaming：模拟正在回答
    useChatStore.setState({ streaming: true })
    await waitFor(() => expect(screen.getByLabelText('停止生成')).toBeInTheDocument())
    expect(screen.queryByRole('button', { name: /发送/ })).not.toBeInTheDocument()
  })

  it('点停止：中断请求、保留已生成内容并标记「已中断」', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input)
        if (url.endsWith('/api/sessions')) return jsonResponse({ id: 's1' }, 201)
        // 确认 store 把 AbortSignal 传给了 fetch（否则 abort 不会真正生效）
        expect(init?.signal).toBeInstanceOf(AbortSignal)
        return new Response(sseStream(PARTIAL, { hang: true }), {
          status: 200,
          headers: { 'Content-Type': 'text/event-stream' },
        })
      }),
    )

    const user = userEvent.setup()
    render(<Composer />)
    void useChatStore.getState().sendMessage('讲个长故事')

    // 等前两个 delta 到达
    await waitFor(() => {
      const messages = useChatStore.getState().messages
      expect(messages[1]?.content).toBe('这是前半段回答')
    })

    await user.click(screen.getByLabelText('停止生成'))

    const state = useChatStore.getState()
    expect(state.streaming).toBe(false)
    expect(state.messages[1].interrupted).toBe(true)
    expect(state.messages[1].content).toBe('这是前半段回答') // 已生成内容保留
    expect(state.error).toBeNull() // 中断不是错误
  })

  it('「已中断」标记渲染在气泡操作区', () => {
    const message: ChatMessage = {
      id: 'a1',
      role: 'assistant',
      content: '被打断的回答',
      interrupted: true,
    }
    render(<MessageBubble message={message} />)
    expect(screen.getByText('已中断')).toBeInTheDocument()
  })
})

describe('重新生成', () => {
  beforeEach(() => {
    useChatStore.getState().reset()
    vi.unstubAllGlobals()
  })

  it('AI 消息有「重新生成」按钮，用户消息没有', () => {
    const { unmount } = render(
      <MessageBubble message={{ id: 'a1', role: 'assistant', content: '答' }} />,
    )
    expect(screen.getByLabelText('重新生成')).toBeInTheDocument()
    unmount()

    render(<MessageBubble message={{ id: 'u1', role: 'user', content: '问' }} />)
    expect(screen.queryByLabelText('重新生成')).not.toBeInTheDocument()
  })

  it('点重新生成：删掉旧回答、调用 regenerate 接口、流式写入新回答', async () => {
    const calls: Array<{ url: string; method?: string }> = []
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input)
        calls.push({ url, method: init?.method })
        if (url.includes('/regenerate')) {
          return new Response(
            sseStream([
              'data: {"type":"message_start","session_id":"s1"}\n\n',
              'data: {"type":"text_delta","delta":"新的回答"}\n\n',
              'data: {"type":"message_end","content":"新的回答","usage":{"prompt_tokens":1,"completion_tokens":2,"total_tokens":3}}\n\n',
            ]),
            { status: 200, headers: { 'Content-Type': 'text/event-stream' } },
          )
        }
        if (url.startsWith('/api/sessions')) return jsonResponse({ sessions: [] })
        return jsonResponse({}, 404)
      }),
    )

    useChatStore.setState({
      sessionId: 's1',
      messages: [
        { id: 'u1', role: 'user', content: '原始问题' },
        { id: 'a1', role: 'assistant', content: '旧回答' },
      ],
    })

    const user = userEvent.setup()
    render(<MessageBubble message={{ id: 'a1', role: 'assistant', content: '旧回答' }} />)
    await user.click(screen.getByLabelText('重新生成'))

    await waitFor(() =>
      expect(useChatStore.getState().messages[1]?.content).toBe('新的回答'),
    )
    const messages = useChatStore.getState().messages
    expect(messages).toHaveLength(2) // user + 新 assistant，旧回答被覆盖
    expect(messages[0].content).toBe('原始问题')
    expect(messages[1].content).toBe('新的回答')
    expect(calls.some((call) => call.url.includes('/regenerate'))).toBe(true)
    expect(useChatStore.getState().streaming).toBe(false)
  })

  it('流式期间「重新生成」按钮禁用', () => {
    useChatStore.setState({ streaming: true })
    render(<MessageBubble message={{ id: 'a1', role: 'assistant', content: '答' }} />)
    expect(screen.getByLabelText('重新生成')).toBeDisabled()
  })
})
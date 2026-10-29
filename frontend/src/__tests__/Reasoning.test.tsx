import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { MessageBubble } from '../components/MessageBubble'
import { useChatStore } from '../store/chatStore'
import type { ChatMessage } from '../types'

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

const FRAMES = [
  'data: {"type":"message_start","session_id":"s1"}\n\n',
  'data: {"type":"reasoning_delta","text":"先把两个数都写成同样的小数位数："}\n\n',
  'data: {"type":"reasoning_delta","text":"9.11 与 9.90。"}\n\n',
  'data: {"type":"text_delta","delta":"9.11 比 9.9 大"}\n\n',
  'data: {"type":"text_delta","delta":"（其实更小）"}\n\n',
  'data: {"type":"message_end","content":"9.11 比 9.9 大（其实更小）","reasoning":"先把两个数都写成同样的小数位数：9.11 与 9.90。","usage":{"prompt_tokens":5,"completion_tokens":6,"total_tokens":11}}\n\n',
]

function stubFetch() {
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if (url.includes('/api/sessions') && init?.method === 'POST') {
        return jsonResponse(
          {
            id: 's1',
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
      if (url.startsWith('/api/chat/stream')) {
        return new Response(sseStream(FRAMES), {
          status: 200,
          headers: { 'Content-Type': 'text/event-stream' },
        })
      }
      if (url.startsWith('/api/sessions')) return jsonResponse({ sessions: [] })
      return jsonResponse({}, 404)
    }),
  )
}

describe('思维链（reasoning）', () => {
  beforeEach(() => {
    useChatStore.getState().reset()
    vi.unstubAllGlobals()
  })

  it('reasoning_delta 增量累积到当前 assistant 消息', async () => {
    const observed: string[] = []
    const unsubscribe = useChatStore.subscribe((state) => {
      const last = state.messages[state.messages.length - 1]
      if (last?.reasoning && last.reasoning !== observed[observed.length - 1]) {
        observed.push(last.reasoning)
      }
    })
    stubFetch()

    await useChatStore.getState().sendMessage('9.11 和 9.9 哪个大')
    unsubscribe()

    const message = useChatStore.getState().messages[1]
    expect(message.reasoning).toBe('先把两个数都写成同样的小数位数：9.11 与 9.90。')
    expect(message.reasoningStreaming).toBe(false) // message_end 后收尾
    // 是逐帧累加的（至少 2 个中间态），不是最后一次性写入
    expect(observed.length).toBeGreaterThanOrEqual(2)
    expect(observed[0]).toBe('先把两个数都写成同样的小数位数：')
  })

  it('正文仍然是逐字增量（与 reasoning 互不干扰）', async () => {
    stubFetch()
    await useChatStore.getState().sendMessage('9.11 和 9.9 哪个大')
    const message = useChatStore.getState().messages[1]
    expect(message.content).toBe('9.11 比 9.9 大（其实更小）')
    expect(message.usage?.total_tokens).toBe(11)
  })

  it('渲染：思考中显示「深度思考中…」并自动展开', () => {
    const message: ChatMessage = {
      id: 'm1',
      role: 'assistant',
      content: '',
      reasoning: '正在推理…',
      reasoningStreaming: true,
    }
    const { container } = render(<MessageBubble message={message} />)
    expect(container.textContent).toContain('深度思考中')
    expect(container.textContent).toContain('正在推理…') // 自动展开
  })

  it('渲染：思考结束后折叠为「已深度思考（N 字）」，点击可展开回看', async () => {
    const user = userEvent.setup()
    const message: ChatMessage = {
      id: 'm1',
      role: 'assistant',
      content: '最终答案',
      reasoning: '这是一段很长的思考过程',
      reasoningStreaming: false,
    }
    const { container } = render(<MessageBubble message={message} />)

    expect(container.textContent).toContain('已深度思考（11 字）')
    // 默认收起：看不到思考内容，但正文在
    expect(container.textContent).not.toContain('这是一段很长的思考过程')
    expect(container.textContent).toContain('最终答案')

    await user.click(screen.getByLabelText('已深度思考'))
    await waitFor(() => expect(container.textContent).toContain('这是一段很长的思考过程'))
  })

  it('点历史会话：从 GET /api/sessions/{id} 的 reasoning_content 还原折叠面板', async () => {
    vi.unstubAllGlobals()
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input)
        if (url.includes('/api/sessions/s-old')) {
          return jsonResponse({
            session: {
              id: 's-old',
              title: '数字大小比较',
              model: 'qwen3.8-flash',
              created_at: '',
              updated_at: '',
              message_count: 2,
              total_tokens: 916,
            },
            messages: [
              { role: 'user', content: '9.11 和 9.9 哪个大' },
              {
                role: 'assistant',
                content: '9.9 更大',
                reasoning_content: '先把两个数写成同样的小数位数：9.11 与 9.90。',
              },
            ],
          })
        }
        return jsonResponse({ sessions: [] })
      }),
    )

    await useChatStore.getState().openSession('s-old')
    const messages = useChatStore.getState().messages
    expect(messages[1].reasoning).toBe('先把两个数写成同样的小数位数：9.11 与 9.90。')

    const { container } = render(<MessageBubble message={messages[1]} />)
    expect(container.textContent).toMatch(/已深度思考（\d+ 字）/)
  })

  it('没有 reasoning 的消息不渲染思考面板', () => {
    const message: ChatMessage = { id: 'm1', role: 'assistant', content: '普通回答' }
    const { container } = render(<MessageBubble message={message} />)
    expect(screen.queryByLabelText('已深度思考')).not.toBeInTheDocument()
    expect(screen.queryByLabelText('深度思考中')).not.toBeInTheDocument()
    expect(container.textContent).toContain('普通回答')
  })

  it('用户消息永不渲染思考面板', () => {
    const message: ChatMessage = {
      id: 'm1',
      role: 'user',
      content: '你好',
      reasoning: '不该显示',
      reasoningStreaming: true,
    }
    const { container } = render(<MessageBubble message={message} />)
    expect(container.textContent).not.toContain('不该显示')
  })
})
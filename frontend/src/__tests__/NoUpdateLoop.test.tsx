import { act, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import App from '../App'
import { useChatStore } from '../store/chatStore'

function jsonResponse(body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { 'Content-Type': 'application/json' },
  })
}

/** 模拟真实浏览器：scrollTo 之后会异步派发 scroll 事件（这是 JSdom 不会做的）。 */
function installScrolling(el: HTMLElement) {
  let scrollTop = 0
  let scrollHeight = 1000
  const listeners: Array<() => void> = []
  Object.defineProperty(el, 'scrollHeight', { configurable: true, get: () => scrollHeight })
  Object.defineProperty(el, 'clientHeight', { configurable: true, get: () => 600 })
  Object.defineProperty(el, 'scrollTop', {
    configurable: true,
    get: () => scrollTop,
    set: (v: number) => {
      scrollTop = v
    },
  })
  el.scrollTo = ((opts?: ScrollToOptions) => {
    scrollTop = Math.min(opts?.top ?? 0, scrollHeight - 600)
    // 浏览器行为：滚动后派发 scroll 事件（先试同步，最坏情况）
    listeners.forEach((fn) => fn())
  }) as typeof el.scrollTo
  const onScroll = () => listeners.forEach((fn) => fn())
  el.addEventListener('scroll', onScroll)
  return {
    grow: (delta: number) => {
      scrollHeight += delta
    },
    listener: (fn: () => void) => listeners.push(fn),
  }
}

const FRAMES = 100

function stubStream() {
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      if (url.endsWith('/api/chat/stream')) {
        const encoder = new TextEncoder()
        const body = new ReadableStream({
          start(controller) {
            const parts: string[] = []
            parts.push('data: {"type":"message_start"}\n\n')
            for (let index = 0; index < FRAMES; index += 1) {
              parts.push(
                `data: ${JSON.stringify({ type: 'text_delta', delta: `第 ${index} 行内容，用来把页面撑长。\n` })}\n\n`,
              )
            }
            parts.push('data: {"type":"message_end","content":"done"}\n\n')
            for (const part of parts) controller.enqueue(encoder.encode(part))
            controller.close()
          },
        })
        return new Response(body, { status: 200, headers: { 'Content-Type': 'text/event-stream' } })
      }
      if (url.endsWith('/api/models')) return jsonResponse({ models: [], default: null, base_url: '' })
      if (url.endsWith('/api/sessions')) return jsonResponse({ sessions: [] })
      if (url.includes('/api/kb/')) return jsonResponse({ docs: [], chunks: 0, mode: 'keyword', supported: [], max_file_mb: 10, pipeline: '' })
      if (url.endsWith('/api/config/workspace'))
        return jsonResponse({
          workspace: 'D:\\MyAi\\workspace',
          default_workspace: 'D:\\MyAi\\workspace',
          is_default: true,
          extras: [],
        })
      return jsonResponse({})
    }),
  )
}

describe('流式渲染稳定性（防无限更新）', () => {
  let consoleError: ReturnType<typeof vi.spyOn>

  beforeEach(() => {
    useChatStore.getState().reset()
    consoleError = vi.spyOn(console, 'error').mockImplementation(() => undefined)
  })
  afterEach(() => {
    consoleError.mockRestore()
    vi.unstubAllGlobals()
  })

  it('100 帧流式 + 真实 scroll 事件不会触发 Maximum update depth', async () => {
    stubStream()
    const { container } = render(<App />)

    const scroller = container.querySelector('[data-testid="chat-scroll"]')
    if (scroller) {
      const scrolling = installScrolling(scroller as HTMLElement)
      scrolling.listener(() => {
        // 模拟内容持续变长
        scrolling.grow(40)
      })
    }

    await act(async () => {
      useChatStore.setState({ model: 'deepseek' })
      await useChatStore.getState().sendMessage('写 100 行')
    })

    const messages = consoleError.mock.calls
      .map((call: unknown[]) => String(call[0] ?? ''))
      .filter((text: string) => text.includes('Maximum update depth'))
    expect(messages).toEqual([])
    expect(screen.queryByText(/Maximum update depth/)).not.toBeInTheDocument()
  })
})
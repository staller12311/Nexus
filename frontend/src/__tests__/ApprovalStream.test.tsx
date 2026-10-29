import { act, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import App from '../App'
import { useChatStore } from '../store/chatStore'

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

const LONG_CODE = 'print("start")\n' + Array.from({ length: 280 }, (_, i) => `value_${i} = "line_${i}"`).join('\n')
const APPROVAL_ARGS = JSON.stringify({ code: LONG_CODE })
const STREAM_FRAMES = [
  { type: 'message_start', session_id: 's1' },
  { type: 'reasoning_delta', text: '先读文件…' },
  { type: 'tool_call_start', tool_call_id: 'c1', name: 'run_python_code', arguments: APPROVAL_ARGS },
  {
    type: 'approval_required',
    approval_id: 'ap_1',
    session_id: 's1',
    tool_call_id: 'c1',
    name: 'run_python_code',
    arguments: APPROVAL_ARGS,
    expires_in: 300,
  },
]

describe('审批卡 + 流式渲染稳定性', () => {
  let consoleError: ReturnType<typeof vi.spyOn>
  let streamController: ReadableStreamDefaultController<Uint8Array> | null = null

  beforeEach(() => {
    useChatStore.getState().reset()
    consoleError = vi.spyOn(console, 'error').mockImplementation(() => undefined)
    streamController = null

    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input)
        if (url.endsWith('/api/chat/stream')) {
          const encoder = new TextEncoder()
          return new Response(
            new ReadableStream<Uint8Array>({
              start(controller) {
                streamController = controller
                for (const frame of STREAM_FRAMES) {
                  controller.enqueue(encoder.encode(`data: ${JSON.stringify(frame)}\n\n`))
                }
              },
            }),
            { status: 200, headers: { 'Content-Type': 'text/event-stream' } },
          )
        }
        if (url.endsWith('/api/chat/approve')) {
          return jsonResponse({ ok: true, approval_id: 'ap_1', approved: true })
        }
        if (url.endsWith('/api/models')) return jsonResponse({ models: [], default: null, base_url: '' })
        if (url.endsWith('/api/sessions')) return jsonResponse({ sessions: [] })
        if (url.includes('/api/kb/')) {
          return jsonResponse({ docs: [], chunks: 0, mode: 'keyword', supported: [], max_file_mb: 10, pipeline: '' })
        }
        if (url.endsWith('/api/config/workspace')) {
          return jsonResponse({ workspace: 'D:\\MyAi\\workspace', default_workspace: 'D:\\MyAi\\workspace', is_default: true, extras: [] })
        }
        return jsonResponse({})
      }),
    )
  })

  afterEach(() => {
    consoleError.mockRestore()
    vi.unstubAllGlobals()
  })

  it('审批卡展示期间持续流式增量不会触发 Maximum update depth', async () => {
    const user = userEvent.setup()
    render(<App />)

    act(() => {
      useChatStore.setState({ model: 'deepseek' })
      void useChatStore.getState().sendMessage('写商城页面')
    })

    expect(await screen.findByTestId('approval-card-c1')).toBeInTheDocument()

    // 审批卡还在等用户操作时，模拟后续事件（真实顺序里审批通过后才有 end，但这里压测渲染稳定性）
    const encoder = new TextEncoder()
    await act(async () => {
      for (let index = 0; index < 80; index += 1) {
        streamController?.enqueue(
          encoder.encode(
            `data: ${JSON.stringify({ type: 'text_delta', delta: `第 ${index} 段文字\n` })}\n\n`,
          ),
        )
        await Promise.resolve()
      }
    })

    await user.click(screen.getByRole('button', { name: '同意执行' }))
    await act(async () => {
      streamController?.enqueue(
        encoder.encode(
          `data: ${JSON.stringify({ type: 'tool_call_end', tool_call_id: 'c1', name: 'run_python_code', ok: true, content: '退出码：0' })}\n\n`,
        ),
      )
      streamController?.enqueue(encoder.encode('data: {"type":"message_end","content":"完成"}\n\n'))
      streamController?.close()
    })

    const errors = consoleError.mock.calls
      .map((call: unknown[]) => String(call[0] ?? ''))
      .filter((text: string) => text.includes('Maximum update depth'))
    expect(errors).toEqual([])
  })
})
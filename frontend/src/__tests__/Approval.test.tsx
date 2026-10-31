import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { ApprovalCard } from '../components/ApprovalCard'
import { ToolCard } from '../components/ToolCard'
import { useChatStore } from '../store/chatStore'
import type { ChatMessage, ToolCallView } from '../types'

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
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

function writeCall(overrides: Partial<ToolCallView> = {}): ToolCallView {
  return {
    id: 'call_1',
    name: 'write_file',
    arguments: JSON.stringify({ path: 'src/app.py', content: 'print("line one")\nprint("line two")' }),
    status: 'pending',
    approval: { id: 'ap_1', status: 'pending', expiresIn: 300 },
    ...overrides,
  }
}

describe('富文本审批卡片', () => {
  beforeEach(() => {
    useChatStore.getState().reset()
    vi.unstubAllGlobals()
  })

  it('write_file：黄色卡片 + 路径 + 只读代码预览 + 两个按钮', () => {
    render(<ApprovalCard call={writeCall()} />)

    expect(screen.getByTestId('approval-card-call_1')).toBeInTheDocument()
    expect(screen.getByText('AI 想写入文件')).toBeInTheDocument()
    expect(screen.getByText('src/app.py')).toBeInTheDocument()
    expect(screen.getByText(/需要审批/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '同意执行' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '拒绝' })).toBeInTheDocument()
    // 代码预览真的渲染出来了（CodeBlock 的只读内容）
    expect(screen.getByTestId('approval-card-call_1').textContent).toContain('line one')
    expect(screen.getByTestId('approval-card-call_1').textContent).toContain('line two')
  })

  it('delete_path / move_path：展示不可逆提醒与源/目标路径', () => {
    const { unmount } = render(
      <ApprovalCard
        call={writeCall({
          name: 'delete_path',
          arguments: JSON.stringify({ path: 'tmp/old.txt' }),
        })}
      />,
    )
    expect(screen.getByText('AI 想删除文件或空目录')).toBeInTheDocument()
    expect(screen.getByText('tmp/old.txt')).toBeInTheDocument()
    expect(screen.getByText(/不可撤销/)).toBeInTheDocument()
    unmount()

    render(
      <ApprovalCard
        call={writeCall({
          name: 'move_path',
          arguments: JSON.stringify({ src: 'a.txt', dest: 'b.txt' }),
        })}
      />,
    )
    expect(screen.getByText('AI 想移动 / 重命名')).toBeInTheDocument()
    expect(screen.getByText('a.txt')).toBeInTheDocument()
    expect(screen.getByText('b.txt')).toBeInTheDocument()
  })

  it('超过 200 行折叠，但提示同意后会完整写入', () => {
    const content = Array.from({ length: 260 }, (_, index) => `line_${index}`).join('\n')
    render(
      <ApprovalCard
        call={writeCall({ arguments: JSON.stringify({ path: 'big.py', content }) })}
      />,
    )
    expect(screen.getByText(/仅预览前 200 行；还有 60 行/)).toBeInTheDocument()
  })

  it('点「拒绝」：POST 审批接口 approved=false，卡片转为已拒绝', async () => {
    const calls: Array<{ url: string; body?: string }> = []
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        calls.push({ url: String(input), body: String(init?.body ?? '') })
        return jsonResponse({ ok: true, approval_id: 'ap_1', approved: false })
      }),
    )
    useChatStore.setState({
      messages: [
        {
          id: 'm1',
          role: 'assistant',
          content: '',
          toolCalls: [writeCall()],
        } as ChatMessage,
      ],
    })

    const user = userEvent.setup()
    render(<ApprovalCard call={writeCall()} />)
    await user.click(screen.getByRole('button', { name: '拒绝' }))

    await waitFor(() =>
      expect(useChatStore.getState().messages[0].toolCalls?.[0].approval?.status).toBe(
        'rejected',
      ),
    )
    expect(calls[0].url).toBe('/api/chat/approve')
    expect(JSON.parse(calls[0].body ?? '{}')).toMatchObject({
      approval_id: 'ap_1',
      approved: false,
    })
  })

  it('审批接口返回 409：卡片标记为过期并给出提示，不抛异常', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => jsonResponse({ detail: '审批已处理过：ap_1' }, 409)),
    )
    useChatStore.setState({
      messages: [
        { id: 'm1', role: 'assistant', content: '', toolCalls: [writeCall()] } as ChatMessage,
      ],
    })

    const user = userEvent.setup()
    render(<ApprovalCard call={writeCall()} />)
    await user.click(screen.getByRole('button', { name: '同意执行' }))

    await waitFor(() =>
      expect(useChatStore.getState().messages[0].toolCalls?.[0].approval?.status).toBe(
        'expired',
      ),
    )
    expect(useChatStore.getState().notice).toContain('审批未生效')
  })
})

describe('审批事件与工具卡片', () => {
  beforeEach(() => {
    useChatStore.getState().reset()
    vi.unstubAllGlobals()
  })

  it('SSE approval_required 会给对应工具卡片挂上待审批状态', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input)
        if (url.endsWith('/api/sessions') || url.includes('/api/sessions?')) {
          return jsonResponse({ id: 's1', sessions: [] })
        }
        if (url.includes('/api/chat/stream')) {
          return new Response(
            sseStream([
              'data: {"type":"message_start","session_id":"s1"}\n\n',
              'data: {"type":"tool_call_start","tool_call_id":"call_1","name":"write_file","arguments":"{\\"path\\":\\"a.txt\\"}"}\n\n',
              'data: {"type":"approval_required","approval_id":"ap_1","tool_call_id":"call_1","name":"write_file","arguments":"{\\"path\\":\\"a.txt\\"}","expires_in":300}\n\n',
              'data: {"type":"tool_call_end","tool_call_id":"call_1","name":"write_file","ok":false,"content":"用户已拒绝此操作，工具未执行。"}\n\n',
              'data: {"type":"message_end","content":"已取消","usage":{"prompt_tokens":1,"completion_tokens":1,"total_tokens":2,"cache_hit_tokens":0}}\n\n',
            ]),
            { status: 200, headers: { 'Content-Type': 'text/event-stream' } },
          )
        }
        return jsonResponse({ sessions: [] })
      }),
    )

    await useChatStore.getState().sendMessage('写个文件')

    const assistant = useChatStore.getState().messages[1]
    expect(assistant.toolCalls?.[0].approval).toMatchObject({
      id: 'ap_1',
      status: 'pending',
    })
  })

  it('run_python_code：鲜红代码块 + 完整代码 + 强制警告', () => {
    const code = Array.from({ length: 240 }, (_, index) => `value_${index} = ${index}`).join('\n')
    render(
      <ApprovalCard
        call={writeCall({
          name: 'run_python_code',
          arguments: JSON.stringify({ code }),
        })}
      />,
    )

    expect(screen.getByText('AI 想执行 Python 代码')).toBeInTheDocument()
    expect(screen.getByTestId('danger-warning')).toHaveTextContent(
      '🚨 警告：这是一段可执行代码，请在确认安全后点击同意。',
    )
    expect(screen.getByTestId('danger-code')).toHaveTextContent('value_239 = 239')
    expect(screen.getByTestId('danger-code').querySelector('[data-danger="true"]')).not.toBeNull()
    expect(screen.getByText(/进程级轻量沙箱，非 OS 隔离/)).toBeInTheDocument()
  })

  it('run_shell：展示完整命令而不是 JSON 参数', () => {
    const command = 'echo one && echo two | findstr two'
    render(
      <ApprovalCard
        call={writeCall({
          name: 'run_shell',
          arguments: JSON.stringify({ command }),
        })}
      />,
    )

    expect(screen.getByText('AI 想执行 Shell 命令')).toBeInTheDocument()
    expect(screen.getByTestId('danger-warning')).toBeInTheDocument()
    expect(screen.getByTestId('danger-code')).toHaveTextContent(command)
    expect(screen.queryByText(/"command"/)).not.toBeInTheDocument()
  })

  it('待审批时渲染 ApprovalCard；决定后渲染普通工具卡 + 审批徽标', () => {
    const pending = writeCall({ status: 'pending' })
    const { unmount } = render(<ToolCard call={pending} />)
    expect(screen.getByTestId('approval-card-call_1')).toBeInTheDocument()
    unmount()

    const decided = writeCall({
      status: 'success',
      content: '已写入 src/app.py',
      approval: { id: 'ap_1', status: 'approved' },
    })
    render(<ToolCard call={decided} />)
    expect(screen.queryByTestId('approval-card-call_1')).not.toBeInTheDocument()
    expect(screen.getByText('已同意')).toBeInTheDocument()
    expect(screen.getByRole('button')).toHaveAttribute('aria-expanded', 'false')
  })
})
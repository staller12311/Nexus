import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { Sidebar } from '../components/Sidebar'
import { useChatStore } from '../store/chatStore'
import type { ApiMessage, SessionInfo } from '../types'

const sessionA: SessionInfo = {
  id: 'aaaa1111',
  title: '',
  model: 'webmock',
  created_at: '2026-10-27T08:00:00+00:00',
  updated_at: '2026-10-27T09:15:00+00:00',
  message_count: 5,
  total_tokens: 36,
  first_user_message: '读一下 readme.txt 的内容',
}

const sessionB: SessionInfo = {
  ...sessionA,
  id: 'bbbb2222',
  first_user_message: '',
  message_count: 0,
  total_tokens: 0,
}

const historyMessages: ApiMessage[] = [
  { role: 'system', content: '你是助手' },
  { role: 'user', content: '读一下 readme.txt 的内容' },
  {
    role: 'assistant',
    content: null,
    tool_calls: [
      {
        id: 'call_read',
        type: 'function',
        function: { name: 'read_file', arguments: '{"path": "readme.txt"}' },
      },
    ],
  },
  { role: 'tool', tool_call_id: 'call_read', content: '# readme.txt（2 字节）\nhi' },
  { role: 'assistant', content: 'readme.txt 里写的是：hi' },
]

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

describe('侧边栏会话管理', () => {
  beforeEach(() => {
    useChatStore.getState().reset()
    vi.unstubAllGlobals()
    useChatStore.getState().setSessions([sessionA, sessionB])
    useChatStore.getState().setModel('webmock')
  })

  it('标题：空 title 用第一条 user 消息（截断），没有则显示「新会话」，不显示 UUID', () => {
    render(<Sidebar />)
    expect(screen.getByText('读一下 readme.txt…')).toBeInTheDocument()
    expect(screen.getByText('新会话')).toBeInTheDocument()
    expect(screen.queryByText(/aaaa1111/)).not.toBeInTheDocument()
    expect(screen.queryByText(/bbbb2222/)).not.toBeInTheDocument()
  })

  it('点击历史会话：拉详情、还原出工具卡片、高亮选中项', async () => {
    const user = userEvent.setup()
    const calls: string[] = []
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input)
        calls.push(url)
        return jsonResponse({ session: sessionA, messages: historyMessages })
      }),
    )

    render(<Sidebar />)
    await user.click(screen.getByText('读一下 readme.txt…'))

    await waitFor(() => expect(useChatStore.getState().sessionId).toBe('aaaa1111'))
    expect(calls.some((url) => url.includes('/api/sessions/aaaa1111'))).toBe(true)

    const messages = useChatStore.getState().messages
    expect(messages).toHaveLength(3)
    expect(messages[1].toolCalls?.[0]).toMatchObject({
      id: 'call_read',
      name: 'read_file',
      status: 'success',
    })
    expect(messages[2].content).toBe('readme.txt 里写的是：hi')

    // 高亮：选中项带 aria-current
    const active = screen.getByText('读一下 readme.txt…').closest('button')
    expect(active).toHaveAttribute('aria-current', 'true')
  })

  it('正在生成时点击会话：不改 sessionId，给出「请稍候」提示', async () => {
    const user = userEvent.setup()
    const calls: string[] = []
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL) => {
        calls.push(String(input))
        return jsonResponse({ session: sessionA, messages: historyMessages })
      }),
    )
    useChatStore.setState({ streaming: true })

    render(<Sidebar />)
    await user.click(screen.getByText('读一下 readme.txt…'))

    expect(useChatStore.getState().sessionId).toBeNull()
    expect(useChatStore.getState().error).toContain('请稍候')
    // 知识库面板自己会拉 /api/kb/*，这里只关心"没有发起会话请求"
    expect(calls.filter((url) => !url.includes('/api/kb/'))).toHaveLength(0)
  })

  it('新建会话：POST 建会话、清空主区、刷新列表并高亮新会话', async () => {
    const user = userEvent.setup()
    const created = { ...sessionB, id: 'cccc3333' }
    const calls: string[] = []
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input)
        calls.push(`${init?.method ?? 'GET'} ${url}`)
        if (url.endsWith('/api/sessions') && init?.method === 'POST') {
          return jsonResponse(created, 201)
        }
        if (url.includes('/api/sessions')) {
          return jsonResponse({ sessions: [created, sessionA, sessionB] })
        }
        return jsonResponse({}, 404)
      }),
    )

    useChatStore.setState({ messages: [{ id: 'old', role: 'user', content: '旧消息' }] })
    render(<Sidebar />)
    await user.click(screen.getByRole('button', { name: /新建会话/ }))

    await waitFor(() => expect(useChatStore.getState().sessionId).toBe('cccc3333'))
    expect(calls).toContain('POST /api/sessions')
    expect(calls.some((call) => call === 'GET /api/sessions?limit=20')).toBe(true)
    expect(useChatStore.getState().messages).toEqual([])
    expect(useChatStore.getState().sessions).toHaveLength(3)

    const active = screen.getAllByText('新会话')[0].closest('button')
    expect(active).toHaveAttribute('aria-current', 'true')
  })

  it('相对时间：5 分钟前 / 今天 HH:MM 出现在列表项里', () => {
    const now = Date.now()
    useChatStore.getState().setSessions([
      { ...sessionA, updated_at: new Date(now - 5 * 60 * 1000).toISOString() },
      { ...sessionB, updated_at: new Date(now - 3 * 3600 * 1000).toISOString() },
    ])
    render(<Sidebar />)
    expect(screen.getByText(/5 分钟前 · 5 条 · 36 tokens/)).toBeInTheDocument()
    expect(screen.getByText(/之前|今天/)).toBeInTheDocument()
  })
})
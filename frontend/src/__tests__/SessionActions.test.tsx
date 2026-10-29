import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { Sidebar } from '../components/Sidebar'
import { useChatStore } from '../store/chatStore'
import type { SessionInfo } from '../types'

function makeSession(id: string, title: string, count = 3): SessionInfo {
  return {
    id,
    title,
    model: 'webmock',
    created_at: '2026-10-27T08:00:00+00:00',
    updated_at: '2026-10-27T09:15:00+00:00',
    message_count: count,
    total_tokens: 36,
    first_user_message: '',
  }
}

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

describe('重命名与删除', () => {
  beforeEach(() => {
    useChatStore.getState().reset()
    vi.unstubAllGlobals()
    useChatStore.getState().setModel('webmock')
    useChatStore.getState().setSessions([makeSession('aaaa1111', '第一个会话'), makeSession('bbbb2222', '')])
  })
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('悬浮区有重命名与删除两个按钮', () => {
    render(<Sidebar />)
    expect(screen.getByLabelText('重命名 第一个会话')).toBeInTheDocument()
    expect(screen.getByLabelText('删除 第一个会话')).toBeInTheDocument()
  })

  it('就地重命名：输入框预填，Enter 保存并发 PATCH', async () => {
    const user = userEvent.setup()
    const calls: Array<{ url: string; method?: string; body?: string }> = []
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input)
        calls.push({ url, method: init?.method, body: String(init?.body ?? '') })
        if (init?.method === 'PATCH') {
          return jsonResponse({ ...makeSession('aaaa1111', '新名字') })
        }
        return jsonResponse({ sessions: [makeSession('aaaa1111', '新名字'), makeSession('bbbb2222', '')] })
      }),
    )

    render(<Sidebar />)
    await user.click(screen.getByLabelText('重命名 第一个会话'))

    const input = screen.getByLabelText('会话标题')
    expect(input).toHaveValue('第一个会话')

    await user.clear(input)
    await user.type(input, '新名字{Enter}')

    await waitFor(() =>
      expect(useChatStore.getState().sessions[0].title).toBe('新名字'),
    )
    const patch = calls.find((call) => call.method === 'PATCH')
    expect(patch?.url).toBe('/api/sessions/aaaa1111')
    expect(JSON.parse(patch!.body!)).toEqual({ title: '新名字' })
    expect(useChatStore.getState().notice).toBe('已重命名')
  })

  it('Esc 取消重命名：不保存、不改标题', async () => {
    const user = userEvent.setup()
    const calls: string[] = []
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL) => {
        calls.push(String(input))
        return jsonResponse({ sessions: [] })
      }),
    )

    render(<Sidebar />)
    await user.click(screen.getByLabelText('重命名 第一个会话'))
    const input = screen.getByLabelText('会话标题')
    await user.clear(input)
    await user.type(input, '不该保存{Escape}')
    expect(input).not.toBeInTheDocument()

    expect(useChatStore.getState().sessions[0].title).toBe('第一个会话')
    expect(calls.some((url) => url.includes('PATCH'))).toBe(false)
    expect(calls.some((url) => url.includes('aaaa1111') && !url.includes('?'))).toBe(false)
  })

  it('删除需要二次确认：点垃圾桶只弹确认框，确认后才发 DELETE', async () => {
    const user = userEvent.setup()
    const calls: Array<{ url: string; method?: string }> = []
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input)
        calls.push({ url, method: init?.method })
        if (init?.method === 'DELETE') {
          return jsonResponse({ deleted: true, session: makeSession('aaaa1111', '第一个会话') })
        }
        return jsonResponse({ sessions: [makeSession('bbbb2222', '')] })
      }),
    )

    render(<Sidebar />)
    await user.click(screen.getByLabelText('删除 第一个会话'))

    // 还没确认：不应对后端发任何 DELETE
    expect(screen.getByRole('dialog', { name: '确认删除会话' })).toBeInTheDocument()
    expect(calls.some((call) => call.method === 'DELETE')).toBe(false)

    await user.click(screen.getByRole('button', { name: '删除' }))

    await waitFor(() =>
      expect(useChatStore.getState().sessions.map((item) => item.id)).toEqual(['bbbb2222']),
    )
    const del = calls.find((call) => call.method === 'DELETE')
    expect(del?.url).toBe('/api/sessions/aaaa1111')
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    expect(useChatStore.getState().notice).toBe('已删除会话')
  })

  it('取消删除：不发请求、列表不变', async () => {
    const user = userEvent.setup()
    const calls: string[] = []
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL) => {
        calls.push(String(input))
        return jsonResponse({ sessions: [] })
      }),
    )

    render(<Sidebar />)
    await user.click(screen.getByLabelText('删除 第一个会话'))
    await user.click(screen.getByRole('button', { name: '取消' }))

    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    expect(useChatStore.getState().sessions).toHaveLength(2)
    expect(calls.some((url) => url.includes('DELETE'))).toBe(false)
  })

  it('删掉当前打开的会话 → 主区清空、sessionId 归零', async () => {
    const user = userEvent.setup()
    useChatStore.setState({
      sessionId: 'aaaa1111',
      messages: [{ id: 'm1', role: 'user', content: '你好' }],
      systemPrompt: '旧提示词',
    })
    vi.stubGlobal(
      'fetch',
      vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
        if (init?.method === 'DELETE') {
          return jsonResponse({ deleted: true, session: makeSession('aaaa1111', '第一个会话') })
        }
        return jsonResponse({ sessions: [] })
      }),
    )

    render(<Sidebar />)
    await user.click(screen.getByLabelText('删除 第一个会话'))
    await user.click(screen.getByRole('button', { name: '删除' }))

    await waitFor(() => expect(useChatStore.getState().sessionId).toBeNull())
    expect(useChatStore.getState().messages).toEqual([])
    expect(useChatStore.getState().systemPrompt).toBe('')
  })

  it('改名后标题优先用 title，而不是第一条消息', () => {
    useChatStore.getState().setSessions([makeSession('cccc3333', '人工起的名字')])
    render(<Sidebar />)
    expect(screen.getByText('人工起的名字')).toBeInTheDocument()
  })
})
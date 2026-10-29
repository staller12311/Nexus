import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { Sidebar } from '../components/Sidebar'
import { useChatStore } from '../store/chatStore'
import type { SessionInfo } from '../types'

const session: SessionInfo = {
  id: 'aaaa1111',
  title: '',
  model: 'webmock',
  created_at: '2026-10-27T08:00:00+00:00',
  updated_at: '2026-10-27T09:15:00+00:00',
  message_count: 5,
  total_tokens: 36,
  first_user_message: '读一下 readme.txt',
}

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

describe('系统提示词设置', () => {
  beforeEach(() => {
    useChatStore.getState().reset()
    vi.unstubAllGlobals()
    useChatStore.getState().setSessions([session])
    useChatStore.getState().setModel('webmock')
  })
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('没有会话时齿轮依然可点，保存会自动先建会话再 PATCH', async () => {
    const user = userEvent.setup()
    const calls: Array<{ url: string; method?: string }> = []
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input)
        calls.push({ url, method: init?.method })
        if (init?.method === 'POST') {
          return jsonResponse({ ...session, id: 'NEW12345' }, 201)
        }
        if (url.includes('/system_prompt')) {
          return jsonResponse({
            session_id: 'NEW12345',
            system_prompt: '新提示词',
            created: true,
            seq: 0,
          })
        }
        return jsonResponse({ sessions: [session] })
      }),
    )

    render(<Sidebar />)
    expect(screen.getByLabelText('设置')).toBeEnabled()

    await user.click(screen.getByLabelText('设置'))
    expect(screen.getByText(/自动新建一个会话/)).toBeInTheDocument()
    await user.type(screen.getByRole('textbox'), '新提示词')
    await user.click(screen.getByRole('button', { name: '保存' }))

    await waitFor(() => expect(useChatStore.getState().systemPrompt).toBe('新提示词'))
    // 先 POST 建会话，再 PATCH（忽略知识库面板自己的 /api/kb/* 请求）
    const business = calls.filter((call) => !call.url.includes('/api/kb/'))
    expect(business[0]).toMatchObject({ url: '/api/sessions', method: 'POST' })
    expect(calls.some((call) => call.url === '/api/sessions/NEW12345/system_prompt')).toBe(true)
    expect(useChatStore.getState().sessionId).toBe('NEW12345')
  })

  it('打开弹窗后预填当前提示词，保存时 PATCH 正确路径与 body', async () => {
    const user = userEvent.setup()
    useChatStore.setState({ sessionId: 'aaaa1111', systemPrompt: '你是配置里的默认提示词。' })

    const calls: Array<{ url: string; method?: string; body?: string }> = []
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input)
        calls.push({ url, method: init?.method, body: String(init?.body ?? '') })
        // 列表刷新（GET /sessions）要和 PATCH 区分开，否则 store 会拿到错形状
        if (url.includes('/api/sessions?') || url.endsWith('/api/sessions')) {
          return jsonResponse({ sessions: [session] })
        }
        return jsonResponse({
          session_id: 'aaaa1111',
          system_prompt: '你是一个不讲人话、满口文言文的学者。',
          created: false,
          seq: -1,
        })
      }),
    )

    render(<Sidebar />)
    await user.click(screen.getByLabelText('设置'))

    const textarea = screen.getByRole('textbox')
    expect(textarea).toHaveValue('你是配置里的默认提示词。')

    await user.clear(textarea)
    await user.type(textarea, '你是一个不讲人话、满口文言文的学者。')
    await user.click(screen.getByRole('button', { name: '保存' }))

    await waitFor(() => expect(useChatStore.getState().systemPrompt).toContain('文言文'))

    const patch = calls.find((call) => call.method === 'PATCH')
    expect(patch?.url).toBe('/api/sessions/aaaa1111/system_prompt')
    expect(JSON.parse(patch!.body!)).toEqual({
      system_prompt: '你是一个不讲人话、满口文言文的学者。',
    })

    // 保存成功后弹窗关闭，并给出「下一句对话生效」提示
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    expect(useChatStore.getState().notice).toBe('已更新，下一句对话生效')
  })

  it('保存失败时保留弹窗并把错误交给 ErrorBanner', async () => {
    const user = userEvent.setup()
    useChatStore.setState({ sessionId: 'aaaa1111', systemPrompt: 'old' })
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => jsonResponse({ detail: '写入系统提示词失败：database is locked' }, 500)),
    )

    const { container } = render(<Sidebar />)
    // Sidebar 自己不含 ErrorBanner，但 store 的错误状态应被设置
    await user.click(screen.getByLabelText('设置'))
    await user.click(screen.getByRole('button', { name: '保存' }))

    await waitFor(() => expect(useChatStore.getState().error).toContain('database is locked'))
    expect(useChatStore.getState().streaming).toBe(false)
    expect(container.ownerDocument.body.textContent).toContain('系统提示词')
  })

  it('Esc 关闭弹窗、不保存', async () => {
    const user = userEvent.setup()
    useChatStore.setState({ sessionId: 'aaaa1111', systemPrompt: 'keep me' })
    const calls: string[] = []
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL) => {
        calls.push(String(input))
        return jsonResponse({})
      }),
    )

    render(<Sidebar />)
    await user.click(screen.getByLabelText('设置'))
    expect(screen.getByRole('dialog')).toBeInTheDocument()
    await user.keyboard('{Escape}')
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    // 知识库面板的 /api/kb/* 不算"保存请求"
    expect(calls.filter((url) => !url.includes('/api/kb/'))).toHaveLength(0)
  })

  it('openSession 会把会话的 system_prompt 填进 store', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () =>
        jsonResponse({
          session,
          messages: [
            { role: 'system', content: '会话自己的提示词' },
            { role: 'user', content: '你好' },
          ],
          system_prompt: '会话自己的提示词',
        }),
      ),
    )

    await useChatStore.getState().openSession('aaaa1111')
    expect(useChatStore.getState().systemPrompt).toBe('会话自己的提示词')
  })
})
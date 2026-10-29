import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { Composer } from '../components/Composer'
import { useChatStore } from '../store/chatStore'

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

const DEFAULT_WS = 'D:\\MyAi\\workspace'

function stubFetch(handler?: (url: string, init?: RequestInit) => Response | null) {
  const calls: Array<{ url: string; method?: string; body?: string }> = []
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      const method = init?.method ?? 'GET'
      calls.push({ url, method, body: String(init?.body ?? '') })
      const custom = handler?.(url, init)
      if (custom) return custom
      if (url.endsWith('/api/config/workspace')) {
        if (method === 'POST') {
          const body = JSON.parse(String(init?.body ?? '{}')) as { path?: string }
          return jsonResponse({
            workspace: body.path,
            default_workspace: DEFAULT_WS,
            is_default: body.path === DEFAULT_WS,
          })
        }
        if (method === 'DELETE') {
          return jsonResponse({ workspace: DEFAULT_WS, default_workspace: DEFAULT_WS, is_default: true })
        }
        return jsonResponse({ workspace: DEFAULT_WS, default_workspace: DEFAULT_WS, is_default: true })
      }
      if (url.endsWith('/api/kb/status')) {
        return jsonResponse({
          docs: 0,
          chunks: 0,
          mode: 'keyword',
          supported: ['.md', '.txt'],
          max_file_mb: 10,
          pipeline: 'BM25',
        })
      }
      if (url.endsWith('/api/kb/docs')) return jsonResponse({ docs: [] })
      return jsonResponse({ sessions: [] })
    }),
  )
  return calls
}

describe('工作区切换（Workspace Switcher）', () => {
  beforeEach(() => {
    useChatStore.getState().reset()
    vi.unstubAllGlobals()
  })
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('徽章可点击，点开后显示极简输入框与权限提示', async () => {
    stubFetch()
    const user = userEvent.setup()
    render(<Composer />)

    await user.click(screen.getByTestId('sandbox-badge'))

    expect(screen.getByRole('dialog', { name: '切换工作区' })).toBeInTheDocument()
    expect(screen.getByLabelText('工作区绝对路径')).toBeInTheDocument()
    expect(screen.getByText(/当前 AI 将获得主目录的读写权限/)).toBeInTheDocument()
    // 预填当前工作区
    await waitFor(() =>
      expect(screen.getByLabelText('工作区绝对路径')).toHaveValue(DEFAULT_WS),
    )
  })

  it('输入绝对路径并应用：调用 POST /api/config/workspace 并更新徽章提示', async () => {
    const calls = stubFetch()
    const user = userEvent.setup()
    render(<Composer />)

    await user.click(screen.getByTestId('sandbox-badge'))
    const input = screen.getByLabelText('工作区绝对路径')
    await user.clear(input)
    await user.type(input, 'D:\\MyProject')
    await user.click(screen.getByRole('button', { name: '应用' }))

    await waitFor(() => {
      const post = calls.find((call) => call.method === 'POST' && call.url.endsWith('/api/config/workspace'))
      expect(post).toBeTruthy()
      expect(JSON.parse(post!.body!)).toEqual({ path: 'D:\\MyProject', extras: [] })
    })
    // 弹窗关闭，store 更新
    await waitFor(() =>
      expect(screen.queryByRole('dialog', { name: '切换工作区' })).not.toBeInTheDocument(),
    )
    expect(useChatStore.getState().workspace).toBe('D:\\MyProject')
  })

  it('后端拒绝危险路径时：弹窗内显示错误、不关闭', async () => {
    stubFetch((url, init) => {
      if (url.endsWith('/api/config/workspace') && init?.method === 'POST') {
        return jsonResponse(
          { detail: '出于安全考虑，禁止将系统核心目录设为工作区（C:\\Windows 位于受保护目录 C:\\Windows 之内）' },
          400,
        )
      }
      return null
    })
    const user = userEvent.setup()
    render(<Composer />)

    await user.click(screen.getByTestId('sandbox-badge'))
    const input = screen.getByLabelText('工作区绝对路径')
    await user.clear(input)
    await user.type(input, 'C:\\Windows')
    await user.click(screen.getByRole('button', { name: '应用' }))

    await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent(/禁止将系统核心目录/))
    expect(screen.getByRole('dialog', { name: '切换工作区' })).toBeInTheDocument()
  })

  it('「恢复默认」调用 DELETE 并把工作区恢复成默认值', async () => {
    const calls = stubFetch()
    const user = userEvent.setup()
    useChatStore.setState({ workspace: 'D:\\Other' })
    render(<Composer />)

    await user.click(screen.getByTestId('sandbox-badge'))
    await user.click(screen.getByRole('button', { name: /恢复默认/ }))

    await waitFor(() =>
      expect(calls.some((call) => call.method === 'DELETE' && call.url.endsWith('/api/config/workspace'))).toBe(true),
    )
    await waitFor(() => expect(useChatStore.getState().workspace).toBe(DEFAULT_WS))
  })

  it('非绝对路径给出前端提示（最终仍以后端校验为准）', async () => {
    stubFetch()
    const user = userEvent.setup()
    render(<Composer />)

    await user.click(screen.getByTestId('sandbox-badge'))
    const input = screen.getByLabelText('工作区绝对路径')
    await user.clear(input)
    await user.type(input, 'my-project')

    expect(screen.getByText(/看起来不是绝对路径/)).toBeInTheDocument()
  })
})
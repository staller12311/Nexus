import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { Composer } from '../components/Composer'
import { useChatStore } from '../store/chatStore'
import type { WorkspaceInfo } from '../types'

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

const DEFAULT_WS = 'D:\\MyAi\\workspace'
const BASE_INFO: WorkspaceInfo = {
  workspace: DEFAULT_WS,
  default_workspace: DEFAULT_WS,
  is_default: true,
  extras: [],
}

function stubFetch(
  handler?: (url: string, init?: RequestInit) => Response | null,
  getInfo: WorkspaceInfo = BASE_INFO,
) {
  const calls: Array<{ url: string; method?: string; body?: string }> = []
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      const method = init?.method ?? 'GET'
      const body = String(init?.body ?? '')
      calls.push({ url, method, body })
      const custom = handler?.(url, init)
      if (custom) return custom
      if (url.endsWith('/api/config/workspace')) {
        if (method === 'POST') {
          const payload = JSON.parse(body) as {
            path: string
            extras?: Array<{ name: string; path: string; writable: boolean }>
          }
          return jsonResponse({
            workspace: payload.path,
            default_workspace: DEFAULT_WS,
            is_default: false,
            extras: payload.extras ?? [],
          })
        }
        if (method === 'DELETE') return jsonResponse(BASE_INFO)
        return jsonResponse(getInfo)
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

async function openDialog(user: ReturnType<typeof userEvent.setup>) {
  await user.click(screen.getByTestId('sandbox-badge'))
  return screen.getByRole('dialog', { name: '切换工作区' })
}

describe('多根工作区（附加目录）前端', () => {
  beforeEach(() => {
    useChatStore.getState().reset()
    vi.unstubAllGlobals()
  })
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('可以添加附加目录行并提交 extras（默认只读）', async () => {
    const calls = stubFetch()
    const user = userEvent.setup()
    render(<Composer />)
    await openDialog(user)

    const main = screen.getByLabelText('工作区绝对路径')
    await user.clear(main)
    await user.type(main, 'D:\\ProjectA')

    await user.click(screen.getByRole('button', { name: /添加/ }))
    await user.type(screen.getByLabelText('附加目录 1 挂载名'), 'libs')
    await user.type(screen.getByLabelText('附加目录 1 路径'), 'D:\\SharedLibs')
    await user.click(screen.getByRole('button', { name: '应用' }))

    await waitFor(() => {
      const post = calls.find((call) => call.method === 'POST' && call.url.endsWith('/api/config/workspace'))
      expect(post).toBeTruthy()
      expect(JSON.parse(post!.body!)).toEqual({
        path: 'D:\\ProjectA',
        extras: [{ name: 'libs', path: 'D:\\SharedLibs', writable: false }],
      })
    })
    expect(useChatStore.getState().workspaceExtras).toEqual([
      { name: 'libs', path: 'D:\\SharedLibs', writable: false },
    ])
  })

  it('勾选「允许写入」后提交 writable: true', async () => {
    const calls = stubFetch()
    const user = userEvent.setup()
    render(<Composer />)
    await openDialog(user)

    await user.click(screen.getByRole('button', { name: /添加/ }))
    await user.type(screen.getByLabelText('附加目录 1 挂载名'), 'shared')
    await user.type(screen.getByLabelText('附加目录 1 路径'), 'D:\\Team\\Shared')
    await user.click(screen.getByLabelText('附加目录 1 允许写入'))
    await user.click(screen.getByRole('button', { name: '应用' }))

    await waitFor(() => {
      const post = calls.find((call) => call.method === 'POST' && call.url.endsWith('/api/config/workspace'))
      expect(JSON.parse(post!.body!).extras).toEqual([
        { name: 'shared', path: 'D:\\Team\\Shared', writable: true },
      ])
    })
  })

  it('非法挂载名在前端就被拦下（不发给后端）', async () => {
    const calls = stubFetch()
    const user = userEvent.setup()
    render(<Composer />)
    await openDialog(user)

    await user.click(screen.getByRole('button', { name: /添加/ }))
    await user.type(screen.getByLabelText('附加目录 1 挂载名'), 'Bad Name')
    await user.type(screen.getByLabelText('附加目录 1 路径'), 'D:\\X')
    await user.click(screen.getByRole('button', { name: '应用' }))

    await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent(/挂载名.*不合法/))
    expect(calls.some((call) => call.method === 'POST')).toBe(false)
  })

  it('后端拒绝附加目录（例如 C:\\Windows）时展示错误并保留输入', async () => {
    stubFetch((url, init) => {
      if (url.endsWith('/api/config/workspace') && init?.method === 'POST') {
        return jsonResponse({ detail: '出于安全考虑，禁止将系统核心目录设为工作区（C:\\Windows）' }, 400)
      }
      return null
    })
    const user = userEvent.setup()
    render(<Composer />)
    await openDialog(user)

    await user.click(screen.getByRole('button', { name: /添加/ }))
    await user.type(screen.getByLabelText('附加目录 1 挂载名'), 'win')
    await user.type(screen.getByLabelText('附加目录 1 路径'), 'C:\\Windows')
    await user.click(screen.getByRole('button', { name: '应用' }))

    await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent(/系统核心目录/))
    expect(screen.getByLabelText('附加目录 1 路径')).toHaveValue('C:\\Windows')
    expect(screen.getByRole('dialog', { name: '切换工作区' })).toBeInTheDocument()
  })

  it('已有挂载会回显，并可删除', async () => {
    const extras = [
      { name: 'libs', path: 'D:\\SharedLibs', writable: false },
      { name: 'shared', path: 'D:\\Team\\Shared', writable: true },
    ]
    stubFetch(undefined, {
      workspace: 'D:\\ProjectA',
      default_workspace: DEFAULT_WS,
      is_default: false,
      extras,
    })
    const user = userEvent.setup()
    useChatStore.setState({
      workspace: 'D:\\ProjectA',
      workspaceDefault: DEFAULT_WS,
      workspaceExtras: extras,
    })
    render(<Composer />)
    await openDialog(user)

    expect(screen.getByLabelText('附加目录 1 挂载名')).toHaveValue('libs')
    expect(screen.getByLabelText('附加目录 2 挂载名')).toHaveValue('shared')
    expect(screen.getByLabelText('附加目录 2 允许写入')).toBeChecked()

    await user.click(screen.getByLabelText('删除附加目录 2'))
    expect(screen.queryByLabelText('附加目录 2 挂载名')).not.toBeInTheDocument()
  })

  it('徽章 tooltip 列出主目录 + 挂载权限', async () => {
    stubFetch()
    useChatStore.setState({
      workspace: 'D:\\ProjectA',
      workspaceDefault: DEFAULT_WS,
      workspaceExtras: [{ name: 'libs', path: 'D:\\SharedLibs', writable: false }],
    })
    render(<Composer />)

    const badge = screen.getByTestId('sandbox-badge')
    expect(badge).toHaveTextContent('工作区沙箱保护中')
    // 徽章上直接显示当前工作区（视觉提示挂载已生效）
    expect(badge).toHaveTextContent('D:\\ProjectA')
  })
})
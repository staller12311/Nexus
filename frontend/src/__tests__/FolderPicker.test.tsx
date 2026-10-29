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

type Handler = (url: string, init?: RequestInit) => Response | null

function stubFetch(handler?: Handler) {
  const calls: Array<{ url: string; method?: string; body?: string }> = []
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      const method = init?.method ?? 'GET'
      calls.push({ url, method, body: String(init?.body ?? '') })
      const custom = handler?.(url, init)
      if (custom) return custom
      if (url.includes('/api/fs/dirs')) {
        // 模拟：此电脑 → C:\ / D:\；D:\ → ProjectA / SharedLibs
        const query = new URL(url, 'http://x').searchParams.get('path') ?? ''
        if (!query) {
          return jsonResponse({
            path: '',
            parent: null,
            entries: [],
            drives: ['C:\\', 'D:\\'],
            truncated: false,
          })
        }
        if (query === 'D:\\') {
          return jsonResponse({
            path: 'D:\\',
            parent: null,
            entries: [
              { name: 'ProjectA', path: 'D:\\ProjectA' },
              { name: 'SharedLibs', path: 'D:\\SharedLibs' },
            ],
            drives: ['C:\\', 'D:\\'],
            truncated: false,
          })
        }
        return jsonResponse({
          path: query,
          parent: query.includes('\\') ? 'D:\\' : null,
          entries: [],
          drives: ['C:\\', 'D:\\'],
          truncated: false,
        })
      }
      if (url.endsWith('/api/fs/mkdir')) {
        const payload = JSON.parse(String(init?.body ?? '{}')) as { parent: string; name: string }
        const parent = payload.parent.replace(/\\+$/, '')
        return jsonResponse({ name: payload.name, path: `${parent}\\${payload.name}` })
      }
      if (url.endsWith('/api/config/workspace')) {
        if (method === 'POST') {
          const payload = JSON.parse(String(init?.body ?? '{}')) as { path: string }
          return jsonResponse({
            workspace: payload.path,
            default_workspace: DEFAULT_WS,
            is_default: false,
            extras: [],
          })
        }
        return jsonResponse({
          workspace: DEFAULT_WS,
          default_workspace: DEFAULT_WS,
          is_default: true,
          extras: [],
        })
      }
      if (url.endsWith('/api/kb/status')) {
        return jsonResponse({
          docs: 0,
          chunks: 0,
          mode: 'keyword',
          supported: ['.md'],
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

async function openWorkspaceDialog(user: ReturnType<typeof userEvent.setup>) {
  await user.click(screen.getByTestId('sandbox-badge'))
  return screen.getByRole('dialog', { name: '切换工作区' })
}

describe('目录选择器（点击浏览代替手打路径）', () => {
  beforeEach(() => {
    useChatStore.getState().reset()
    vi.unstubAllGlobals()
  })
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('点文件夹图标打开选择器，显示盘符列表', async () => {
    stubFetch()
    const user = userEvent.setup()
    render(<Composer />)
    await openWorkspaceDialog(user)

    await user.click(screen.getByLabelText('浏览选择主目录'))
    const picker = await screen.findByRole('dialog', { name: '选择文件夹' })
    expect(picker).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: '此电脑' }))
    await waitFor(() => expect(screen.getByText('D:\\')).toBeInTheDocument())
    expect(screen.getByText('C:\\')).toBeInTheDocument()
  })

  it('逐级进入目录并「选择此文件夹」后回填到输入框', async () => {
    stubFetch()
    const user = userEvent.setup()
    render(<Composer />)
    await openWorkspaceDialog(user)

    await user.click(screen.getByLabelText('浏览选择主目录'))
    await user.click(await screen.findByRole('button', { name: '此电脑' }))
    await user.click(await screen.findByText('D:\\'))
    await user.click(await screen.findByText('ProjectA'))
    // 已进入 D:\ProjectA；点确认
    await user.click(screen.getByRole('button', { name: '选择此文件夹' }))

    await waitFor(() =>
      expect(screen.getByLabelText('工作区绝对路径')).toHaveValue('D:\\ProjectA'),
    )
    expect(screen.queryByRole('dialog', { name: '选择文件夹' })).not.toBeInTheDocument()
  })

  it('附加目录也可以点浏览按钮选择（回填到对应行）', async () => {
    stubFetch()
    const user = userEvent.setup()
    render(<Composer />)
    await openWorkspaceDialog(user)

    await user.click(screen.getByRole('button', { name: /添加/ }))
    await user.click(screen.getByLabelText('浏览选择附加目录 1'))
    await user.click(await screen.findByText('D:\\'))
    await user.click(await screen.findByText('SharedLibs'))
    await user.click(screen.getByRole('button', { name: '选择此文件夹' }))

    await waitFor(() =>
      expect(screen.getByLabelText('附加目录 1 路径')).toHaveValue('D:\\SharedLibs'),
    )
  })

  it('可以在选择器里新建文件夹（并回填）', async () => {
    const calls = stubFetch()
    const user = userEvent.setup()
    render(<Composer />)
    await openWorkspaceDialog(user)

    await user.click(screen.getByLabelText('浏览选择主目录'))
    await user.click(await screen.findByRole('button', { name: '此电脑' }))
    await user.click(await screen.findByText('D:\\'))
    await user.type(screen.getByLabelText('新建文件夹名'), 'MyProject')
    await user.click(screen.getByRole('button', { name: '新建' }))

    await waitFor(() =>
      expect(screen.getByLabelText('工作区绝对路径')).toHaveValue('D:\\MyProject'),
    )
    const mkdir = calls.find((call) => call.url.endsWith('/api/fs/mkdir'))
    expect(mkdir).toBeTruthy()
    expect(JSON.parse(mkdir!.body!)).toEqual({ parent: 'D:\\', name: 'MyProject' })
  })

  it('后端拒绝浏览系统目录时在选择器内显示错误', async () => {
    stubFetch((url) => {
      if (url.includes('/api/fs/dirs') && url.includes('path=C%3A%5C')) {
        return jsonResponse({ detail: '出于安全考虑，不允许浏览系统目录' }, 400)
      }
      return null
    })
    const user = userEvent.setup()
    render(<Composer />)
    await openWorkspaceDialog(user)

    await user.click(screen.getByLabelText('浏览选择主目录'))
    await user.click(await screen.findByRole('button', { name: '此电脑' }))
    await user.click(await screen.findByText('C:\\'))
    await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent(/不允许浏览系统目录/))
  })

  it('选择器内的面包屑可以回到「此电脑」', async () => {
    stubFetch()
    const user = userEvent.setup()
    render(<Composer />)
    await openWorkspaceDialog(user)

    await user.click(screen.getByLabelText('浏览选择主目录'))
    await user.click(await screen.findByRole('button', { name: '此电脑' }))
    await user.click(await screen.findByText('D:\\'))
    await user.click(await screen.findByText('ProjectA'))
    // 面包屑里有「此电脑」
    await user.click(screen.getByRole('button', { name: '此电脑' }))
    await waitFor(() => expect(screen.getByText('C:\\')).toBeInTheDocument())
  })
})
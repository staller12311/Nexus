import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { Composer } from '../components/Composer'
import { Sidebar } from '../components/Sidebar'
import { useChatStore } from '../store/chatStore'
import type { ModelInfo } from '../types'

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

function model(name: string, extra: Partial<ModelInfo> = {}): ModelInfo {
  return {
    name,
    model: `${name}-model`,
    base_url: 'https://api.example.com/v1',
    api_key_env: `${name.toUpperCase()}_API_KEY`,
    key_set: true,
    is_default: false,
    context_window: 65536,
    is_custom: false,
    has_key: true,
    api_key_masked: '',
    ...extra,
  }
}

/** Composer 挂载时会拉工作区；其余请求给最小可用响应。 */
function stubFetch() {
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      if (url.endsWith('/api/config/workspace')) {
        return jsonResponse({
          workspace: 'D:\\MyAi\\workspace',
          default_workspace: 'D:\\MyAi\\workspace',
          is_default: true,
          extras: [],
        })
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
}

describe('侧边栏极简化', () => {
  beforeEach(() => {
    useChatStore.getState().reset()
    vi.unstubAllGlobals()
    stubFetch()
  })

  it('只有品牌 / 新建会话 / 历史列表：模型选择已迁走', () => {
    render(<Sidebar />)

    expect(screen.getByText('Nexus 智枢')).toBeInTheDocument()
    expect(screen.getByText('PRO')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /新建会话/ })).toBeInTheDocument()
    expect(screen.getByText('历史会话')).toBeInTheDocument()

    // 模型下拉框与「添加模型」按钮都不再出现在侧边栏
    expect(screen.queryByLabelText('选择模型')).not.toBeInTheDocument()
    expect(screen.queryByLabelText('添加模型')).not.toBeInTheDocument()
  })
})

describe('输入框右下角的模型选择器', () => {
  beforeEach(() => {
    useChatStore.getState().reset()
    vi.unstubAllGlobals()
    stubFetch()
    useChatStore.getState().setModels([
      model('deepseek', { is_default: true }),
      model('moonshot', { is_custom: true }),
    ])
    useChatStore.getState().setModel('deepseek')
  })

  it('点击后向上弹出菜单并展示全部模型', async () => {
    const user = userEvent.setup()
    render(<Composer />)

    await user.click(screen.getByLabelText('选择模型'))

    const listbox = screen.getByRole('listbox', { name: '可用模型' })
    expect(screen.getByRole('option', { name: /deepseek/ })).toBeInTheDocument()
    expect(screen.getByRole('option', { name: /moonshot/ })).toBeInTheDocument()

    // 向上弹出（bottom-full）+ 高层级（z-50），避免被底部状态栏/屏幕边缘裁切
    const panel = listbox.closest('div.absolute')
    expect(panel?.className).toContain('bottom-full')
    expect(panel?.className).toContain('z-50')
  })

  it('选择模型后写回 store 并收起菜单', async () => {
    const user = userEvent.setup()
    render(<Composer />)

    await user.click(screen.getByLabelText('选择模型'))
    await user.click(screen.getByRole('option', { name: /moonshot/ }))

    await waitFor(() => expect(useChatStore.getState().model).toBe('moonshot'))
    expect(screen.queryByRole('listbox')).not.toBeInTheDocument()
    expect(screen.getByLabelText('选择模型')).toHaveTextContent('moonshot')
  })

  it('菜单底部的「添加 / 管理模型」打开模型管理弹窗', async () => {
    const user = userEvent.setup()
    render(<Composer />)

    await user.click(screen.getByLabelText('选择模型'))
    await user.click(screen.getByRole('button', { name: /添加 \/ 管理模型/ }))

    expect(screen.getByRole('dialog', { name: '模型管理' })).toBeInTheDocument()
  })

  it('正在生成时：模型按钮禁用，但「添加模型」仍可打开弹窗', async () => {
    const user = userEvent.setup()
    useChatStore.setState({ streaming: true })
    render(<Composer />)

    expect(screen.getByLabelText('选择模型')).toBeDisabled()

    await user.click(screen.getByLabelText('模型管理'))
    expect(screen.getByRole('dialog', { name: '模型管理' })).toBeInTheDocument()
  })
})
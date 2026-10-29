import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { Composer } from '../components/Composer'
import { useChatStore } from '../store/chatStore'
import type { ModelInfo } from '../types'

function builtin(name: string, isDefault = false): ModelInfo {
  return {
    name,
    model: `${name}-model`,
    base_url: `https://api.${name}.com/v1`,
    api_key_env: `${name.toUpperCase()}_API_KEY`,
    key_set: true,
    is_default: isDefault,
    context_window: 65536,
    is_custom: false,
    has_key: true,
    api_key_masked: '',
  }
}

function custom(name: string): ModelInfo {
  return {
    name,
    model: `${name}-model`,
    base_url: `https://api.${name}.com/v1`,
    api_key_env: '(自定义)',
    key_set: true,
    is_default: false,
    context_window: 32768,
    is_custom: true,
    has_key: true,
    api_key_masked: 'sk-****abcd',
  }
}

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

/** 有状态的假后端：POST/DELETE 会真的改动模型列表，便于验证"刷新后生效"。 */
function stubBackend(initial: ModelInfo[]) {
  let models = [...initial]
  const calls: Array<{ url: string; method?: string; body?: string }> = []

  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      const method = init?.method ?? 'GET'
      const body = String(init?.body ?? '')
      calls.push({ url, method, body })

      if (url.startsWith('/api/models/custom') && method === 'POST') {
        const payload = JSON.parse(body) as { name: string; base_url: string; model: string }
        const created = {
          name: payload.name,
          base_url: payload.base_url,
          model: payload.model,
          has_key: true,
          api_key_masked: 'sk-****wxyz',
          created_at: '',
          updated_at: '',
        }
        models = [...models, custom(payload.name)]
        return jsonResponse(created, 201)
      }
      if (url.startsWith('/api/models/custom/') && method === 'DELETE') {
        const name = decodeURIComponent(url.split('/').pop() ?? '')
        models = models.filter((item) => item.name !== name)
        return jsonResponse({ deleted: true, name, referenced_sessions: 0 })
      }
      if (url.startsWith('/api/models/custom')) {
        return jsonResponse({ models: [] })
      }
      if (url.startsWith('/api/models')) {
        return jsonResponse(models)
      }
      if (url.startsWith('/api/sessions')) {
        return jsonResponse({ sessions: [] })
      }
      return jsonResponse({}, 404)
    }),
  )
  return calls
}

describe('模型管理面板', () => {
  beforeEach(() => {
    useChatStore.getState().reset()
    vi.unstubAllGlobals()
  })
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('内置模型只读（无删除按钮），自定义模型显示掩码 Key 与删除按钮', async () => {
    const user = userEvent.setup()
    useChatStore.getState().setModels([builtin('deepseek', true), custom('moonshot')])
    stubBackend([])

    render(<Composer />)
    await user.click(screen.getByLabelText('模型管理'))

    expect(screen.getByText('内置模型（来自 config.yaml，只读）')).toBeInTheDocument()
    expect(screen.getByText('sk-****abcd')).toBeInTheDocument()
    expect(screen.getByLabelText('删除模型 moonshot')).toBeInTheDocument()
    expect(screen.queryByLabelText('删除模型 deepseek')).not.toBeInTheDocument()
  })

  it('表单校验：非法名称 / 非 http 的 URL 直接提示，不发请求', async () => {
    const user = userEvent.setup()
    useChatStore.getState().setModels([builtin('deepseek', true)])
    const calls = stubBackend([])

    render(<Composer />)
    await user.click(screen.getByLabelText('模型管理'))

    await user.type(screen.getByLabelText('模型名称'), '有 空格')
    await user.type(screen.getByLabelText('Base URL'), 'ftp://x/v1')
    await user.type(screen.getByLabelText('Model ID'), 'm')
    await user.type(screen.getByLabelText('API Key'), 'k')
    await user.click(screen.getByRole('button', { name: /保存/ }))

    expect(screen.getByText(/只能包含字母/)).toBeInTheDocument()
    expect(calls.some((call) => call.method === 'POST')).toBe(false)
  })

  it('添加成功：POST 的 Key 已 trim、列表刷新、自动切到新模型', async () => {
    const user = userEvent.setup()
    useChatStore.getState().setModels([builtin('deepseek', true)])
    const calls = stubBackend([builtin('deepseek', true)])

    render(<Composer />)
    await user.click(screen.getByLabelText('模型管理'))

    await user.type(screen.getByLabelText('模型名称'), 'moonshot')
    await user.type(screen.getByLabelText('Base URL'), '  https://api.moonshot.cn/v1/  ')
    await user.type(screen.getByLabelText('Model ID'), 'moonshot-v1-8k')
    // 粘贴的 Key 前后带空格
    await user.type(screen.getByLabelText('API Key'), '  sk-abcdefg123456  ')
    await user.click(screen.getByRole('button', { name: /保存/ }))

    await waitFor(() => expect(useChatStore.getState().model).toBe('moonshot'))
    const post = calls.find((call) => call.method === 'POST')
    expect(post?.url).toBe('/api/models/custom')
    expect(JSON.parse(post!.body!)).toEqual({
      name: 'moonshot',
      base_url: 'https://api.moonshot.cn/v1/',
      model: 'moonshot-v1-8k',
      api_key: 'sk-abcdefg123456', // 前后空格已去掉
    })
    expect(useChatStore.getState().notice).toContain('已添加模型 moonshot')
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
  })

  it('删除需二次确认；删掉当前选中模型后回退到默认模型', async () => {
    const user = userEvent.setup()
    useChatStore.getState().setModels([builtin('deepseek', true), custom('moonshot')])
    useChatStore.setState({ model: 'moonshot' })
    const calls = stubBackend([builtin('deepseek', true), custom('moonshot')])

    render(<Composer />)
    await user.click(screen.getByLabelText('模型管理'))
    await user.click(screen.getByLabelText('删除模型 moonshot'))

    // 只点垃圾桶：还没发 DELETE
    expect(screen.getByText(/删除模型/)).toBeInTheDocument()
    expect(calls.some((call) => call.method === 'DELETE')).toBe(false)

    await user.click(screen.getByRole('button', { name: '确认删除' }))

    await waitFor(() =>
      expect(calls.some((call) => call.method === 'DELETE' && call.url === '/api/models/custom/moonshot')).toBe(
        true,
      ),
    )
    await waitFor(() => expect(useChatStore.getState().model).toBe('deepseek'))
    expect(useChatStore.getState().models.some((item) => item.name === 'moonshot')).toBe(false)
  })

  it('正在回答时：下拉框禁用、保存与删除都禁用', async () => {
    const user = userEvent.setup()
    useChatStore.getState().setModels([builtin('deepseek', true), custom('moonshot')])
    stubBackend([])
    useChatStore.setState({ streaming: true })

    render(<Composer />)
    // 模型选择器已迁移到输入框右下角：生成期间禁用，但「添加模型」仍可打开管理弹窗
    expect(screen.getByLabelText('选择模型')).toBeDisabled()
    expect(screen.getByLabelText('选择模型')).toHaveAttribute(
      'title',
      'AI 正在回答，稍后可切换模型',
    )

    await user.click(screen.getByLabelText('模型管理'))
    expect(screen.getByText(/AI 正在回答，暂不能修改模型/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /保存/ })).toBeDisabled()
    expect(screen.getByLabelText('删除模型 moonshot')).toBeDisabled()
  })

  it('删除被 streaming 拦截时给出提示、不发请求', async () => {
    useChatStore.getState().setModels([builtin('deepseek', true), custom('moonshot')])
    const calls = stubBackend([])
    useChatStore.setState({ streaming: true })

    const ok = await useChatStore.getState().deleteCustomModel('moonshot')

    expect(ok).toBe(false)
    expect(useChatStore.getState().error).toContain('请等待当前回答结束')
    expect(calls.some((call) => call.method === 'DELETE')).toBe(false)
  })
})
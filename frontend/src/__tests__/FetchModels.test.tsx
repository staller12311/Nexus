import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { Sidebar } from '../components/Sidebar'
import { useChatStore } from '../store/chatStore'
import type { ModelInfo } from '../types'

function builtin(): ModelInfo {
  return {
    name: 'deepseek',
    model: 'deepseek-chat',
    base_url: 'https://api.deepseek.com/v1',
    api_key_env: 'DEEPSEEK_API_KEY',
    key_set: true,
    is_default: true,
    is_custom: false,
    has_key: true,
    api_key_masked: '',
  }
}

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

function stubFetch(fetchResult: { models?: string[]; status?: number; detail?: string }) {
  const calls: Array<{ url: string; method?: string; body?: string }> = []
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      calls.push({ url, method: init?.method, body: String(init?.body ?? '') })
      if (url.endsWith('/models/fetch')) {
        if (fetchResult.status && fetchResult.status >= 400) {
          return jsonResponse({ detail: fetchResult.detail ?? '拉取失败' }, fetchResult.status)
        }
        return jsonResponse({
          base_url: 'https://api.moonshot.cn/v1/models',
          models: fetchResult.models ?? ['moonshot-v1-8k', 'moonshot-v1-32k'],
          count: (fetchResult.models ?? ['moonshot-v1-8k', 'moonshot-v1-32k']).length,
        })
      }
      if (url.startsWith('/api/models')) return jsonResponse([builtin()])
      if (url.startsWith('/api/sessions')) return jsonResponse({ sessions: [] })
      return jsonResponse({}, 404)
    }),
  )
  return calls
}

async function openDialog(user: ReturnType<typeof userEvent.setup>) {
  render(<Sidebar />)
  await user.click(screen.getByLabelText('添加模型'))
}

async function fillBase(user: ReturnType<typeof userEvent.setup>) {
  await user.type(screen.getByLabelText('模型名称'), 'moonshot')
  await user.type(screen.getByLabelText('Base URL'), 'https://api.moonshot.cn/v1')
  await user.type(screen.getByLabelText('API Key'), 'sk-test-key-123456')
}

describe('获取模型列表', () => {
  beforeEach(() => {
    useChatStore.getState().reset()
    useChatStore.getState().setModels([builtin()])
    vi.unstubAllGlobals()
  })
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('成功后出现可搜索选择器，且**不会**自动塞一个模型进去', async () => {
    const user = userEvent.setup()
    const calls = stubFetch({ models: ['qwen-turbo', 'qwen-plus', 'qwen-max'] })
    await openDialog(user)
    await fillBase(user)

    await user.click(screen.getByRole('button', { name: /获取模型列表/ }))

    const search = await screen.findByLabelText('搜索模型')
    // 输入框保持为空，只有 placeholder（不回填任何模型 id）
    expect(search).toHaveValue('')
    expect(search).toHaveAttribute('placeholder', '请选择模型…')
    expect(screen.queryByText(/^已选：/)).not.toBeInTheDocument()
    // 提示语要求用户自己搜索选择
    expect(screen.getByText(/请在上方搜索并选择/)).toBeInTheDocument()
    // 原来的手动输入框已被替换
    expect(screen.queryByLabelText('Model ID')).not.toBeInTheDocument()

    // 请求体里带的是用户填的 base_url 与 key
    const fetchCall = calls.find((call) => call.url.endsWith('/models/fetch'))
    expect(JSON.parse(fetchCall!.body!)).toEqual({
      base_url: 'https://api.moonshot.cn/v1',
      api_key: 'sk-test-key-123456',
    })
  })

  it('从下拉框选另一个模型后，保存时提交的是选中的 id', async () => {
    const user = userEvent.setup()
    const calls = stubFetch({ models: ['qwen-turbo', 'qwen-plus'] })
    await openDialog(user)
    await fillBase(user)
    await user.click(screen.getByRole('button', { name: /获取模型列表/ }))
    await waitFor(() => expect(screen.getByLabelText('搜索模型')).toBeInTheDocument())

    await user.click(screen.getByLabelText('搜索模型'))
    await user.click(screen.getByRole('option', { name: /qwen-plus/ }))
    await user.click(screen.getByRole('button', { name: /保存/ }))

    await waitFor(() => {
      const post = calls.find((call) => call.method === 'POST' && call.url === '/api/models/custom')
      expect(post).toBeTruthy()
      expect(JSON.parse(post!.body!).model).toBe('qwen-plus')
    })
  })

  it('拉取失败：给出提示、保留手动输入框', async () => {
    const user = userEvent.setup()
    stubFetch({ status: 502, detail: '该服务商没有 /models 接口（HTTP 404）' })
    await openDialog(user)
    await fillBase(user)

    await user.click(screen.getByRole('button', { name: /获取模型列表/ }))

    await waitFor(() => expect(screen.getByRole('status')).toHaveTextContent(/没有 \/models 接口/))
    expect(screen.getByLabelText('Model ID')).toBeInTheDocument() // 手动输入仍在
    expect(screen.queryByLabelText('搜索模型')).not.toBeInTheDocument()
    expect(screen.getByRole('status')).toHaveTextContent(/可继续填写/)
  })

  it('没填 Base URL / Key 就点：直接提示，不发请求', async () => {
    const user = userEvent.setup()
    const calls = stubFetch({})
    await openDialog(user)

    await user.click(screen.getByRole('button', { name: /获取模型列表/ }))

    expect(screen.getByRole('status')).toHaveTextContent(/先填好 Base URL 和 API Key/)
    expect(calls.some((call) => call.url.endsWith('/models/fetch'))).toBe(false)
  })

  it('拉取成功后可以切回手动输入', async () => {
    const user = userEvent.setup()
    stubFetch({ models: ['qwen-turbo'] })
    await openDialog(user)
    await fillBase(user)
    await user.click(screen.getByRole('button', { name: /获取模型列表/ }))
    await waitFor(() => expect(screen.getByLabelText('搜索模型')).toBeInTheDocument())

    await user.click(screen.getByRole('button', { name: '手动输入 Model ID' }))
    expect(screen.getByLabelText('Model ID')).toBeInTheDocument()
    expect(screen.queryByLabelText('搜索模型')).not.toBeInTheDocument()
  })

  it('改了 Base URL 后，之前拉到的列表失效', async () => {
    const user = userEvent.setup()
    stubFetch({ models: ['qwen-turbo'] })
    await openDialog(user)
    await fillBase(user)
    await user.click(screen.getByRole('button', { name: /获取模型列表/ }))
    await waitFor(() => expect(screen.getByLabelText('搜索模型')).toBeInTheDocument())

    const baseInput = screen.getByLabelText('Base URL')
    await user.type(baseInput, '2')
    expect(screen.getByLabelText('Model ID')).toBeInTheDocument() // 回到手动输入
  })
})
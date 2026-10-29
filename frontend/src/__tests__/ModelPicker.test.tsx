import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { Composer } from '../components/Composer'
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
    context_window: 65536,
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

// 模拟 dashscope 的真实返回：大量非纯文本模型 + 主流文本模型
const FETCHED = [
  'qwen-audio-3.1-realtime-flash',
  'qwen-image-2.1-pro',
  'qwen3.7-text-embedding-flash',
  'qwen-vl-ocr',
  'qwen-tts-v2',
  'qwen3.8-flash',
  'qwen3.8-max-0902',
  'qwen-plus',
  'qwen-max',
  'qwen-turbo',
  'deepseek-chat',
]

function stubFetch() {
  const calls: Array<{ url: string; method?: string; body?: string }> = []
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      calls.push({ url, method: init?.method, body: String(init?.body ?? '') })
      if (url.endsWith('/models/fetch')) {
        return jsonResponse({ base_url: 'https://dashscope.aliyuncs.com/compatible-mode/v1/models', models: FETCHED, count: FETCHED.length })
      }
      if (url.startsWith('/api/models')) return jsonResponse([builtin()])
      if (url.startsWith('/api/sessions')) return jsonResponse({ sessions: [] })
      return jsonResponse({}, 404)
    }),
  )
  return calls
}

async function openWithFetched(user: ReturnType<typeof userEvent.setup>) {
  render(<Composer />)
  await user.click(screen.getByLabelText('模型管理'))
  await user.type(screen.getByLabelText('模型名称'), 'qianwen')
  await user.type(screen.getByLabelText('Base URL'), 'https://dashscope.aliyuncs.com/compatible-mode/v1')
  await user.type(screen.getByLabelText('API Key'), 'sk-test')
  await user.click(screen.getByRole('button', { name: /获取模型列表/ }))
  await waitFor(() => expect(screen.getByLabelText('搜索模型')).toBeInTheDocument())
}

describe('模型选择器（搜索 + 智能过滤 + 排序）', () => {
  beforeEach(() => {
    useChatStore.getState().reset()
    useChatStore.getState().setModels([builtin()])
    vi.unstubAllGlobals()
  })
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('只列出纯文本模型，并告知隐藏了多少个', async () => {
    const user = userEvent.setup()
    stubFetch()
    await openWithFetched(user)

    // 打开列表
    await user.click(screen.getByLabelText('搜索模型'))

    const options = within(screen.getByRole('listbox'))
      .getAllByRole('option')
      .map((item) => item.textContent?.trim())
    expect(options).not.toContain('qwen-audio-3.1-realtime-flash')
    expect(options).not.toContain('qwen-image-2.1-pro')
    expect(options).not.toContain('qwen3.7-text-embedding-flash')
    expect(options).not.toContain('qwen-vl-ocr')
    expect(options).not.toContain('qwen-tts-v2')
    expect(options).toContain('qwen3.8-flash')
    expect(screen.getByText(/已隐藏 5 个音频\/图像\/向量等模型/)).toBeInTheDocument()
  })

  it('主流模型排在最前面', async () => {
    const user = userEvent.setup()
    stubFetch()
    await openWithFetched(user)
    await user.click(screen.getByLabelText('搜索模型'))

    const options = within(screen.getByRole('listbox'))
      .getAllByRole('option')
      .map((item) => item.textContent?.trim())
    expect(options.slice(0, 3)).toEqual(['qwen-max', 'qwen-plus', 'qwen-turbo'])
    expect(options[3]).toBe('deepseek-chat')
  })

  it('输入关键字实时过滤（qwen → 只剩 qwen 系）', async () => {
    const user = userEvent.setup()
    stubFetch()
    await openWithFetched(user)

    const box = screen.getByLabelText('搜索模型')
    await user.click(box)
    await user.type(box, 'qwen3.8')

    const options = within(screen.getByRole('listbox'))
      .getAllByRole('option')
      .map((item) => item.textContent?.trim())
    // 两者都不命中「主流关键字」，保持上游返回的原顺序（qwen3.8-flash 在前）
    expect(options).toEqual(['qwen3.8-flash', 'qwen3.8-max-0902'])
    expect(screen.getByText(/匹配「qwen3.8」/)).toBeInTheDocument()
  })

  it('搜索 deepseek 能精准命中，回车选中', async () => {
    const user = userEvent.setup()
    stubFetch()
    await openWithFetched(user)

    const box = screen.getByLabelText('搜索模型')
    await user.click(box)
    await user.type(box, 'deepseek{Enter}')

    // 收起列表并显示已选
    await waitFor(() => expect(screen.queryByRole('listbox')).not.toBeInTheDocument())
    expect(screen.getByText('已选：deepseek-chat')).toBeInTheDocument()
  })

  it('点击选项后选中，保存时提交该 id', async () => {
    const user = userEvent.setup()
    const calls = stubFetch()
    await openWithFetched(user)
    await user.click(screen.getByLabelText('搜索模型'))

    await user.click(screen.getByRole('option', { name: /qwen3\.8-max-0902/ }))
    expect(screen.getByText('已选：qwen3.8-max-0902')).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: /保存/ }))
    await waitFor(() => {
      const post = calls.find((call) => call.method === 'POST' && call.url === '/api/models/custom')
      expect(post).toBeTruthy()
      expect(JSON.parse(post!.body!).model).toBe('qwen3.8-max-0902')
    })
  })

  it('输入框只显示搜索词：清空后恢复全部，输入后按包含关系过滤', async () => {
    const user = userEvent.setup()
    stubFetch()
    await openWithFetched(user)

    const search = screen.getByLabelText('搜索模型')
    // 刚打开时为空，placeholder 提示
    await user.click(search)
    expect(search).toHaveValue('')
    const before = within(screen.getByRole('listbox')).getAllByRole('option').length
    expect(before).toBe(6) // FETCHED 里 6 个纯文本模型

    await user.type(search, 'qwen3.8')
    expect(search).toHaveValue('qwen3.8')
    expect(within(screen.getByRole('listbox')).getAllByRole('option')).toHaveLength(2)

    // 清空搜索词 → 恢复全部
    await user.clear(search)
    expect(search).toHaveValue('')
    expect(within(screen.getByRole('listbox')).getAllByRole('option')).toHaveLength(6)
  })

  it('搜不到时给出空态提示', async () => {
    const user = userEvent.setup()
    stubFetch()
    await openWithFetched(user)
    await user.click(screen.getByLabelText('搜索模型'))
    await user.type(screen.getByLabelText('搜索模型'), 'zzz')
    expect(screen.getByText('没有匹配的模型')).toBeInTheDocument()
  })
})
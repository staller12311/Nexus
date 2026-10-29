import { render, screen } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { Composer } from '../components/Composer'
import { reconstructMessages } from '../lib/reconstruct'
import { useChatStore } from '../store/chatStore'
import type { ApiMessage } from '../types'

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

/** Composer 挂载时会拉一次工作区；给个稳定桩，避免未处理 rejection。 */
function stubMinimalFetch() {
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
      return jsonResponse({ sessions: [] })
    }),
  )
}

describe('极客状态栏（Composer 底部）', () => {
  beforeEach(() => {
    useChatStore.getState().reset()
    vi.unstubAllGlobals()
    stubMinimalFetch()
  })

  it('展示 tok/s、全库 token 与缓存命中率、上下文占用率', () => {
    useChatStore.setState({
      stats: {
        sessions: 3,
        prompt_tokens: 600_000_000,
        completion_tokens: 20_000_000,
        total_tokens: 591_000_000,
        cache_hit_tokens: 588_000_000,
        cache_hit_rate: 0.98,
      },
      contextUsage: 0.27,
      lastTokensPerSecond: 137,
    })

    render(<Composer />)

    expect(screen.getByText(/⚡ 137 tok\/s/)).toBeInTheDocument()
    expect(screen.getByText(/591M tok · 命中 98%/)).toBeInTheDocument()
    expect(screen.getByText(/🌡️ 27%/)).toBeInTheDocument()
  })

  it('没有统计数据时静默：只显示快捷键与免责声明', () => {
    render(<Composer />)

    expect(screen.getByText(/Enter 发送 · Shift\+Enter 换行/)).toBeInTheDocument()
    expect(screen.getByText(/内容由模型生成/)).toBeInTheDocument()
    expect(screen.queryByText(/📚/)).not.toBeInTheDocument()
    expect(screen.queryByText(/⚡/)).not.toBeInTheDocument()
    expect(screen.queryByText(/🌡️/)).not.toBeInTheDocument()
  })
})

describe('历史消息的单轮 usage', () => {
  it('reconstructMessages 会把落库的 usage（含缓存命中）带回气泡', () => {
    const raw: ApiMessage[] = [
      { role: 'user', content: '写个快排' },
      {
        role: 'assistant',
        content: '好',
        usage: {
          prompt_tokens: 1200,
          completion_tokens: 50,
          total_tokens: 1250,
          cache_hit_tokens: 1080,
        },
      },
    ]

    const [assistant] = reconstructMessages(raw).filter((message) => message.role === 'assistant')
    expect(assistant.usage).toEqual({
      prompt_tokens: 1200,
      completion_tokens: 50,
      total_tokens: 1250,
      cache_hit_tokens: 1080,
    })
  })

  it('旧消息没有 usage 时保持 undefined（不渲染 0 值）', () => {
    const raw: ApiMessage[] = [
      { role: 'assistant', content: '旧回答' },
    ]
    const [assistant] = reconstructMessages(raw)
    expect(assistant.usage).toBeUndefined()
  })
})
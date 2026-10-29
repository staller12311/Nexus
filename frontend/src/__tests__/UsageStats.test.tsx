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

function statsFixture(overrides: Record<string, unknown> = {}) {
  return {
    sessions: 1,
    prompt_tokens: 300_000,
    completion_tokens: 31_000,
    total_tokens: 331_000,
    cache_hit_tokens: 282_600,
    cache_hit_rate: 0.942,
    session_id: 's1',
    ...overrides,
  }
}

describe('极客状态栏（Composer 底部）', () => {
  beforeEach(() => {
    useChatStore.getState().reset()
    vi.unstubAllGlobals()
    stubMinimalFetch()
  })

  it('展示累计 token / 节省比例与金额 / 上下文占用，且没有彩色 Emoji', () => {
    useChatStore.setState({
      sessionId: 's1',
      stats: statsFixture(),
      contextUsage: 0.05,
    })

    render(<Composer />)

    expect(screen.getByText(/累计/)).toBeInTheDocument()
    expect(screen.getByText('331K')).toBeInTheDocument()
    expect(screen.getByText(/节省/)).toBeInTheDocument()
    expect(screen.getByText('94.2%')).toBeInTheDocument()
    expect(screen.getByText(/约 ¥0\.28/)).toBeInTheDocument()
    expect(screen.getByText(/当前上下文/)).toBeInTheDocument()
    expect(screen.getByText('5%')).toBeInTheDocument()

    // 状态栏不再出现 emoji，只有 lucide 的 svg 图标
    expect(document.body.textContent ?? '').not.toMatch(/[📚⚡🌡️🎫🧠]/u)
    expect(document.querySelectorAll('svg').length).toBeGreaterThan(3)
  })

  it('0 历史/第一条消息：显示「暂无数据」，绝不出现 NaN / 负数', () => {
    useChatStore.setState({ sessionId: 's1', stats: null, contextUsage: null })
    render(<Composer />)

    expect(screen.getByText(/节省/)).toBeInTheDocument()
    expect(screen.getByText('暂无数据')).toBeInTheDocument()
    const text = document.body.textContent ?? ''
    expect(text).not.toMatch(/NaN|Infinity/)
    expect(text).not.toMatch(/-\d/)
    expect(screen.getByText(/当前上下文/)).toBeInTheDocument()
  })

  it('命中率为 0 时显示 0.0% / ¥0.00（不做除零）', () => {
    useChatStore.setState({
      sessionId: 's1',
      stats: statsFixture({
        prompt_tokens: 120,
        completion_tokens: 30,
        total_tokens: 150,
        cache_hit_tokens: 0,
        cache_hit_rate: 0,
      }),
      contextUsage: 0,
    })
    render(<Composer />)

    expect(screen.getByText('0.0%')).toBeInTheDocument()
    expect(screen.getByText(/约 ¥0\.00/)).toBeInTheDocument()
    expect(screen.getByText('0%')).toBeInTheDocument()
  })
})

describe('统计口径与容错（store）', () => {
  beforeEach(() => {
    useChatStore.getState().reset()
    vi.unstubAllGlobals()
  })

  it('loadStats 只按当前会话请求（带 session_id），并原样保存后端命中率', async () => {
    const calls: string[] = []
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL) => {
        calls.push(String(input))
        return jsonResponse(statsFixture())
      }),
    )
    useChatStore.setState({ sessionId: 'sess-42' })

    await useChatStore.getState().loadStats()

    expect(calls).toHaveLength(1)
    expect(calls[0]).toBe('/api/stats?session_id=sess-42')
    expect(useChatStore.getState().stats?.cache_hit_rate).toBe(0.942)
  })

  it('没有会话时不发请求，统计保持 null', async () => {
    const calls: string[] = []
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL) => {
        calls.push(String(input))
        return jsonResponse({})
      }),
    )

    await useChatStore.getState().loadStats()

    expect(calls).toHaveLength(0)
    expect(useChatStore.getState().stats).toBeNull()
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
    const raw: ApiMessage[] = [{ role: 'assistant', content: '旧回答' }]
    const [assistant] = reconstructMessages(raw)
    expect(assistant.usage).toBeUndefined()
  })
})
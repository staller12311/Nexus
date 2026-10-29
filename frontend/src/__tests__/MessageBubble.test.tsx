import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { MessageBubble } from '../components/MessageBubble'
import type { ChatMessage } from '../types'

const base = { id: 'm1', role: 'assistant' as const }

describe('MessageBubble 渲染规则', () => {
  it('content=null 且有 toolCalls：只渲染工具卡片，不渲染空气泡', () => {
    const message: ChatMessage = {
      ...base,
      content: null,
      toolCalls: [
        {
          id: 'call_1',
          name: 'read_file',
          arguments: '{"path": "readme.txt"}',
          status: 'success',
          content: 'hi',
        },
      ],
    }
    const { container } = render(<MessageBubble message={message} />)

    // 有工具卡片
    expect(screen.getByText(/read_file/)).toBeInTheDocument()
    expect(screen.getByText(/read_file/).closest('button')).toBeTruthy()
    // 没有文字气泡：容器里只有一个子节点（工具卡片所在的外层 div 内）
    expect(container.querySelectorAll('.markdown')).toHaveLength(0)
    expect(container.textContent).not.toContain('null')
  })

  it('content="" 且无 toolCalls：整条不渲染', () => {
    const message: ChatMessage = { ...base, content: '', toolCalls: [] }
    const { container } = render(<MessageBubble message={message} />)
    expect(container).toBeEmptyDOMElement()
  })

  it('content=文字 且有 toolCalls：文字气泡 + 工具卡片都在', () => {
    const message: ChatMessage = {
      ...base,
      content: 'A：我先看看时间。',
      toolCalls: [
        { id: 'call_a', name: 'get_time', arguments: '{"zone":"UTC"}', status: 'pending' },
      ],
    }
    const { container } = render(<MessageBubble message={message} />)

    expect(screen.getByText('A：我先看看时间。')).toBeInTheDocument()
    expect(screen.getByText(/get_time/)).toBeInTheDocument()
    expect(screen.getByText(/执行中/)).toBeInTheDocument()
    expect(container.querySelectorAll('.markdown')).toHaveLength(1)
  })

  it('失败的工具调用标红并显示错误文本', () => {
    const message: ChatMessage = {
      ...base,
      content: null,
      toolCalls: [
        {
          id: 'call_x',
          name: 'read_file',
          arguments: '{"path": "../secret.txt"}',
          status: 'error',
          content: '错误：路径越出 workspace',
        },
      ],
    }
    render(<MessageBubble message={message} />)
    expect(screen.getByText(/失败/)).toBeInTheDocument()
  })

  it('usage 显示在 AI 回复底部，并带缓存命中率', () => {
    const message: ChatMessage = {
      ...base,
      content: '读到了 hi',
      usage: {
        prompt_tokens: 24,
        completion_tokens: 12,
        total_tokens: 36,
        cache_hit_tokens: 12,
      },
    }
    render(<MessageBubble message={message} />)
    expect(
      screen.getByText(/prompt 24 \(命中 50%\) \/ completion 12 \/ total 36/),
    ).toBeInTheDocument()
  })
})
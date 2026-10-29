import { render, screen } from '@testing-library/react'
import { beforeEach, describe, expect, it } from 'vitest'
import { MessageBubble } from '../components/MessageBubble'
import { useChatStore } from '../store/chatStore'
import type { ChatMessage } from '../types'

function assistant(content: string): ChatMessage {
  return { id: 'a1', role: 'assistant', content }
}

describe('Markdown 链接：新标签页打开', () => {
  beforeEach(() => {
    useChatStore.getState().reset()
  })

  it('普通链接带上 target=_blank 与 rel=noopener noreferrer', () => {
    render(<MessageBubble message={assistant('来源：[一篇报道](https://news.example.com/a)')} />)

    const link = screen.getByRole('link', { name: '一篇报道' })
    expect(link).toHaveAttribute('href', 'https://news.example.com/a')
    expect(link).toHaveAttribute('target', '_blank')
    expect(link).toHaveAttribute('rel', 'noopener noreferrer')
  })

  it('搜索结果里的裸链接（autolink）同样生效', () => {
    render(<MessageBubble message={assistant('链接：https://deepmind.google/blog/genie-4/')} />)

    const links = screen.getAllByRole('link')
    expect(links).toHaveLength(1)
    expect(links[0]).toHaveAttribute('target', '_blank')
    expect(links[0]).toHaveAttribute('rel', 'noopener noreferrer')
  })

  it('多个链接都带属性，且不会丢失原 href', () => {
    render(
      <MessageBubble
        message={assistant(
          '- [A](https://a.example.com/1)\n- [B](https://b.example.com/2)',
        )}
      />,
    )

    const links = screen.getAllByRole('link')
    expect(links).toHaveLength(2)
    for (const link of links) {
      expect(link).toHaveAttribute('target', '_blank')
      expect(link).toHaveAttribute('rel', 'noopener noreferrer')
    }
    expect(links[0]).toHaveAttribute('href', 'https://a.example.com/1')
    expect(links[1]).toHaveAttribute('href', 'https://b.example.com/2')
  })

  it('用户消息不经过 markdown（保持纯文本，不生成链接）', () => {
    render(
      <MessageBubble
        message={{ id: 'u1', role: 'user', content: '[不是链接](https://x.example.com)' }}
      />,
    )
    expect(screen.queryByRole('link')).not.toBeInTheDocument()
  })
})
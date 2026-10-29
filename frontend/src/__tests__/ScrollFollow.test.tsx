import { act, fireEvent, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it } from 'vitest'
import { ChatArea } from '../components/ChatArea'
import { useChatStore } from '../store/chatStore'
import type { ChatMessage } from '../types'

/** 让 jsdom 的容器拥有可控的滚动几何 + 记录 scrollTo 调用。 */
function installScrollGeometry(element: HTMLElement, { scrollHeight = 2000, clientHeight = 600 } = {}) {
  let scrollTop = 0
  const calls: Array<{ top: number; behavior?: string }> = []
  Object.defineProperty(element, 'scrollHeight', { configurable: true, get: () => scrollHeight })
  Object.defineProperty(element, 'clientHeight', { configurable: true, get: () => clientHeight })
  Object.defineProperty(element, 'scrollTop', {
    configurable: true,
    get: () => scrollTop,
    set: (value: number) => {
      scrollTop = value
    },
  })
  element.scrollTo = ((options?: ScrollToOptions) => {
    calls.push({ top: options?.top ?? 0, behavior: options?.behavior })
    scrollTop = options?.top ?? 0
  }) as typeof element.scrollTo
  return { calls, getScrollTop: () => scrollTop }
}

function assistant(id: string, content: string, streaming = false): ChatMessage {
  return { id, role: 'assistant', content, streaming }
}

describe('滚动劫持修复', () => {
  beforeEach(() => {
    useChatStore.getState().reset()
  })

  it('流式新内容会跟随到底部（用户没往上翻时）', () => {
    useChatStore.setState({
      messages: [assistant('a1', '第一段', true)],
    })
    render(<ChatArea />)
    const scroller = screen.getByTestId('chat-scroll')
    const geometry = installScrollGeometry(scroller)

    act(() => {
      useChatStore.setState({
        messages: [assistant('a1', '第一段 + 新内容', true)],
      })
    })

    expect(geometry.calls.length).toBeGreaterThan(0)
    expect(geometry.getScrollTop()).toBe(2000)
  })

  it('用户往上滚超过 100px 后，流式新内容不再把页面拽回底部', () => {
    useChatStore.setState({ messages: [assistant('a1', '长回答开头', true)] })
    render(<ChatArea />)
    const scroller = screen.getByTestId('chat-scroll')
    const geometry = installScrollGeometry(scroller)

    // 用户往上翻：scrollTop=500 → 距底部 = 2000 - 500 - 600 = 900 > 100
    Object.defineProperty(scroller, 'scrollTop', {
      configurable: true,
      get: () => 500,
      set: () => undefined,
    })
    fireEvent.scroll(scroller)

    const callsBefore = geometry.calls.length
    act(() => {
      useChatStore.setState({ messages: [assistant('a1', '长回答开头 + 更多内容', true)] })
    })

    // 关键断言：流式更新**没有**触发任何滚动
    expect(geometry.calls.length).toBe(callsBefore)
    expect(screen.getByLabelText('回到底部')).toBeInTheDocument()
  })

  it('距底部小于 100px 时仍算「贴底」，继续自动跟随', () => {
    useChatStore.setState({ messages: [assistant('a1', '内容', true)] })
    render(<ChatArea />)
    const scroller = screen.getByTestId('chat-scroll')
    const geometry = installScrollGeometry(scroller)

    // 距底部 = 2000 - 1450 - 600 = -50 → 贴底
    Object.defineProperty(scroller, 'scrollTop', {
      configurable: true,
      get: () => 1450,
      set: () => undefined,
    })
    fireEvent.scroll(scroller)

    act(() => {
      useChatStore.setState({ messages: [assistant('a1', '内容 + 新增', true)] })
    })
    expect(geometry.calls.length).toBeGreaterThan(0)
    expect(screen.queryByLabelText('回到底部')).not.toBeInTheDocument()
  })

  it('点「回到底部」：平滑滚动到底并隐藏按钮', async () => {
    useChatStore.setState({ messages: [assistant('a1', '长回答', true)] })
    render(<ChatArea />)
    const scroller = screen.getByTestId('chat-scroll')
    const geometry = installScrollGeometry(scroller)

    Object.defineProperty(scroller, 'scrollTop', {
      configurable: true,
      get: () => 400,
      set: () => undefined,
    })
    fireEvent.scroll(scroller)
    expect(screen.getByLabelText('回到底部')).toBeInTheDocument()

    const user = userEvent.setup()
    await user.click(screen.getByLabelText('回到底部'))

    const lastCall = geometry.calls[geometry.calls.length - 1]
    expect(lastCall.top).toBe(2000)
    expect(lastCall.behavior).toBe('smooth')
    expect(screen.queryByLabelText('回到底部')).not.toBeInTheDocument()
  })

  it('用户自己发新消息时，即使在往上看也强制回到底部', () => {
    useChatStore.setState({ messages: [assistant('a1', '旧回答', false)] })
    render(<ChatArea />)
    const scroller = screen.getByTestId('chat-scroll')
    const geometry = installScrollGeometry(scroller)

    Object.defineProperty(scroller, 'scrollTop', {
      configurable: true,
      get: () => 100,
      set: () => undefined,
    })
    fireEvent.scroll(scroller)
    const callsBefore = geometry.calls.length

    act(() => {
      useChatStore.setState({
        messages: [
          assistant('a1', '旧回答', false),
          { id: 'u2', role: 'user', content: '新问题' },
          assistant('a2', '', true),
        ],
      })
    })

    expect(geometry.calls.length).toBeGreaterThan(callsBefore)
    expect(screen.queryByLabelText('回到底部')).not.toBeInTheDocument()
  })
})
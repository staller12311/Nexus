import { render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it } from 'vitest'
import { MessageBubble } from '../components/MessageBubble'
import { useChatStore } from '../store/chatStore'
import type { ChatMessage } from '../types'

/** 200 行 Python 代码，模拟用户实测的场景（流式输出长代码）。 */
function longCode(lines = 200): string {
  const body = Array.from({ length: lines }, (_, index) => `x${index} = ${index} * 2  # 第 ${index} 行`).join('\n')
  return '```python\n' + body + '\n```'
}

function assistant(content: string, streaming: boolean): ChatMessage {
  return { id: 'a1', role: 'assistant', content, streaming }
}

describe('流式渲染性能（长代码不卡死）', () => {
  beforeEach(() => {
    useChatStore.getState().reset()
  })

  it('流式期间代码块用纯 <pre>，绝不挂语法高亮器', () => {
    const message = assistant(longCode(), true) // 没闭合的三引号也要能渲染
    const { container } = render(<MessageBubble message={message} />)

    // 纯文本块存在
    const plain = container.querySelector('[data-testid="code-plain"]')
    expect(plain).not.toBeNull()
    // 高亮器会产出 token/行号 span，这里一个都不该有
    expect(container.querySelector('.token')).toBeNull()
    expect(container.querySelector('[data-testid="code-line-numbers"]')).toBeNull()
    // 也没有「复制代码」按钮（减少流式期间的 DOM churn）
    expect(screen.queryByLabelText('复制代码')).not.toBeInTheDocument()
  })

  it('流式结束后挂载语法高亮（token span 出现）', async () => {
    const message = assistant(longCode(40), false)
    const { container } = render(<MessageBubble message={message} />)

    await waitFor(() => expect(container.querySelector('.token')).not.toBeNull())
    expect(container.querySelector('[data-testid="code-plain"]')).toBeNull()
    expect(screen.getByLabelText('复制代码')).toBeInTheDocument()
  })

  it('历史消息在最后一条流式更新时保持同一对象引用（memo 才能跳过渲染）', () => {
    const history: ChatMessage[] = Array.from({ length: 5 }, (_, index) => ({
      id: `h${index}`,
      role: 'assistant',
      content: `历史回答 ${index}`,
    }))
    useChatStore.setState({
      messages: [...history, { id: 'a1', role: 'assistant', content: '', streaming: true }],
    })
    const before = useChatStore.getState().messages

    // 模拟流式期间连续 20 次增量
    for (let index = 0; index < 20; index += 1) {
      useChatStore.getState().appendDelta('x')
      useChatStore.getState().flushDeltas()
    }
    const after = useChatStore.getState().messages

    expect(after).toHaveLength(6)
    // 前 5 条历史消息必须是**同一个对象**：memo 的浅比较才会命中并跳过重渲染
    for (let index = 0; index < 5; index += 1) {
      expect(after[index]).toBe(before[index])
    }
    // 只有最后一条被替换成新对象
    expect(after[5]).not.toBe(before[5])
    expect(after[5].content).toHaveLength(20)
  })

  it('节流：流式期间不会每个 token 都触发渲染', () => {
    useChatStore.setState({
      messages: [{ id: 'a1', role: 'assistant', content: '', streaming: true }],
    })
    let updates = 0
    const unsubscribe = useChatStore.subscribe(() => {
      updates += 1
    })
    for (let index = 0; index < 500; index += 1) {
      useChatStore.getState().appendDelta('x')
    }
    const during = updates
    useChatStore.getState().flushDeltas()
    unsubscribe()

    // 500 个 token 在 50ms 内应合并成个位数次更新（每次 2048 字或 50ms 一次）
    expect(during).toBeLessThan(10)
    expect(useChatStore.getState().messages[0].content).toHaveLength(500)
  })

  it('未闭合代码块（流式中）也能安全渲染，不抛错', () => {
    const partial = '```python\ndef foo():\n    return 1\n\n# 还没写完'
    const message = assistant(partial, true)
    const { container } = render(<MessageBubble message={message} />)
    expect(container.textContent).toContain('def foo():')
  })
})
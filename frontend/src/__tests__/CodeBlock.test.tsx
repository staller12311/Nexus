import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it } from 'vitest'
import { CodeBlock } from '../components/CodeBlock'
import { MessageBubble } from '../components/MessageBubble'
import type { ChatMessage } from '../types'

const CODE = 'def add(a, b):\n    return a + b\n'

describe('CodeBlock', () => {
  it('渲染语言标签、行号与代码内容', () => {
    const { container } = render(<CodeBlock language="python" value={CODE} />)
    expect(screen.getByText('python')).toBeInTheDocument()
    expect(screen.getByLabelText('复制代码')).toBeInTheDocument()
    // 行号由 showLineNumbers 渲染
    expect(container.textContent).toContain('1')
    expect(container.textContent).toContain('def')
    expect(container.textContent).toContain('return a + b')
  })

  it('长代码放进横向滚动容器（不撑爆气泡）', () => {
    const longLine = `x = "${'a'.repeat(400)}"`
    render(<CodeBlock language="python" value={longLine} />)
    const scroller = screen.getByTestId('code-scroll')
    expect(scroller.className).toContain('overflow-x-auto')
  })

  it('点「复制代码」把原文写进剪贴板，并短暂显示「已复制」', async () => {
    const user = userEvent.setup() // user-event 会装一个真实的剪贴板桩，正好用来读回内容
    render(<CodeBlock language="python" value={CODE} />)

    await user.click(screen.getByLabelText('复制代码'))
    await waitFor(() => expect(screen.getByText('已复制')).toBeInTheDocument())
    await expect(navigator.clipboard.readText()).resolves.toBe(CODE)
  })

  it('未注册的语言按纯文本渲染，不报错', () => {
    render(<CodeBlock language="brainfuck" value="+++[>+<]-." />)
    expect(screen.getByText('text')).toBeInTheDocument()
    expect(document.body.textContent).toContain('+++[>+<]-.')
  })
})

describe('MessageBubble 里的代码块', () => {
  const assistant = (content: string): ChatMessage => ({
    id: 'm1',
    role: 'assistant',
    content,
  })

  it('围栏代码块走 CodeBlock（有复制按钮），行内 code 仍是行内', () => {
    const md = '先看代码：\n\n```python\nprint("hi")\n```\n\n行内 `inline_code` 保持原样。'
    render(<MessageBubble message={assistant(md)} />)

    expect(screen.getByText('python')).toBeInTheDocument()
    expect(screen.getByLabelText('复制代码')).toBeInTheDocument()
    expect(screen.getByText('inline_code')).toBeInTheDocument()
    // 行内代码不应带复制按钮之外的结构
    expect(document.querySelectorAll('[data-testid="code-scroll"]')).toHaveLength(1)
  })

  it('没有语言标识但有多行的代码，也按代码块渲染', () => {
    const md = '```\nline one\nline two\n```'
    render(<MessageBubble message={assistant(md)} />)
    expect(screen.getByTestId('code-scroll')).toBeInTheDocument()
  })
})
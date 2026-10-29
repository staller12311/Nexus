import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { Composer } from '../components/Composer'
import { MessageBubble } from '../components/MessageBubble'
import { useChatStore } from '../store/chatStore'
import type { ChatMessage } from '../types'

const writeText = vi.fn(async (_text: string) => undefined)

function stubClipboard() {
  Object.defineProperty(navigator, 'clipboard', {
    value: { writeText },
    configurable: true,
    writable: true,
  })
}

function userMessage(content = '用 Python 写个快排'): ChatMessage {
  return { id: 'u1', role: 'user', content }
}

function assistantMessage(content = '好的，这是快排', reasoning?: string): ChatMessage {
  return {
    id: 'a1',
    role: 'assistant',
    content,
    ...(reasoning ? { reasoning, reasoningStreaming: false } : {}),
  }
}

describe('消息操作按钮（复制 / 编辑）', () => {
  beforeEach(() => {
    useChatStore.getState().reset()
    vi.clearAllMocks()
    stubClipboard()
  })
  afterEach(() => {
    // @ts-expect-error 清理测试桩
    delete navigator.clipboard
  })

  it('用户消息有「复制」和「编辑」，AI 消息只有「复制」', () => {
    const { unmount } = render(<MessageBubble message={userMessage()} />)
    expect(screen.getByLabelText('复制')).toBeInTheDocument()
    expect(screen.getByLabelText('编辑')).toBeInTheDocument()
    unmount()

    render(<MessageBubble message={assistantMessage()} />)
    expect(screen.getByLabelText('复制')).toBeInTheDocument()
    expect(screen.queryByLabelText('编辑')).not.toBeInTheDocument()
  })

  it('点复制：写入剪贴板 + 图标变成对勾「已复制」，1.5 秒后恢复', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime })
    stubClipboard()
    render(<MessageBubble message={userMessage('要复制的内容')} />)

    await user.click(screen.getByLabelText('复制'))

    await waitFor(() => expect(writeText).toHaveBeenCalledWith('要复制的内容'))
    expect(screen.getByLabelText('已复制')).toBeInTheDocument()
    expect(screen.getByText('已复制')).toBeInTheDocument()

    // 1.5 秒后回到可再复制的状态
    await vi.advanceTimersByTimeAsync(1600)
    await waitFor(() => expect(screen.getByLabelText('复制')).toBeInTheDocument())
    vi.useRealTimers()
  })

  it('复制 AI 消息时只复制正文，不包含思考过程', async () => {
    const user = userEvent.setup()
    stubClipboard()
    render(<MessageBubble message={assistantMessage('最终答案', '这是很长的思考过程')} />)

    await user.click(screen.getByLabelText('复制'))

    await waitFor(() => expect(writeText).toHaveBeenCalledWith('最终答案'))
    expect(writeText).not.toHaveBeenCalledWith('这是很长的思考过程')
    expect(writeText.mock.calls[0][0]).not.toContain('思考过程')
  })

  it('剪贴板不可用时不崩、不显示假成功', async () => {
    const user = userEvent.setup()
    // setup 之后再抹掉 clipboard，模拟浏览器拒绝/不支持
    Object.defineProperty(navigator, 'clipboard', { value: undefined, configurable: true })
    render(<MessageBubble message={userMessage('x')} />)

    await user.click(screen.getByLabelText('复制'))

    expect(screen.queryByText('已复制')).not.toBeInTheDocument()
    expect(screen.getByLabelText('复制')).toBeInTheDocument()
  })

  it('点编辑：把用户消息回填到输入框', async () => {
    useChatStore.setState({ messages: [userMessage('原始问题')] })
    const user = userEvent.setup()
    stubClipboard()

    render(
      <>
        <MessageBubble message={userMessage('原始问题')} />
        <Composer />
      </>,
    )

    expect(screen.getByRole('textbox')).toHaveValue('')
    await user.click(screen.getByLabelText('编辑'))

    expect(screen.getByRole('textbox')).toHaveValue('原始问题')
  })

  it('点编辑后 store 会更新草稿并递增聚焦信号', async () => {
    useChatStore.setState({ messages: [userMessage('第二轮追问')] })
    const user = userEvent.setup()
    stubClipboard()
    render(<MessageBubble message={userMessage('第二轮追问')} />)

    const before = useChatStore.getState().draftFocusSignal
    await user.click(screen.getByLabelText('编辑'))

    expect(useChatStore.getState().draft).toBe('第二轮追问')
    expect(useChatStore.getState().draftFocusSignal).toBe(before + 1)
  })

  it('AI 消息的编辑入口不存在（即使 store 里有 assistant 消息）', () => {
    useChatStore.setState({ messages: [assistantMessage('答案')] })
    render(<MessageBubble message={assistantMessage('答案')} />)
    expect(screen.queryByLabelText('编辑')).not.toBeInTheDocument()
  })
})
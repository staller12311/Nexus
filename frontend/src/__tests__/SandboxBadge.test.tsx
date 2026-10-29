import { render, screen } from '@testing-library/react'
import { beforeEach, describe, expect, it } from 'vitest'
import { Composer } from '../components/Composer'
import { useChatStore } from '../store/chatStore'

describe('安全沙箱徽章', () => {
  beforeEach(() => {
    useChatStore.getState().reset()
  })

  it('回形针旁边显示「安全沙箱保护中」徽章', () => {
    render(<Composer />)

    const badge = screen.getByTestId('sandbox-badge')
    expect(badge).toBeInTheDocument()
    expect(badge).toHaveTextContent('安全沙箱保护中')
    // 徽章紧跟在「上传文档到知识库」按钮之后
    const uploadButton = screen.getByLabelText('上传文档到知识库')
    expect(uploadButton.parentElement?.contains(badge)).toBe(true)
    expect(uploadButton.compareDocumentPosition(badge) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
  })

  it('悬浮提示（title + tooltip）说明 workspace 限制与禁止越权', () => {
    render(<Composer />)

    const badge = screen.getByTestId('sandbox-badge')
    const hint = badge.getAttribute('title') ?? ''
    expect(hint).toContain('workspace')
    expect(hint).toContain('无法越权访问系统文件')

    // 样式化 tooltip 也存在（group-hover 显示）
    const tooltip = screen.getByRole('tooltip')
    expect(tooltip).toHaveTextContent('workspace')
    expect(tooltip).toHaveTextContent('无法越权访问系统文件')
  })

  it('徽章不挡住输入与发送（渲染后发送按钮仍在）', () => {
    useChatStore.getState().setDraft('测试')
    render(<Composer />)
    expect(screen.getByRole('button', { name: /发送/ })).toBeInTheDocument()
    expect(screen.getByRole('textbox')).toHaveValue('测试')
  })
})
import { beforeEach, describe, expect, it } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { SummaryNoticeCard } from '../components/SummaryNotice'
import { useChatStore } from '../store/chatStore'

const notice = {
  id: 'n1',
  content: '为节省上下文，已自动压缩前 3 轮对话。',
  summary: '用户之前要求写贪吃蛇，模型修复了 JSON 报错。',
  rounds: 3,
}

describe('上下文摘要通知', () => {
  beforeEach(() => {
    useChatStore.getState().reset()
  })

  it('默认显示极淡系统提示，点击后展示摘要', async () => {
    const user = userEvent.setup()
    render(<SummaryNoticeCard notice={notice} />)

    expect(screen.getByTestId('summary-notice')).toHaveTextContent('[系统提示：为节省上下文，已自动压缩前 3 轮对话。]')
    expect(screen.queryByTestId('summary-notice-detail')).not.toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: /点击查看摘要/ }))
    expect(screen.getByTestId('summary-notice-detail')).toHaveTextContent('贪吃蛇')
  })

  it('store 能记录 SSE notice 并清空', () => {
    useChatStore.getState().addSummaryNotice(notice.content, notice.summary, notice.rounds)
    expect(useChatStore.getState().summaryNotices).toHaveLength(1)
    expect(useChatStore.getState().summaryNotices[0].rounds).toBe(3)
    useChatStore.getState().reset()
    expect(useChatStore.getState().summaryNotices).toHaveLength(0)
  })
})
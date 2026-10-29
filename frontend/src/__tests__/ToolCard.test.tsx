import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it } from 'vitest'
import { ToolCard } from '../components/ToolCard'
import type { ToolCallView } from '../types'

const base: ToolCallView = {
  id: 'call_1',
  name: 'read_file',
  arguments: '{"path": "readme.txt"}',
  status: 'pending',
}

describe('ToolCard', () => {
  it('折叠态：工具名 + 参数摘要 + 状态标签', () => {
    render(<ToolCard call={base} />)
    expect(screen.getByText('read_file')).toBeInTheDocument()
    expect(screen.getByText('{"path":"readme.txt"}')).toBeInTheDocument()
    expect(screen.getByText('执行中')).toBeInTheDocument()
    expect(screen.queryByText('返回结果')).not.toBeInTheDocument()
  })

  it('展开态：格式化后的参数 + 返回结果', async () => {
    const user = userEvent.setup()
    render(<ToolCard call={{ ...base, status: 'success', content: '# readme.txt（2 字节）\nhi' }} />)

    await user.click(screen.getByRole('button'))
    expect(screen.getByText('参数')).toBeInTheDocument()
    expect(screen.getByText(/"path": "readme\.txt"/)).toBeInTheDocument() // pretty print 带缩进
    expect(screen.getByText('返回结果')).toBeInTheDocument()
    expect(screen.getByText(/hi/)).toBeInTheDocument()
    expect(screen.getByText('成功')).toBeInTheDocument()
  })

  it('失败态：标红并把错误信息完整展示在「错误信息」下', async () => {
    const user = userEvent.setup()
    render(
      <ToolCard
        call={{
          ...base,
          id: 'call_err',
          status: 'error',
          content: '错误：文件不存在：nope.txt（workspace 根目录：D:\\MyAi\\workspace）',
        }}
      />,
    )

    expect(screen.getByText('失败')).toBeInTheDocument()
    await user.click(screen.getByRole('button'))
    expect(screen.getByText('错误信息')).toBeInTheDocument()
    expect(screen.getByText(/文件不存在：nope\.txt/)).toBeInTheDocument()
  })

  it('参数不是合法 JSON：原样显示并标注', async () => {
    const user = userEvent.setup()
    render(<ToolCard call={{ ...base, arguments: '{半截非法', status: 'success', content: 'ok' }} />)

    await user.click(screen.getByRole('button'))
    expect(screen.getByText('参数（原始字符串，JSON 解析失败）')).toBeInTheDocument()
    // 折叠态摘要与展开态 pre 都会显示原始串，所以这里应命中两处
    expect(screen.getAllByText('{半截非法')).toHaveLength(2)
  })

  it('pending 展开时提示执行中', async () => {
    const user = userEvent.setup()
    render(<ToolCard call={base} />)
    await user.click(screen.getByRole('button'))
    expect(screen.getByText('（执行中…）')).toBeInTheDocument()
  })
})
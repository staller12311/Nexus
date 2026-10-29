import { describe, expect, it } from 'vitest'
import { reconstructMessages } from '../lib/reconstruct'
import type { ApiMessage } from '../types'

const user = (content: string): ApiMessage => ({ role: 'user', content })
const assistantTools = (content: string | null, calls: [string, string, string][]): ApiMessage => ({
  role: 'assistant',
  content,
  tool_calls: calls.map(([id, name, args]) => ({
    id,
    type: 'function',
    function: { name, arguments: args },
  })),
})
const tool = (id: string, content: string): ApiMessage => ({
  role: 'tool',
  tool_call_id: id,
  content,
})

describe('reconstructMessages', () => {
  it('把 assistant(tool_calls) 与其后的 tool 消息合并成一张卡片', () => {
    const messages = reconstructMessages([
      { role: 'system', content: '你是助手' },
      user('读一下 readme.txt 的内容'),
      assistantTools(null, [['call_read', 'read_file', '{"path": "readme.txt"}']]),
      tool('call_read', '# readme.txt（2 字节）\nhi'),
      { role: 'assistant', content: 'readme.txt 里写的是：hi' },
    ])

    expect(messages).toHaveLength(3) // system 被丢掉
    expect(messages[0]).toMatchObject({ role: 'user', content: '读一下 readme.txt 的内容' })

    const withTool = messages[1]
    expect(withTool.role).toBe('assistant')
    expect(withTool.content).toBeNull() // content=null + tool_calls：只渲染卡片
    expect(withTool.toolCalls).toHaveLength(1)
    expect(withTool.toolCalls?.[0]).toEqual({
      id: 'call_read',
      name: 'read_file',
      arguments: '{"path": "readme.txt"}',
      status: 'success',
      content: '# readme.txt（2 字节）\nhi',
    })

    expect(messages[2]).toMatchObject({ role: 'assistant', content: 'readme.txt 里写的是：hi' })
    expect(messages[2].toolCalls).toBeUndefined()
  })

  it('两轮工具调用（A→tool→B→tool→C）：落库 7 条还原成 4 条，A/B 文字与卡片都在', () => {
    const messages = reconstructMessages([
      { role: 'system', content: '你是助手' },
      user('现在几点'),
      assistantTools('A：我先看看时间。', [['call_a', 'get_time', '{"zone": "UTC"}']]),
      tool('call_a', '当前时间（UTC）：2026-10-27T08:07:15+00:00'),
      assistantTools('B：再看一次时间。', [['call_b', 'get_time', '{"zone": "LOCAL"}']]),
      tool('call_b', '当前时间（本地）：2026-10-27T16:07:15+08:00'),
      { role: 'assistant', content: 'C：最终答案就是现在的时间。' },
    ])

    expect(messages).toHaveLength(4)
    expect(messages[1].content).toBe('A：我先看看时间。')
    expect(messages[1].toolCalls?.[0]).toMatchObject({ id: 'call_a', status: 'success' })
    expect(messages[2].content).toBe('B：再看一次时间。')
    expect(messages[2].toolCalls?.[0]).toMatchObject({ id: 'call_b', status: 'success' })
    expect(messages[3].content).toBe('C：最终答案就是现在的时间。')
  })

  it('一条 assistant 带多个并行 tool_calls：各自配对自己的结果', () => {
    const messages = reconstructMessages([
      user('看看时间和目录'),
      assistantTools(null, [
        ['call_1', 'get_time', '{}'],
        ['call_2', 'list_dir', '{"path": "."}'],
      ]),
      tool('call_1', '当前时间（UTC）：2026-10-27T08:00:00+00:00'),
      tool('call_2', 'readme.txt'),
    ])

    expect(messages).toHaveLength(2)
    const calls = messages[1].toolCalls ?? []
    expect(calls).toHaveLength(2)
    expect(calls[0]).toMatchObject({ id: 'call_1', name: 'get_time', status: 'success' })
    expect(calls[1]).toMatchObject({ id: 'call_2', name: 'list_dir', status: 'success' })
    expect(calls[1].content).toBe('readme.txt')
  })

  it('历史里失败的工具（结果是「错误：」前缀）标成 error 而不是 success', () => {
    const messages = reconstructMessages([
      user('读一下 nope.txt'),
      assistantTools(null, [['call_x', 'read_file', '{"path": "nope.txt"}']]),
      tool('call_x', '错误：文件不存在：nope.txt（workspace 根目录：D:\\MyAi\\workspace）'),
    ])

    expect(messages[1].toolCalls?.[0].status).toBe('error')
    expect(messages[1].toolCalls?.[0].content).toContain('文件不存在')
  })

  it('有 tool_calls 但没有对应结果：标成 error，避免永远转圈', () => {
    const messages = reconstructMessages([
      user('几点'),
      assistantTools(null, [['call_lost', 'get_time', '{}']]),
    ])

    expect(messages[1].toolCalls?.[0].status).toBe('error')
    expect(messages[1].toolCalls?.[0].content).toContain('没有拿到结果')
  })

  it('孤儿 tool 消息（找不到归属）：忽略，不产生空消息', () => {
    const messages = reconstructMessages([user('hi'), tool('call_orphan', '没人认领的结果')])
    expect(messages).toHaveLength(1)
    expect(messages[0].role).toBe('user')
  })

  it('空序列 / 只有 system：返回空数组（界面显示空状态而不是白屏）', () => {
    expect(reconstructMessages([])).toEqual([])
    expect(reconstructMessages([{ role: 'system', content: '只有系统提示' }])).toEqual([])
  })

  it('生成的每条消息都有唯一 id', () => {
    const messages = reconstructMessages([
      user('一'),
      { role: 'assistant', content: '二' },
      user('三'),
    ])
    const ids = messages.map((message) => message.id)
    expect(new Set(ids).size).toBe(ids.length)
  })

  it('历史里的 reasoning_content 还原成折叠面板用的 reasoning（已完成态）', () => {
    const messages = reconstructMessages([
      user('9.11 和 9.9 哪个大'),
      {
        role: 'assistant',
        content: '9.9 更大',
        reasoning_content: '先把两个数写成同样的小数位数…',
      },
    ])
    expect(messages[1].reasoning).toBe('先把两个数写成同样的小数位数…')
    expect(messages[1].reasoningStreaming).toBe(false) // 历史消息不是"思考中"
  })

  it('没有 reasoning_content 的旧消息不带 reasoning 字段', () => {
    const messages = reconstructMessages([user('hi'), { role: 'assistant', content: '你好' }])
    expect(messages[1]).not.toHaveProperty('reasoning')
    expect(messages[1]).not.toHaveProperty('reasoningStreaming')
  })

  it('空白 reasoning_content 不产生空面板', () => {
    const messages = reconstructMessages([
      { role: 'assistant', content: '答', reasoning_content: '   ' },
    ])
    expect(messages[0]).not.toHaveProperty('reasoning')
  })
})
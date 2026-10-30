import { describe, expect, it, vi } from 'vitest'
import { parseSSEStream, streamChat, type ServerEvent } from '../lib/sse'

function streamOf(chunks: string[]): ReadableStream<Uint8Array> {
  const encoder = new TextEncoder()
  return new ReadableStream<Uint8Array>({
    start(controller) {
      for (const chunk of chunks) controller.enqueue(encoder.encode(chunk))
      controller.close()
    },
  })
}

async function collect(chunks: string[]): Promise<ServerEvent[]> {
  const events: ServerEvent[] = []
  for await (const event of parseSSEStream(streamOf(chunks))) events.push(event)
  return events
}

describe('parseSSEStream', () => {
  it('一次读到多帧：全部解析，顺序保持', async () => {
    const events = await collect([
      'data: {"type":"message_start","session_id":"s1"}\n\n' +
        'data: {"type":"text_delta","delta":"你"}\n\n' +
        'data: {"type":"message_end","content":"你好"}\n\n',
    ])
    expect(events.map((event) => event.type)).toEqual(['message_start', 'text_delta', 'message_end'])
    expect(events[1].delta).toBe('你')
  })

  it('跨 chunk 截断：一个 JSON 被切成两半也能正确解析', async () => {
    const events = await collect([
      'data: {"type":"tool_call_start","tool_call_id":"call_1","name":"read_file",',
      '"arguments":"{\\"path\\": \\"readme.txt\\"}"}\n\n',
    ])
    expect(events).toHaveLength(1)
    expect(events[0]).toMatchObject({
      type: 'tool_call_start',
      tool_call_id: 'call_1',
      name: 'read_file',
    })
    expect(events[0].arguments).toBe('{"path": "readme.txt"}')
  })

  it('帧边界被切在 \\n\\n 中间（一个 chunk 以 \\n 结尾）也能正确分帧', async () => {
    const events = await collect([
      'data: {"type":"text_delta","delta":"A"}\n',
      '\ndata: {"type":"text_delta","delta":"B"}\n\n',
    ])
    expect(events.map((event) => event.delta)).toEqual(['A', 'B'])
  })

  it('逐字节喂入（最极端切分）仍不丢帧', async () => {
    const raw = 'data: {"type":"text_delta","delta":"逐字节"}\n\ndata: {"type":"message_end"}\n\n'
    const events = await collect(raw.split(''))
    expect(events.map((event) => event.type)).toEqual(['text_delta', 'message_end'])
    expect(events[0].delta).toBe('逐字节')
  })

  it('坏帧被丢弃，后续帧照常解析', async () => {
    const events = await collect([
      'data: {"type":"text_delta","delta":"ok1"}\n\n' +
        'data: {半截的非法 JSON\n\n' +
        'data: {"type":"text_delta","delta":"ok2"}\n\n',
    ])
    expect(events.map((event) => event.delta)).toEqual(['ok1', 'ok2'])
  })

  it('非 data 帧（注释/心跳）被忽略', async () => {
    const events = await collect([': keep-alive\n\n' + 'data: {"type":"message_end"}\n\n'])
    expect(events.map((event) => event.type)).toEqual(['message_end'])
  })

  it('流结束时未收尾的半帧不产出', async () => {
    const events = await collect(['data: {"type":"text_delta","delta":"x"}\n\n', 'data: {"type":"text'])
    expect(events.map((event) => event.type)).toEqual(['text_delta'])
  })

  it('缺少 type 字段的对象被忽略', async () => {
    const events = await collect(['data: {"foo":"bar"}\n\n', 'data: {"type":"message_end"}\n\n'])
    expect(events.map((event) => event.type)).toEqual(['message_end'])
  })
})

  it('streamChat 把 attachments 放进 POST 请求体', async () => {
    let body: Record<string, unknown> = {}
    vi.stubGlobal(
      'fetch',
      vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
        body = JSON.parse(String(init?.body ?? '{}')) as Record<string, unknown>
        return new Response(streamOf(['data: {"type":"message_end"}\n\n']), {
          status: 200,
          headers: { 'Content-Type': 'text/event-stream' },
        })
      }),
    )
    const events: ServerEvent[] = []
    await streamChat({
      sessionId: 's1',
      message: '看图',
      attachments: [{ id: 'a'.repeat(32) + '.png' }],
      onEvent: (event) => events.push(event),
    })
    expect(body.attachments).toEqual([{ id: 'a'.repeat(32) + '.png' }])
    expect(events[0].type).toBe('message_end')
    vi.unstubAllGlobals()
  })

import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ChatArea } from '../components/ChatArea'
import { Composer } from '../components/Composer'
import { KnowledgePanel } from '../components/KnowledgePanel'
import { useChatStore } from '../store/chatStore'
import type { KnowledgeDoc } from '../types'

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

function kbDoc(name: string, status = 'ready'): KnowledgeDoc {
  return {
    id: `d-${name}`,
    name,
    size: 2048,
    pages: 1,
    chunk_count: 4,
    status,
    mode: 'keyword',
    error: null,
    created_at: '2026-10-28T05:00:00Z',
  }
}

/** 可控的假 XHR：手动触发进度，便于断言"上传中"卡片。 */
class FakeXHR {
  static instances: FakeXHR[] = []
  method = ''
  url = ''
  status = 201
  responseText = '{}'
  upload = { onprogress: null as ((event: ProgressEvent) => void) | null }
  onload: (() => void) | null = null
  onerror: (() => void) | null = null
  sent: FormData | null = null
  /** 记录 setRequestHeader，用来断言上传是否带上了会话头 */
  headers: Record<string, string> = {}

  constructor() {
    FakeXHR.instances.push(this)
  }
  open(method: string, url: string) {
    this.method = method
    this.url = url
  }
  setRequestHeader(name: string, value: string) {
    this.headers[name] = value
  }
  send(body: FormData) {
    this.sent = body
  }
  emitProgress(loaded: number, total = 100) {
    this.upload.onprogress?.({ lengthComputable: true, loaded, total } as ProgressEvent)
  }
  finish(status = 201, payload: unknown = kbDoc('README.md', 'parsing')) {
    this.status = status
    this.responseText = JSON.stringify(payload)
    this.onload?.()
  }
}

function stubFetch(docsAfterUpload: KnowledgeDoc[]) {
  let docs = [] as KnowledgeDoc[]
  const calls: string[] = []
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      calls.push(url)
      if (url.endsWith('/api/kb/status')) {
        return jsonResponse({
          docs: docs.length,
          chunks: docs.length * 4,
          mode: 'keyword',
          supported: ['.docx', '.md', '.pdf', '.txt'],
          max_file_mb: 10,
          pipeline: 'BM25 关键词检索（中文双字 + 英文整词）',
        })
      }
      if (url.endsWith('/api/kb/docs')) {
        // 上传完成后立刻能在列表里看到（模拟后端已落库）
        docs = docsAfterUpload
        return jsonResponse({ docs })
      }
      if (url.endsWith('/api/sessions')) {
        return jsonResponse(
          {
            id: 's-auto',
            title: '',
            model: 'deepseek',
            created_at: '2026-11-01T00:00:00Z',
            updated_at: '2026-11-01T00:00:00Z',
            message_count: 0,
            total_tokens: 0,
          },
          201,
        )
      }
      return jsonResponse({ sessions: [] })
    }),
  )
  return calls
}

function renderComposer() {
  return render(
    <>
      <ChatArea />
      <Composer />
    </>,
  )
}

describe('输入框上传文档（回形针 + 聊天卡片）', () => {
  beforeEach(() => {
    useChatStore.getState().reset()
    vi.unstubAllGlobals()
    FakeXHR.instances = []
    vi.stubGlobal('XMLHttpRequest', FakeXHR)
    vi.useRealTimers()
  })
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('回形针按钮存在，且 accept 来自后端 supported 列表', async () => {
    stubFetch([kbDoc('README.md')])
    await useChatStore.getState().loadKb()
    renderComposer()

    expect(screen.getByLabelText('上传文档到知识库')).toBeInTheDocument()
    const input = screen.getByLabelText('选择要上传的文档') as HTMLInputElement
    expect(input).toHaveAttribute('accept', '.docx,.md,.pdf,.txt')
    expect(input.multiple).toBe(true)
  })

  it('本会话没有专属文档、但全局库有文档时，输入框上方给出全局知识库提示条', async () => {
    stubFetch([kbDoc('全局财报.pdf')])
    useChatStore.setState({ sessionId: 's-hint' })
    await useChatStore.getState().loadKb()
    renderComposer()

    const hint = await screen.findByTestId('kb-global-hint')
    expect(hint).toHaveTextContent('已加载全局知识库（1 个文档）')
  })

  it('本会话已有专属文档时，不再提示全局知识库', async () => {
    stubFetch([
      kbDoc('全局财报.pdf'),
      { ...kbDoc('本会话纪要.docx'), session_id: 's-hint' },
    ])
    useChatStore.setState({ sessionId: 's-hint' })
    await useChatStore.getState().loadKb()
    renderComposer()

    await waitFor(() => expect(screen.getByLabelText('上传文档到知识库')).toBeInTheDocument())
    expect(screen.queryByTestId('kb-global-hint')).not.toBeInTheDocument()
  })

  it('没有会话时上传：先自动建会话，上传请求带上 X-Nexus-Session', async () => {
    stubFetch([kbDoc('会话纪要.docx')])
    const user = userEvent.setup()
    renderComposer()
    // 前置：确实处于「没有会话」的状态
    expect(useChatStore.getState().sessionId).toBeNull()

    await user.upload(
      screen.getByLabelText('选择要上传的文档'),
      new File(['x'], '会话纪要.docx', {
        type: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
      }),
    )

    await waitFor(() => expect(FakeXHR.instances.length).toBeGreaterThan(0))
    // 关键断言：请求头带上了刚建出来的会话，文档才会落成「会话专属」而不是全局
    expect(FakeXHR.instances[0].headers['X-Nexus-Session']).toBe('s-auto')
    expect(useChatStore.getState().sessionId).toBe('s-auto')
  })

  it('选中文件后：立刻出现「正在上传文档 xxx」卡片，并带进度条', async () => {
    stubFetch([kbDoc('README.md')])
    const user = userEvent.setup()
    render(
      <>
        <ChatArea />
        <Composer />
      </>,
    )

    await user.upload(
      screen.getByLabelText('选择要上传的文档'),
      new File(['# README'], 'README.md', { type: 'text/markdown' }),
    )

    await waitFor(() =>
      expect(screen.getByText('正在上传文档 README.md')).toBeInTheDocument(),
    )

    const xhr = FakeXHR.instances.at(-1)!
    expect(xhr.url).toContain('/api/kb/docs')
    expect((xhr.sent as FormData).get('file')).toBeInstanceOf(File)

    // 进度 42% → 进度条宽度跟着变
    xhr.emitProgress(42)
    await waitFor(() => {
      const card = screen.getByTestId('kb-upload-cards')
      const bar = within(card).getAllByTestId(/kb-upload-progress-/)[0]
      expect(bar).toHaveStyle({ width: '42%' })
    })
  })

  it('上传完成 → 解析中 → 变成「文档已加入知识库」', async () => {
    const calls = stubFetch([kbDoc('README.md', 'ready')])
    const user = userEvent.setup()
    render(
      <>
        <ChatArea />
        <Composer />
      </>,
    )

    await user.upload(
      screen.getByLabelText('选择要上传的文档'),
      new File(['# README'], 'README.md', { type: 'text/markdown' }),
    )
    await waitFor(() => expect(screen.getByText('正在上传文档 README.md')).toBeInTheDocument())

    FakeXHR.instances.at(-1)!.emitProgress(100)
    FakeXHR.instances.at(-1)!.finish()

    await waitFor(() => expect(screen.getByText('文档已加入知识库')).toBeInTheDocument())
    // 轮询确实发生了，且知识库列表被刷新
    expect(calls.filter((url) => url.endsWith('/api/kb/docs')).length).toBeGreaterThan(0)
    expect(useChatStore.getState().kbDocs[0].name).toBe('README.md')
  })

  it('上传失败：卡片变红并显示原因', async () => {
    stubFetch([])
    const user = userEvent.setup()
    renderComposer()

    await user.upload(
      screen.getByLabelText('选择要上传的文档'),
      new File(['x'], 'big.pdf', { type: 'application/pdf' }),
    )
    await waitFor(() => expect(screen.getByText('正在上传文档 big.pdf')).toBeInTheDocument())

    FakeXHR.instances.at(-1)!.finish(400, { detail: '文件过大（12.0MB），上限 10MB' })

    await waitFor(() =>
      expect(screen.getByText(/上传失败：文件过大/)).toBeInTheDocument(),
    )
  })

  it('多选：每个文件一张卡片', async () => {
    stubFetch([kbDoc('a.md'), kbDoc('b.md')])
    const user = userEvent.setup()
    renderComposer()

    await user.upload(screen.getByLabelText('选择要上传的文档'), [
      new File(['a'], 'a.md', { type: 'text/markdown' }),
      new File(['b'], 'b.md', { type: 'text/markdown' }),
    ])

    await waitFor(() => expect(screen.getByText('正在上传文档 a.md')).toBeInTheDocument())
    expect(screen.getByText('正在上传文档 b.md')).toBeInTheDocument()
  })

  it('已完成的卡片可以关闭', async () => {
    stubFetch([kbDoc('README.md', 'ready')])
    const user = userEvent.setup()
    renderComposer()

    await user.upload(
      screen.getByLabelText('选择要上传的文档'),
      new File(['# README'], 'README.md', { type: 'text/markdown' }),
    )
    await waitFor(() => expect(screen.getByText('正在上传文档 README.md')).toBeInTheDocument())
    FakeXHR.instances.at(-1)!.finish()

    await waitFor(() => expect(screen.getByText('文档已加入知识库')).toBeInTheDocument())
    await user.click(screen.getByLabelText('关闭上传卡片'))
    expect(screen.queryByText('文档已加入知识库')).not.toBeInTheDocument()
  })

  // ---- 会话归属可见性（用户反馈：两段对话各自传的文件都显示"本会话"，分不清谁的）----
  it('输入框上方只列当前会话的文档，别的会话与全局文档不混进来', async () => {
    stubFetch([
      { ...kbDoc('本会话纪要.docx'), session_id: 's-cur' },
      { ...kbDoc('另一段对话的合同.pdf'), session_id: 's-other' },
      kbDoc('全局财报.pdf'),
    ])
    useChatStore.setState({ sessionId: 's-cur' })
    await useChatStore.getState().loadKb()
    renderComposer()

    const chips = await screen.findByTestId('kb-own-chips')
    expect(chips).toHaveTextContent('本会话纪要.docx')
    expect(chips).not.toHaveTextContent('另一段对话的合同.pdf')
    expect(chips).not.toHaveTextContent('全局财报.pdf')
  })

  it('知识库面板按归属分组，其他会话的文档带 session 前缀', async () => {
    stubFetch([
      { ...kbDoc('本会话纪要.docx'), session_id: 's-cur' },
      { ...kbDoc('另一段对话的合同.pdf'), session_id: 's-other' },
      kbDoc('全局财报.pdf'),
    ])
    useChatStore.setState({ sessionId: 's-cur' })
    await useChatStore.getState().loadKb()
    render(<KnowledgePanel />)

    const own = await screen.findByTestId('kb-group-own')
    expect(own).toHaveTextContent('本会话（1）')
    expect(own).toHaveTextContent('本会话纪要.docx')

    const others = screen.getByTestId('kb-group-others')
    expect(others).toHaveTextContent('其他会话（1）')
    // 关键断言：这段前缀是「分不清哪个文件属于哪段对话」的修复点
    expect(others).toHaveTextContent('其他会话 s-othe')

    const global = screen.getByTestId('kb-group-global')
    expect(global).toHaveTextContent('全局（所有会话可见）（1）')
  })
})
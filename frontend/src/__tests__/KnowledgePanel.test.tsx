import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { KnowledgePanel } from '../components/KnowledgePanel'
import { useChatStore } from '../store/chatStore'
import type { KnowledgeDoc, KnowledgeStatus } from '../types'

function status(overrides: Partial<KnowledgeStatus> = {}): KnowledgeStatus {
  return {
    docs: 1,
    chunks: 3,
    mode: 'keyword',
    supported: ['.docx', '.md', '.pdf', '.txt'],
    max_file_mb: 10,
    pipeline: 'BM25 关键词检索（中文双字 + 英文整词）',
    ...overrides,
  }
}

function doc(overrides: Partial<KnowledgeDoc> = {}): KnowledgeDoc {
  return {
    id: 'd1',
    name: '报销制度.txt',
    size: 1024,
    pages: 1,
    chunk_count: 3,
    status: 'ready',
    mode: 'keyword',
    error: null,
    created_at: '2026-10-28T03:00:00Z',
    ...overrides,
  }
}

function jsonResponse(body: unknown, statusCode = 200): Response {
  return new Response(JSON.stringify(body), {
    status: statusCode,
    headers: { 'Content-Type': 'application/json' },
  })
}

/** 极简 XHR 替身：能触发 upload.onprogress 与 onload。 */
class FakeXHR {
  static last: FakeXHR | null = null
  method = ''
  url = ''
  status = 201
  responseText = '{}'
  upload = { onprogress: null as ((event: ProgressEvent) => void) | null }
  onload: (() => void) | null = null
  onerror: (() => void) | null = null
  sent: FormData | null = null

  constructor() {
    FakeXHR.last = this
  }

  open(method: string, url: string) {
    this.method = method
    this.url = url
  }

  send(body: FormData) {
    this.sent = body
    // 模拟上传进度 50% → 100% → 完成
    setTimeout(() => this.upload.onprogress?.({ lengthComputable: true, loaded: 50, total: 100 } as ProgressEvent), 0)
    setTimeout(() => {
      this.upload.onprogress?.({ lengthComputable: true, loaded: 100, total: 100 } as ProgressEvent)
      this.responseText = JSON.stringify(doc({ status: 'parsing', chunk_count: 0 }))
      this.onload?.()
    }, 1)
  }
}

function stubBackend(options: { docs?: KnowledgeDoc[] } = {}) {
  const calls: string[] = []
  const docs = options.docs ?? []
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      calls.push(url)
      if (url.endsWith('/api/kb/status')) return jsonResponse(status({ docs: docs.length }))
      if (url.includes('/api/kb/docs/') && !url.endsWith('/api/kb/docs')) {
        const id = url.split('/').pop() ?? ''
        return jsonResponse(doc({ id }))
      }
      if (url.endsWith('/api/kb/docs')) return jsonResponse({ docs })
      if (url.startsWith('/api/sessions')) return jsonResponse({ sessions: [] })
      return jsonResponse({}, 404)
    }),
  )
  return calls
}

describe('知识库面板', () => {
  beforeEach(() => {
    useChatStore.getState().reset()
    vi.unstubAllGlobals()
    FakeXHR.last = null
    vi.stubGlobal('XMLHttpRequest', FakeXHR)
  })
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('加载时显示文档数、片段数与检索管线', async () => {
    stubBackend({ docs: [doc()] })
    render(<KnowledgePanel />)

    await waitFor(() => expect(screen.getByTestId('kb-pipeline')).toHaveTextContent(/BM25/))
    expect(screen.getByTestId('kb-doc-d1')).toHaveTextContent('报销制度.txt')
    expect(screen.getByTestId('kb-doc-d1')).toHaveTextContent('就绪 · 3 片段')
    expect(screen.getByTestId('kb-doc-d1')).toHaveTextContent('1 KB')
  })

  it('解析中的文档显示「解析中…」和转圈图标', async () => {
    stubBackend({ docs: [doc({ status: 'parsing', chunk_count: 0 })] })
    render(<KnowledgePanel />)
    await waitFor(() => expect(screen.getByTestId('kb-doc-d1')).toHaveTextContent('解析中…'))
  })

  it('失败的文档显示错误原因', async () => {
    stubBackend({
      docs: [doc({ status: 'failed', error: '没有解析出任何文本（可能是扫描版 PDF，需要 OCR）' })],
    })
    render(<KnowledgePanel />)
    await waitFor(() =>
      expect(screen.getByTestId('kb-doc-d1')).toHaveTextContent(/扫描版 PDF/),
    )
  })

  it('选择文件后走 XHR 上传，并显示上传百分比', async () => {
    stubBackend({ docs: [] })
    const user = userEvent.setup()
    render(<KnowledgePanel />)
    await waitFor(() => expect(screen.getByLabelText('选择文件上传')).toBeInTheDocument())

    const file = new File(['# 标题\n内容'], '报销制度.txt', { type: 'text/plain' })
    await user.upload(screen.getByLabelText('选择文件上传'), file)

    await waitFor(() => expect(FakeXHR.last).not.toBeNull())
    expect(FakeXHR.last?.method).toBe('POST')
    expect(FakeXHR.last?.url).toContain('/api/kb/docs')
    // FormData 里确实带了文件
    expect((FakeXHR.last?.sent as FormData).get('file')).toBeInstanceOf(File)
  })

  it('上传失败时把后端 detail 显示出来（不静默）', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input)
        if (url.endsWith('/api/kb/status')) return jsonResponse(status())
        if (url.endsWith('/api/kb/docs')) return jsonResponse({ docs: [] })
        return jsonResponse({ sessions: [] })
      }),
    )
    class FailingXHR extends FakeXHR {
      send(body: FormData) {
        this.sent = body
        setTimeout(() => {
          this.status = 400
          this.responseText = JSON.stringify({ detail: '文件过大（12.0MB），上限 10MB' })
          this.onload?.()
        }, 1)
      }
    }
    vi.stubGlobal('XMLHttpRequest', FailingXHR)

    const user = userEvent.setup()
    render(<KnowledgePanel />)
    await waitFor(() => expect(screen.getByLabelText('选择文件上传')).toBeInTheDocument())
    await user.upload(
      screen.getByLabelText('选择文件上传'),
      new File(['x'], '超大文件.pdf', { type: 'application/pdf' }),
    )

    await waitFor(() => expect(screen.getByText(/文件过大/)).toBeInTheDocument())
  })

  it('删除需要二次确认，确认后从列表移除', async () => {
    const calls = stubBackend({ docs: [doc()] })
    const user = userEvent.setup()
    render(<KnowledgePanel />)
    await waitFor(() => expect(screen.getByTestId('kb-doc-d1')).toBeInTheDocument())

    await user.click(screen.getByLabelText('删除 报销制度.txt'))
    await user.click(screen.getByRole('button', { name: '确认' }))

    await waitFor(() => expect(screen.queryByTestId('kb-doc-d1')).not.toBeInTheDocument())
    expect(calls.some((url) => url.includes('/api/kb/docs/d1'))).toBe(true)
  })

  it('面板可以折叠', async () => {
    stubBackend({ docs: [doc()] })
    const user = userEvent.setup()
    render(<KnowledgePanel />)
    await waitFor(() => expect(screen.getByTestId('kb-doc-d1')).toBeInTheDocument())

    await user.click(screen.getByLabelText('知识库'))
    expect(screen.queryByTestId('kb-doc-d1')).not.toBeInTheDocument()
    expect(screen.getByLabelText('知识库')).toHaveAttribute('aria-expanded', 'false')
  })
})
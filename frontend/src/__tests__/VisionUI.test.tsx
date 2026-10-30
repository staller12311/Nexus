import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { Composer } from '../components/Composer'
import { MessageBubble } from '../components/MessageBubble'
import { reconstructMessages } from '../lib/reconstruct'
import { useChatStore } from '../store/chatStore'
import type { Attachment, ChatMessage, ModelInfo } from '../types'

vi.mock('../lib/imageCompress', () => ({
  compressImageFile: vi.fn(async (file: File) => new File(['jpeg-bytes'], `${file.name}.jpg`, { type: 'image/jpeg' })),
}))

const ATTACHMENT: Attachment = {
  id: 'a'.repeat(32) + '.jpg',
  url: `/api/uploads/${'a'.repeat(32)}.jpg`,
  mime: 'image/jpeg',
  name: 'cat.jpg',
  size: 200_000,
}

function model(name: string, supportsVision: boolean): ModelInfo {
  return {
    name,
    model: name,
    base_url: 'http://127.0.0.1:8128/v1',
    api_key_env: 'FAKE_KEY',
    key_set: true,
    is_default: true,
    context_window: 1_000_000,
    is_custom: false,
    has_key: true,
    api_key_masked: '',
    supports_vision: supportsVision,
  }
}

/** 只处理上传/工作区的 XHR：上传返回固定附件，不触发真实聊天。 */
class FakeXHR {
  static instances: FakeXHR[] = []
  method = ''
  url = ''
  status = 201
  responseText = '{}'
  upload = { onprogress: null as null | ((event: ProgressEvent) => void) }
  onload: (() => void) | null = null
  onerror: (() => void) | null = null
  sent: FormData | null = null

  constructor() {
    FakeXHR.instances.push(this)
  }
  open(method: string, url: string) {
    this.method = method
    this.url = url
  }
  send(body: FormData) {
    this.sent = body
  }
  finish(payload: unknown) {
    this.status = 201
    this.responseText = JSON.stringify(payload)
    this.onload?.()
  }
}

function jsonResponse(body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { 'Content-Type': 'application/json' },
  })
}

describe('图片消息 UI（第一批）', () => {
  beforeEach(() => {
    useChatStore.getState().reset()
    vi.unstubAllGlobals()
    FakeXHR.instances = []
    vi.stubGlobal('XMLHttpRequest', FakeXHR)
    vi.stubGlobal('URL', {
      ...URL,
      createObjectURL: vi.fn(() => 'blob:preview'),
      revokeObjectURL: vi.fn(),
    })
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input)
        if (url.endsWith('/api/config/workspace')) {
          return jsonResponse({ workspace: 'D:\\MyAi\\workspace', default_workspace: 'D:\\MyAi\\workspace', is_default: true, extras: [] })
        }
        return jsonResponse({})
      }),
    )
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('用户气泡渲染图片附件，占位文字 [图片] 不再重复显示', () => {
    const message: ChatMessage = {
      id: 'u1',
      role: 'user',
      content: '[图片]',
      attachments: [ATTACHMENT],
    }
    render(<MessageBubble message={message} />)
    expect(screen.getByTestId('message-image')).toHaveAttribute('src', ATTACHMENT.url)
    expect(screen.queryByText('[图片]')).not.toBeInTheDocument()
  })

  it('历史 messages 的 attachments 会被还原到 ChatMessage', () => {
    const messages = reconstructMessages([
      { role: 'user', content: '看图', attachments: [ATTACHMENT] },
      { role: 'assistant', content: '看到了' },
    ])
    expect(messages[0].attachments).toEqual([ATTACHMENT])
  })

  it('视觉模型：可上传、显示缩略图、可删除', async () => {
    useChatStore.setState({
      models: [model('vision', true)],
      model: 'vision',
      sessionId: 's1',
    })
    const user = userEvent.setup()
    render(<Composer />)

    const input = screen.getByLabelText('选择要发送的图片') as HTMLInputElement
    await user.upload(input, new File(['x'], 'cat.png', { type: 'image/png' }))

    await waitFor(() => expect(FakeXHR.instances.length).toBeGreaterThan(0))
    FakeXHR.instances.at(-1)!.finish(ATTACHMENT)
    await waitFor(() => expect(screen.getByAltText('cat.png.jpg')).toBeInTheDocument())

    await user.click(screen.getByLabelText('移除图片'))
    expect(screen.queryByAltText('cat.png.jpg')).not.toBeInTheDocument()
  })

  it('纯文本模型：图片按钮禁用并提示切换视觉模型', async () => {
    useChatStore.setState({ models: [model('deepseek', false)], model: 'deepseek', sessionId: 's1' })
    const user = userEvent.setup()
    render(<Composer />)

    const button = screen.getByTestId('image-upload-button')
    expect(button).toHaveAttribute('title', '当前模型不支持图片，请切换到视觉模型')
    await user.click(button)
    expect(screen.getByRole('alert')).toHaveTextContent(/不支持图片/)
  })

  it('发送时把 attachment 引用传给 sendMessage', async () => {
    const sendMessage = vi.fn(async () => undefined)
    useChatStore.setState({
      models: [model('vision', true)],
      model: 'vision',
      sessionId: 's1',
      sendMessage,
    })
    const user = userEvent.setup()
    render(<Composer />)

    await user.upload(
      screen.getByLabelText('选择要发送的图片'),
      new File(['x'], 'cat.png', { type: 'image/png' }),
    )
    await waitFor(() => expect(FakeXHR.instances.length).toBeGreaterThan(0))
    FakeXHR.instances.at(-1)!.finish(ATTACHMENT)
    await waitFor(() => expect(screen.getByAltText('cat.png.jpg')).toBeInTheDocument())

    await user.type(screen.getByRole('textbox'), '看看这张图')
    await user.click(screen.getByLabelText('发送'))

    expect(sendMessage).toHaveBeenCalledWith('看看这张图', [ATTACHMENT])
  })
})
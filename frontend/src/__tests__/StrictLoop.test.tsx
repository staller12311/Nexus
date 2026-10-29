import { StrictMode } from 'react'
import { act, render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import App from '../App'
import { useChatStore } from '../store/chatStore'

function jsonResponse(body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { 'Content-Type': 'application/json' },
  })
}

const WORKSPACE_INFO = {
  workspace: 'D:\\MyAi\\workspace',
  default_workspace: 'D:\\MyAi\\workspace',
  is_default: true,
  extras: [],
}

/** 真实场景：reasoning + 长文本 + 工具调用（含失败）+ 结束 */
function stubStream() {
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if (url.endsWith('/api/chat/stream')) {
        const encoder = new TextEncoder()
        const body = new ReadableStream({
          start(controller) {
            const frames: string[] = []
            for (let index = 0; index < 40; index += 1) {
              frames.push(`data: ${JSON.stringify({ type: 'reasoning_delta', text: `思考第 ${index} 步；` })}\n\n`)
            }
            frames.push(
              `data: ${JSON.stringify({ type: 'text_delta', delta: '好的，我来处理。\n\n' })}\n\n`,
            )
            frames.push(
              `data: ${JSON.stringify({ type: 'tool_call_start', tool_call_id: 'c1', name: 'list_dir', arguments: '{"path":"."}' })}\n\n`,
            )
            frames.push(
              `data: ${JSON.stringify({ type: 'tool_call_end', tool_call_id: 'c1', name: 'list_dir', ok: true, content: 'a/' })}\n\n`,
            )
            frames.push(
              `data: ${JSON.stringify({ type: 'tool_call_start', tool_call_id: 'c2', name: 'write_file', arguments: '{"path":"probe2.txt","content":"probe"}' })}\n\n`,
            )
            frames.push(
              `data: ${JSON.stringify({ type: 'tool_call_end', tool_call_id: 'c2', name: 'write_file', ok: false, content: '错误：当前目录无写入权限' })}\n\n`,
            )
            for (let index = 0; index < 60; index += 1) {
              frames.push(
                `data: ${JSON.stringify({ type: 'text_delta', delta: `第 ${index} 行：一些较长的说明文字，用于把页面撑高。\n` })}\n\n`,
              )
            }
            frames.push('data: {"type":"message_end","content":"完成","usage":{"prompt_tokens":10,"completion_tokens":20,"total_tokens":30}}\n\n')
            for (const frame of frames) controller.enqueue(encoder.encode(frame))
            controller.close()
          },
        })
        return new Response(body, { status: 200, headers: { 'Content-Type': 'text/event-stream' } })
      }
      if (url.endsWith('/api/models'))
        return jsonResponse({ models: [{ name: 'deepseek', model: 'deepseek-flash', base_url: 'x', api_key_env: 'K', key_set: true, is_default: true, is_custom: false, has_key: true, api_key_masked: '' }], default: 'deepseek', base_url: 'x' })
      if (url.endsWith('/api/sessions') && (!init || init.method === undefined))
        return jsonResponse({ sessions: [] })
      if (url.includes('/api/kb/'))
        return jsonResponse({ docs: [], chunks: 0, mode: 'keyword', supported: ['.md'], max_file_mb: 10, pipeline: 'BM25' })
      if (url.includes('/api/fs/dirs'))
        return jsonResponse({ path: '', parent: null, entries: [], drives: ['D:\\'], truncated: false })
      if (url.endsWith('/api/config/workspace')) return jsonResponse(WORKSPACE_INFO)
      return jsonResponse({})
    }),
  )
}

describe('完整流程稳定性（StrictMode + 流式 + 工具 + 工作区弹窗）', () => {
  let consoleError: ReturnType<typeof vi.spyOn>

  beforeEach(() => {
    useChatStore.getState().reset()
    consoleError = vi.spyOn(console, 'error').mockImplementation(() => undefined)
  })
  afterEach(() => {
    consoleError.mockRestore()
    vi.unstubAllGlobals()
  })

  function loopErrors(): string[] {
    return consoleError.mock.calls
      .map((call: unknown[]) => String(call[0] ?? ''))
      .filter((text: string) => text.includes('Maximum update depth'))
  }

  it('发消息 + 流式 100 帧：不出现 Maximum update depth', async () => {
    stubStream()
    render(
      <StrictMode>
        <App />
      </StrictMode>,
    )
    await act(async () => {
      useChatStore.setState({ model: 'deepseek' })
      await useChatStore.getState().sendMessage('写点东西')
    })
    expect(loopErrors()).toEqual([])
  })

  it('打开工作区弹窗并应用（含附加目录）后不出现循环', async () => {
    stubStream()
    const user = userEvent.setup()
    render(
      <StrictMode>
        <App />
      </StrictMode>,
    )

    await user.click(screen.getByTestId('sandbox-badge'))
    expect(screen.getByRole('dialog', { name: '切换工作区' })).toBeInTheDocument()
    await user.click(within(screen.getByRole('dialog', { name: '切换工作区' })).getByRole('button', { name: /添加/ }))
    await user.type(screen.getByLabelText('附加目录 1 挂载名'), 'libs')
    await user.type(screen.getByLabelText('附加目录 1 路径'), 'D:\\SharedLibs')
    await user.click(screen.getByRole('button', { name: '应用' }))

    await act(async () => {
      await Promise.resolve()
    })
    expect(loopErrors()).toEqual([])
  })

  it('打开目录选择器（浏览 D:）后不出现循环', async () => {
    stubStream()
    const user = userEvent.setup()
    render(
      <StrictMode>
        <App />
      </StrictMode>,
    )

    await user.click(screen.getByTestId('sandbox-badge'))
    await user.click(screen.getByLabelText('浏览选择主目录'))
    expect(screen.getByRole('dialog', { name: '选择文件夹' })).toBeInTheDocument()
    await act(async () => {
      await Promise.resolve()
    })
    expect(loopErrors()).toEqual([])
  })
})
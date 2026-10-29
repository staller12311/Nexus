import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'
import App from '../App'

/**
 * 整页渲染（jsdom）：验证空状态、侧边栏、输入区都真的出现在 DOM 里，
 * 并顺带把渲染出来的可见文本打印出来，作为「界面上有什么」的证据。
 */
describe('App 布局', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('空状态：侧边栏 + 引导文案 + 输入框都在，不白屏', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input)
        const body = url.includes('/api/models')
          ? [
              {
                name: 'webmock',
                model: 'web-model',
                base_url: 'http://127.0.0.1:8128/v1',
                api_key_env: 'WEB_API_KEY',
                key_set: true,
                is_default: true,
              },
            ]
          : { sessions: [] }
        return new Response(JSON.stringify(body), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        })
      }),
    )

    const { container } = render(<App />)
    const user = userEvent.setup()

    expect(screen.getByText(/Nexus/)).toBeInTheDocument()
    expect(screen.getByText('你的私有化 AI 调度中枢')).toBeInTheDocument()
    expect(screen.getByText('开始一段新对话')).toBeInTheDocument()
    expect(screen.getByText(/Enter 发送/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /新建会话/ })).toBeInTheDocument()

    // 模型加载后：点击输入框右下角的模型选择器，菜单里应出现 webmock
    await user.click(screen.getByLabelText('选择模型'))
    await waitFor(() => expect(screen.getByRole('option', { name: /webmock/ })).toBeInTheDocument())

    const text = container.textContent ?? ''
    // eslint-disable-next-line no-console
    console.log('[整页可见文本]', text.replace(/\s+/g, ' ').slice(0, 400))
    expect(text).toContain('还没有历史会话')

    // 输入区：空草稿时发送按钮禁用
    const sendButton = screen.getByRole('button', { name: /发送/ })
    expect(sendButton).toBeDisabled()
  })
})
import { render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ErrorBoundary } from '../components/ErrorBoundary'

function Boom(): never {
  throw new Error('组件炸了：Maximum update depth exceeded')
}

describe('页面级错误边界', () => {
  let consoleError: ReturnType<typeof vi.spyOn>

  beforeEach(() => {
    consoleError = vi.spyOn(console, 'error').mockImplementation(() => undefined)
  })
  afterEach(() => {
    consoleError.mockRestore()
  })

  it('渲染异常时显示可恢复提示，并把组件栈打到 console', () => {
    render(
      <ErrorBoundary>
        <Boom />
      </ErrorBoundary>,
    )

    expect(screen.getByText('页面渲染出错了')).toBeInTheDocument()
    expect(screen.getByText(/组件炸了/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '重新加载页面' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '尝试继续' })).toBeInTheDocument()

    const logged = consoleError.mock.calls
      .flat()
      .map((part: unknown) => String(part ?? ''))
      .join('\n')
    expect(logged).toContain('[Nexus] 页面渲染异常')
    expect(logged).toContain('组件栈')
  })
})
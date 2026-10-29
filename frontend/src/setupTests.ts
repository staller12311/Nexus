import '@testing-library/jest-dom/vitest'
import { cleanup } from '@testing-library/react'
import { afterEach } from 'vitest'

// vitest 的 globals 关着，@testing-library/react 不会自动注册清理钩子，
// 这里显式注册，避免上一个用例的 DOM 残留到下一个用例。
afterEach(() => {
  cleanup()
})
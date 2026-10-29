import { defineConfig } from 'vitest/config'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  test: {
    environment: 'jsdom',
    globals: false,
    // 默认的 forks 池会 spawn 子进程，本机沙箱禁止（EPERM）；threads 池用 worker_threads，不 spawn。
    pool: 'threads',
    // 集成测试要等真实后端退避重试（5xx 场景 ~5s），默认 5s 会误杀
    testTimeout: 20000,
    hookTimeout: 20000,
    setupFiles: ['./src/setupTests.ts'],
    // 只收 src 下的测试；仓库根 tests/ 是 pytest 的地盘，绝不混。
    include: ['src/**/*.{test,spec}.{ts,tsx}'],
  },
})
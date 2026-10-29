import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// 前端代码里只写 /api/xxx，由这个代理转发到后端，避免把后端地址写死在业务代码里。
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    strictPort: true,
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: true,
      },
    },
  },
})
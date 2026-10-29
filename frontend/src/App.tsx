import { useEffect } from 'react'
import { api } from './api/client'
import { ChatArea } from './components/ChatArea'
import { Composer } from './components/Composer'
import { ErrorBanner } from './components/ErrorBanner'
import { NoticeBanner } from './components/NoticeBanner'
import { Sidebar } from './components/Sidebar'
import { useChatStore } from './store/chatStore'

export default function App() {
  const loadModels = useChatStore((state) => state.loadModels)
  const setSessions = useChatStore((state) => state.setSessions)
  const setError = useChatStore((state) => state.setError)

  useEffect(() => {
    let cancelled = false

    const bootstrap = async () => {
      try {
        await loadModels()
        const sessions = await api.sessions()
        if (cancelled) return
        setSessions(sessions.sessions)
      } catch (error) {
        if (cancelled) return
        setError(
          error instanceof Error
            ? `无法连接后端：${error.message}`
            : '无法连接后端，请确认 uvicorn 已启动',
        )
      }
    }

    void bootstrap()
    return () => {
      cancelled = true
    }
  }, [loadModels, setError, setSessions])

  return (
    <div className="flex h-full min-h-0 bg-slate-50 text-slate-800">
      <Sidebar />
      <main className="flex min-h-0 min-w-0 flex-1 flex-col">
        <ErrorBanner />
        <NoticeBanner />
        <ChatArea />
        <Composer />
      </main>
    </div>
  )
}
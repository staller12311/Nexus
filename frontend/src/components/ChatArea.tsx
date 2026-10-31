import { useCallback, useEffect, useRef, useState } from 'react'
import { ArrowDown } from 'lucide-react'
import { useChatStore } from '../store/chatStore'
import { MessageBubble } from './MessageBubble'
import { SummaryNoticeCard } from './SummaryNotice'
import { UploadCards } from './UploadCards'

const EXAMPLES = [
  '读一下 readme.txt 的内容',
  '列一下 workspace 里有什么',
  '现在几点了？',
]

/** 距底部超过这个距离（px）就算「用户正在往上看」，不再自动滚动。 */
const SCROLL_THRESHOLD = 100

function EmptyState() {
  const setDraft = useChatStore((state) => state.setDraft)
  return (
    <div className="flex flex-1 flex-col items-center justify-center px-6 text-center">
      <div className="mb-2 text-2xl font-semibold text-slate-700">开始一段新对话</div>
      <p className="mb-6 max-w-md text-sm text-slate-500">
        在左侧选一个模型，在下面输入问题。<span className="font-mono">Enter</span> 发送，
        <span className="font-mono">Shift+Enter</span> 换行。
      </p>
      <div className="flex flex-wrap justify-center gap-2">
        {EXAMPLES.map((example) => (
          <button
            key={example}
            type="button"
            onClick={() => setDraft(example)}
            className="rounded-full border border-slate-200 bg-white px-3 py-1.5 text-xs text-slate-600 hover:border-sky-300 hover:text-sky-700"
          >
            {example}
          </button>
        ))}
      </div>
    </div>
  )
}

/** 距底部还有多少像素（容器不可滚动时返回 0）。 */
function distanceToBottom(element: HTMLElement): number {
  return element.scrollHeight - element.scrollTop - element.clientHeight
}

export function ChatArea() {
  const messages = useChatStore((state) => state.messages)
  const kbUploads = useChatStore((state) => state.kbUploads)
  const summaryNotices = useChatStore((state) => state.summaryNotices)
  const bottomRef = useRef<HTMLDivElement | null>(null)
  const scrollRef = useRef<HTMLDivElement | null>(null)
  // 「用户正在往上看」：true 时流式新内容绝不把页面拽回底部（README 第 56 条）
  const [isUserScrolledUp, setIsUserScrolledUp] = useState(false)
  // 用 ref 镜像一份，避免 effect 里读到旧闭包值
  const scrolledUpRef = useRef(false)
  // 程序化滚动标记：忽略自己触发的 scroll 事件（防滚动反馈环）
  const programmaticScrollRef = useRef(false)
  // 用来判断「是否多了用户消息」：sendMessage 会同时插入 user + assistant 占位，
  // 所以不能用「最后一条是 user」来判断。
  const userCountRef = useRef(0)

  const updateScrolledUp = useCallback((next: boolean) => {
    scrolledUpRef.current = next
    // 只在真正变化时才 setState：避免「程序化滚动 → scroll 事件 → setState → 再渲染」的自我激励
    setIsUserScrolledUp((previous) => (previous === next ? previous : next))
  }, [])

  const scrollToBottom = useCallback((behavior: ScrollBehavior = 'auto') => {
    const element = scrollRef.current
    if (!element) return
    // 我们自己发起的滚动会触发 scroll 事件，用标记忽略它，避免把用户状态翻成「在往上看」
    programmaticScrollRef.current = true
    // jsdom 没有 scrollTo，用可选调用兜底
    element.scrollTo?.({ top: element.scrollHeight, behavior })
    if (!element.scrollTo) element.scrollTop = element.scrollHeight
    if (typeof window.requestAnimationFrame === 'function') {
      window.requestAnimationFrame(() => {
        programmaticScrollRef.current = false
      })
    } else {
      programmaticScrollRef.current = false
    }
    updateScrolledUp(false)
  }, [updateScrolledUp])

  const onScroll = () => {
    const element = scrollRef.current
    if (!element) return
    const distance = distanceToBottom(element)
    // 程序化滚动的「回声」事件：我们每次都滚到底部，所以只有离底部很近才可能是自己造成的；
    // 离底部很远说明用户真的往上翻了 —— 这种情况必须尊重用户，不能被标记吞掉。
    if (programmaticScrollRef.current && distance <= SCROLL_THRESHOLD) return
    programmaticScrollRef.current = false
    // 只根据「是否离底部超过阈值」判定，回到底部附近就恢复自动跟随
    updateScrolledUp(distance > SCROLL_THRESHOLD)
  }

  useEffect(() => {
    // 用户自己发了新消息（用户消息条数变多）→ 无论如何滚到底部并恢复跟随
    const userCount = messages.reduce((total, message) => total + (message.role === 'user' ? 1 : 0), 0)
    const newUserMessage = userCount > userCountRef.current
    userCountRef.current = userCount

    if (newUserMessage) {
      scrollToBottom()
      return
    }
    // 流式增量：只有用户没在往上看时才跟随，否则让页面停在他看的位置
    if (scrolledUpRef.current) return
    scrollToBottom()
  }, [messages, kbUploads, scrollToBottom])

  if (messages.length === 0 && kbUploads.length === 0) {
    return (
      <div className="flex min-h-0 flex-1 flex-col">
        <EmptyState />
      </div>
    )
  }

  return (
    <div className="relative min-h-0 flex-1">
      <div
        ref={scrollRef}
        onScroll={onScroll}
        data-testid="chat-scroll"
        className="h-full min-h-0 overflow-y-auto"
      >
        <div className="mx-auto flex w-full max-w-3xl flex-col gap-3 px-4 py-6">
          {messages.map((message) => (
            <MessageBubble key={message.id} message={message} />
          ))}
          {summaryNotices.map((notice) => (
            <SummaryNoticeCard key={notice.id} notice={notice} />
          ))}
          <UploadCards />
          <div ref={bottomRef} />
        </div>
      </div>

      {/* 悬浮「回到底部」：只在用户往上翻时出现 */}
      {isUserScrolledUp && (
        <button
          type="button"
          onClick={() => scrollToBottom('smooth')}
          aria-label="回到底部"
          className="absolute bottom-4 right-6 z-10 flex items-center gap-1 rounded-full border border-slate-200 bg-white px-3 py-1.5 text-xs text-slate-600 shadow-lg transition hover:border-sky-300 hover:text-sky-700"
        >
          <ArrowDown className="h-3.5 w-3.5" />
          回到底部
        </button>
      )}
    </div>
  )
}
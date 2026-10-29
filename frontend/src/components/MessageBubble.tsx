import { memo, useCallback, useEffect, useMemo, useRef, useState } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { Check, Copy, Pencil, RefreshCw } from 'lucide-react'
import type { ChatMessage } from '../types'
import { useChatStore } from '../store/chatStore'
import { CodeBlock } from './CodeBlock'
import { ReasoningPanel } from './ReasoningPanel'
import { ToolCard } from './ToolCard'

/** 把 ```lang\n...\n``` 交给 CodeBlock；行内 `code` 保持行内样式。
 *
 * 用工厂函数按 `streaming` 生成映射表，再由 useMemo 缓存 —— 流式期间不挂高亮器，
 * 也不因为每次 delta 都重建 components 对象而让 react-markdown 重挂全部代码块。
 */
function makeMarkdownComponents(streaming: boolean) {
  return {
    // 代码块自己会渲染 <pre>，这里把 markdown 的 <pre> 拍平，避免嵌套
    pre: ({ children }: { children?: React.ReactNode }) => <>{children}</>,
    // 链接一律在新标签页打开：搜索结果/引用链接点一下就覆盖掉当前对话太糟糕。
    // rel=noopener noreferrer 同时挡住新页面对 window.opener 的反向操控。
    a: ({ href, children }: { href?: string; children?: React.ReactNode }) => (
      <a
        href={href}
        target="_blank"
        rel="noopener noreferrer"
        className="break-all text-sky-600 underline decoration-sky-300 underline-offset-2 hover:text-sky-700"
      >
        {children}
      </a>
    ),
    code: ({ className, children }: { className?: string; children?: React.ReactNode }) => {
      const text = String(children ?? '')
      const match = /language-([\w+-]+)/.exec(className ?? '')
      const isBlock = Boolean(match) || text.includes('\n')

      if (!isBlock) {
        return (
          <code className="rounded bg-slate-100 px-1 py-0.5 font-mono text-[0.9em] text-slate-800">
            {text}
          </code>
        )
      }
      return (
        <CodeBlock
          language={match?.[1] ?? 'text'}
          value={text.replace(/\n$/, '')}
          streaming={streaming}
        />
      )
    },
  }
}

/** 复制到剪贴板：优先用 async clipboard，失败返回 false（jsdom 里需要 stub）。 */
async function copyText(text: string): Promise<boolean> {
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(text)
      return true
    }
  } catch {
    return false
  }
  return false
}

/**
 * 一条消息的渲染规则（README「历史消息怎么渲染」）：
 * - content 有内容 → 文字气泡
 * - 有 toolCalls   → 工具卡片
 * - 有 reasoning   → 思维链面板（思考中正文还没来，也必须渲染）
 * - 三者都没有     → 整条不渲染（避免 content=null 的空气泡）
 *
 * 悬停出现操作按钮：用户消息「复制 / 编辑」，AI 消息「复制」（只复制正文，不含思考）。
 */
export function MessageBubbleInner({ message }: { message: ChatMessage }) {
  const hasText = Boolean(message.content && message.content.trim().length > 0)
  const hasTools = Boolean(message.toolCalls && message.toolCalls.length > 0)
  const hasReasoning = Boolean(message.reasoning)
  const [copied, setCopied] = useState(false)
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null)
  const editMessage = useChatStore((state) => state.editMessage)
  const regenerate = useChatStore((state) => state.regenerate)
  const streaming = useChatStore((state) => state.streaming)
  // 只有「这条消息自己还在流式」时才关高亮；历史消息一直是高亮状态
  const isStreamingMessage = Boolean(message.streaming)
  const markdownComponents = useMemo(
    () => makeMarkdownComponents(isStreamingMessage),
    [isStreamingMessage],
  )

  useEffect(
    () => () => {
      if (timer.current) clearTimeout(timer.current)
    },
    [],
  )

  const onCopy = useCallback(async () => {
    // AI 消息只复制正文，**不**包含思考过程
    const ok = await copyText(message.content ?? '')
    if (!ok) return
    setCopied(true)
    if (timer.current) clearTimeout(timer.current)
    timer.current = setTimeout(() => setCopied(false), 1500)
  }, [message.content])

  if (!hasText && !hasTools && !hasReasoning) return null

  const isUser = message.role === 'user'

  return (
    <div
      className={`group flex w-full ${isUser ? 'justify-end' : 'justify-start'}`}
      data-testid={`message-${message.role}`}
    >
      <div className={`max-w-[75%] min-w-0 ${isUser ? 'items-end' : 'items-start'}`}>
        {/* 思维链面板：有 reasoning 才渲染，放在正文气泡上方 */}
        {!isUser && message.reasoning && (
          <ReasoningPanel reasoning={message.reasoning} streaming={message.reasoningStreaming} />
        )}

        {hasText && (
          <div
            className={`rounded-2xl px-4 py-2 text-sm shadow-sm ${
              isUser
                ? 'rounded-br-sm bg-sky-600 text-white'
                : 'rounded-bl-sm bg-white text-slate-800 ring-1 ring-slate-200'
            }`}
          >
            {isUser ? (
              <span className="whitespace-pre-wrap break-words">{message.content}</span>
            ) : (
              <div className="markdown min-w-0 break-words">
                <ReactMarkdown remarkPlugins={[remarkGfm]} components={markdownComponents}>
                  {message.content ?? ''}
                </ReactMarkdown>
              </div>
            )}
          </div>
        )}

        {message.toolCalls?.map((call) => (
          <ToolCard key={call.id} call={call} />
        ))}

        {/* 单轮用量：常驻显示的极小灰字（不藏在 hover 操作区里），含缓存命中率 */}
        {!isUser && message.usage && (
          <div
            data-testid="usage-line"
            className="mt-1 flex flex-wrap items-center gap-x-1 text-[10px] leading-4 text-slate-400"
          >
            <span>
              prompt {message.usage.prompt_tokens}
              {message.usage.prompt_tokens > 0 && (
                <>
                  {' '}
                  (命中 {Math.round((message.usage.cache_hit_tokens / message.usage.prompt_tokens) * 100)}%)
                </>
              )}{' '}
              / completion {message.usage.completion_tokens} / total {message.usage.total_tokens}
            </span>
          </div>
        )}

        {/* 悬停操作区：默认透明，hover/focus 时出现 */}
        <div
          className={`mt-1 flex h-5 items-center gap-1 opacity-0 transition-opacity group-hover:opacity-100 focus-within:opacity-100 ${
            isUser ? 'justify-end' : 'justify-start'
          }`}
        >
          <button
            type="button"
            onClick={() => void onCopy()}
            aria-label={copied ? '已复制' : '复制'}
            title={copied ? '已复制' : '复制'}
            className="flex items-center gap-1 rounded px-1 py-0.5 text-[11px] text-slate-400 hover:bg-slate-100 hover:text-slate-600"
          >
            {copied ? (
              <Check className="h-3.5 w-3.5 text-emerald-600" />
            ) : (
              <Copy className="h-3.5 w-3.5" />
            )}
            {copied && <span className="text-emerald-600">已复制</span>}
          </button>

          {isUser && (
            <button
              type="button"
              onClick={() => editMessage(message.id)}
              aria-label="编辑"
              title="编辑：把这条消息填回输入框"
              className="flex items-center gap-1 rounded px-1 py-0.5 text-[11px] text-slate-400 hover:bg-slate-100 hover:text-slate-600"
            >
              <Pencil className="h-3.5 w-3.5" />
            </button>
          )}

          {/* 重新生成：只对 AI 回复开放；流式期间禁用，避免并发两条流 */}
          {!isUser && (
            <button
              type="button"
              onClick={() => void regenerate(message.id)}
              disabled={streaming || message.streaming}
              aria-label="重新生成"
              title="重新生成这条回答（覆盖旧回答）"
              className="flex items-center gap-1 rounded px-1 py-0.5 text-[11px] text-slate-400 hover:bg-slate-100 hover:text-slate-600 disabled:cursor-not-allowed disabled:opacity-40"
            >
              <RefreshCw className="h-3.5 w-3.5" />
            </button>
          )}

          {/* 被用户「停止」打断的回复：明确标注，避免误以为是完整答案 */}
          {message.interrupted && (
            <span className="rounded bg-amber-50 px-1 py-0.5 text-[10px] text-amber-600">
              已中断
            </span>
          )}
        </div>
      </div>
    </div>
  )
}

/**
 * memo：流式期间只有「正在更新的最后一条」会重渲染，历史消息直接跳过。
 * 依赖对象是 message（不可变更新），所以默认浅比较就够用。
 */
export const MessageBubble = memo(MessageBubbleInner)

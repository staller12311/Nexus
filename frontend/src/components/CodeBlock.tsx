import { memo, useMemo, useState } from 'react'
import { Check, Copy } from 'lucide-react'
import { PrismLight as SyntaxHighlighter } from 'react-syntax-highlighter'
import bash from 'react-syntax-highlighter/dist/esm/languages/prism/bash'
import diff from 'react-syntax-highlighter/dist/esm/languages/prism/diff'
import javascript from 'react-syntax-highlighter/dist/esm/languages/prism/javascript'
import json from 'react-syntax-highlighter/dist/esm/languages/prism/json'
import markdown from 'react-syntax-highlighter/dist/esm/languages/prism/markdown'
import python from 'react-syntax-highlighter/dist/esm/languages/prism/python'
import sql from 'react-syntax-highlighter/dist/esm/languages/prism/sql'
import tsx from 'react-syntax-highlighter/dist/esm/languages/prism/tsx'
import typescript from 'react-syntax-highlighter/dist/esm/languages/prism/typescript'
import yaml from 'react-syntax-highlighter/dist/esm/languages/prism/yaml'
import { oneDark } from 'react-syntax-highlighter/dist/esm/styles/prism'

// 只注册常用的几种语言（PrismLight 不会把整包 prismjs 都打进来）；
// 未注册的语言按纯文本显示，不会报错。
const LANGUAGES: Record<string, unknown> = {
  bash,
  sh: bash,
  shell: bash,
  python,
  py: python,
  javascript,
  js: javascript,
  typescript,
  ts: typescript,
  tsx,
  jsx: tsx,
  json,
  yaml,
  yml: yaml,
  sql,
  markdown,
  md: markdown,
  diff,
}

for (const [name, definition] of Object.entries(LANGUAGES)) {
  SyntaxHighlighter.registerLanguage(name, definition)
}

async function copyText(text: string): Promise<boolean> {
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(text)
      return true
    }
  } catch {
    /* 权限被拒等情况，退回下面的老办法 */
  }
  try {
    const area = document.createElement('textarea')
    area.value = text
    area.style.position = 'fixed'
    area.style.opacity = '0'
    document.body.appendChild(area)
    area.select()
    const ok = document.execCommand('copy')
    document.body.removeChild(area)
    return ok
  } catch {
    return false
  }
}

export type CodeBlockProps = {
  /** 语言标识（```python 里的 python）；拿不到就传 text */
  language: string
  value: string
  /** 危险操作（shell / Python 执行）使用鲜红色边框与标题 */
  danger?: boolean
  /**
   * 这条消息还在流式输出中。
   *
   * 流式期间**绝不做语法高亮**：高亮器每次都把「越来越长的整段代码」重新分词 + 生成
   * 成百上千个 span，100 行左右就能把主线程卡死（实测浏览器「页面没有响应」）。
   * 流式期间只渲染纯 <pre><code>，等 message_end 后再一次性挂载高亮（README 第 54 条）。
   */
  streaming?: boolean
}

/**
 * 代码块：深色背景 + 行号 + 语法高亮 + 横向滚动 + 顶部「复制代码」。
 * 横向滚动放在外层容器上，保证长行不会把聊天气泡撑爆。
 */
function CodeBlockInner({ language, value, streaming = false, danger = false }: CodeBlockProps) {
  const [copied, setCopied] = useState(false)
  const normalized = useMemo(() => language.toLowerCase(), [language])
  const registered = normalized in LANGUAGES

  const onCopy = async () => {
    const ok = await copyText(value)
    setCopied(ok)
    window.setTimeout(() => setCopied(false), 2000)
  }

  return (
    <div
      data-danger={danger ? 'true' : undefined}
      className={`my-2 min-w-0 overflow-hidden rounded-lg border shadow-sm ${
        danger ? 'border-2 border-red-500 bg-[#1f0b0d]' : 'border-slate-700 bg-[#0f172a]'
      }`}
    >
      <div
        className={`flex items-center justify-between gap-2 border-b px-3 py-1.5 ${
          danger ? 'border-red-500 bg-red-950/60' : 'border-slate-700 bg-[#111c33]'
        }`}
      >
        <span className="font-mono text-[11px] uppercase tracking-wide text-slate-400">
          {registered ? normalized : 'text'}
          {streaming && <span className="ml-1 normal-case text-slate-500">· 输出中</span>}
        </span>
        {!streaming && (
          <button
            type="button"
            onClick={() => void onCopy()}
            className="flex items-center gap-1 rounded px-1.5 py-0.5 text-[11px] text-slate-300 transition hover:bg-slate-700/60 hover:text-white"
            aria-label="复制代码"
          >
            {copied ? <Check className="h-3.5 w-3.5" /> : <Copy className="h-3.5 w-3.5" />}
            {copied ? '已复制' : '复制代码'}
          </button>
        )}
      </div>

      <div className="max-w-full overflow-x-auto" data-testid="code-scroll">
        {streaming ? (
          // 流式：纯文本，零解析零高亮
          <pre
            data-testid="code-plain"
            className="m-0 overflow-x-auto p-3 font-mono text-[12px] leading-[1.55] text-slate-200"
          >
            <code>{value}</code>
          </pre>
        ) : (
          <SyntaxHighlighter
            language={registered ? normalized : 'text'}
            style={oneDark}
            showLineNumbers
            wrapLongLines={false}
            lineNumberStyle={{ minWidth: '2.25em', color: '#475569', userSelect: 'none' }}
            customStyle={{
              margin: 0,
              padding: '0.75rem',
              background: 'transparent',
              fontSize: '12px',
              lineHeight: 1.55,
            }}
            codeTagProps={{ style: { fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace' } }}
          >
            {value}
          </SyntaxHighlighter>
        )}
      </div>
    </div>
  )
}

/** memo：已完成的代码块在后续流式更新中完全跳过重渲染。 */
export const CodeBlock = memo(CodeBlockInner)
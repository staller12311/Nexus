import { AlertTriangle, ShieldAlert } from 'lucide-react'
import { CodeBlock } from './CodeBlock'
import { useChatStore } from '../store/chatStore'
import type { ToolCallView } from '../types'

/** 代码预览上限：超过就折叠，同意后仍会完整写入/执行 */
const PREVIEW_LINE_LIMIT = 200

function parseArguments(raw: string): Record<string, unknown> | null {
  try {
    const parsed: unknown = JSON.parse(raw)
    return parsed && typeof parsed === 'object' && !Array.isArray(parsed)
      ? (parsed as Record<string, unknown>)
      : null
  } catch {
    return null
  }
}

function asText(value: unknown): string {
  return typeof value === 'string' ? value : ''
}

const EXT_LANGUAGE: Record<string, string> = {
  py: 'python',
  ts: 'typescript',
  tsx: 'tsx',
  js: 'javascript',
  jsx: 'tsx',
  json: 'json',
  md: 'markdown',
  yml: 'yaml',
  yaml: 'yaml',
  sh: 'bash',
  sql: 'sql',
}

function languageFor(path: string): string {
  const ext = path.split('.').pop()?.toLowerCase() ?? ''
  return EXT_LANGUAGE[ext] ?? 'text'
}

const TITLES: Record<string, string> = {
  write_file: 'AI 想写入文件',
  create_dir: 'AI 想创建目录',
  delete_path: 'AI 想删除文件或空目录',
  move_path: 'AI 想移动 / 重命名',
  run_python_code: 'AI 想执行 Python 代码',
  run_shell: 'AI 想执行 Shell 命令',
}

function Subtitle({ text }: { text: string }) {
  return <div className="mt-0.5 text-[11px] text-amber-700">{text}</div>
}

/** 危险工具的富文本审批卡片：黄色、代码逐行预览、同意/拒绝。 */
export function ApprovalCard({ call }: { call: ToolCallView }) {
  const approveToolCall = useChatStore((state) => state.approveToolCall)
  const approval = call.approval
  if (!approval || approval.status !== 'pending') return null

  const args = parseArguments(call.arguments) ?? {}
  const path = asText(args.path) || asText(args.src)
  const dest = asText(args.dest)
  const content = asText(args.content) || asText(args.code)
  const command = asText(args.command)
  const timeout = typeof args.timeout === 'number' ? args.timeout : undefined
  const isExecutable = call.name === 'run_shell' || call.name === 'run_python_code'
  const executableContent = call.name === 'run_shell' ? command : content
  const executableLanguage = call.name === 'run_shell' ? 'bash' : 'python'

  const title = TITLES[call.name] ?? `AI 想调用 ${call.name}`
  const lines = content ? content.split('\n') : []
  const truncated = lines.length > PREVIEW_LINE_LIMIT
  const preview = truncated ? lines.slice(0, PREVIEW_LINE_LIMIT).join('\n') : content

  const subText =
    call.name === 'write_file'
      ? '同意后会立即写入工作区；拒绝则把「用户已拒绝」告诉模型。'
      : call.name === 'delete_path'
        ? '删除后不可恢复；同意前请确认路径。'
        : call.name === 'move_path'
          ? '移动 / 重命名后原路径将不存在。'
          : call.name === 'run_python_code'
            ? `同意后会在工作区内以临时 .py 执行，最长 ${timeout ?? 10}s。这是进程级轻量沙箱，不是 OS 隔离。`
            : call.name === 'run_shell'
              ? '同意后会把完整命令交给系统 Shell 执行（可能是 shell=True，不受 workspace 文件工具边界保护）。'
              : '同意后立即执行；拒绝则把结果告诉模型。'

  return (
    <div
      data-testid={`approval-card-${call.id}`}
      className="my-2 overflow-hidden rounded-xl border border-amber-300 bg-amber-50 shadow-sm"
      role="group"
      aria-label={`需要你确认的操作：${call.name}`}
    >
      <div className="flex items-start gap-2 px-4 pt-3">
        <ShieldAlert className="mt-0.5 h-4 w-4 shrink-0 text-amber-600" />
        <div className="min-w-0 flex-1">
          <div className="text-sm font-semibold text-amber-900">{title}</div>
          <Subtitle text={subText} />
        </div>
        <span className="shrink-0 rounded-full border border-amber-300 bg-white/60 px-2 py-0.5 text-[10px] font-medium text-amber-800">
          需要审批
        </span>
      </div>

      <div className="px-4 pb-3 pt-2">
        {path && (
          <div className="mb-2 flex flex-wrap items-center gap-1.5 text-[11px] text-amber-900">
            <span className="text-amber-700">路径</span>
            <code className="rounded bg-white/70 px-1.5 py-0.5 font-mono text-[11px]">{path}</code>
            {dest && (
              <>
                <span className="text-amber-700">→</span>
                <code className="rounded bg-white/70 px-1.5 py-0.5 font-mono text-[11px]">
                  {dest}
                </code>
              </>
            )}
          </div>
        )}

        {isExecutable ? (
          <div className="rounded-xl border-2 border-red-400 bg-red-50/80 p-2" role="alert">
            <div
              data-testid="danger-warning"
              className="mb-2 rounded-md border border-red-500 bg-red-600 px-3 py-2 text-xs font-semibold leading-5 text-white"
            >
              🚨 警告：这是一段可执行代码，请在确认安全后点击同意。
            </div>
            <div data-testid="danger-code">
              <CodeBlock language={executableLanguage} value={executableContent} danger />
            </div>
            {call.name === 'run_python_code' && (
              <div className="px-1 text-[10px] font-medium text-red-700">
                进程级轻量沙箱，非 OS 隔离；代码以当前后端进程权限运行。
              </div>
            )}
            {call.name === 'run_shell' && (
              <div className="px-1 text-[10px] font-medium text-red-700">
                审批只是人工确认，不构成安全隔离；请逐字核对命令。
              </div>
            )}
          </div>
        ) : content ? (
          <>
            <div className="mb-1 flex items-center gap-2 text-[10px] text-amber-700">
              <span>即将写入的完整内容（{lines.length} 行，只读预览）</span>
            </div>
            {truncated ? (
              <div className="mb-1 rounded bg-white/60 px-2 py-1 text-[10px] text-amber-800">
                仅预览前 {PREVIEW_LINE_LIMIT} 行；还有 {lines.length - PREVIEW_LINE_LIMIT} 行，
                同意后会完整写入。
              </div>
            ) : null}
            <CodeBlock language={languageFor(path) === 'text' ? 'python' : languageFor(path)} value={preview} />
          </>
        ) : call.name === 'delete_path' || call.name === 'move_path' ? (
          <div className="rounded-lg border border-amber-200 bg-white/60 px-3 py-2 text-[11px] text-amber-900">
            <AlertTriangle className="mr-1 inline h-3.5 w-3.5 text-amber-600" />
            这个操作不可撤销，请确认路径无误。
          </div>
        ) : (
          <pre className="max-h-48 overflow-auto whitespace-pre-wrap break-all rounded-lg bg-white/60 p-2 font-mono text-[11px] text-amber-900">
            {call.arguments}
          </pre>
        )}
      </div>

      <div className="flex items-center justify-between gap-2 border-t border-amber-200 px-4 py-2.5">
        <span className="flex items-center gap-1 text-[10px] text-amber-700">
          <AlertTriangle className="h-3 w-3" />
          {approval.expiresIn ? `${Math.round(approval.expiresIn)}s 内有效，超时自动拒绝` : '等待你的决定'}
        </span>
        <div className="flex items-center gap-2">
          <button
            type="button"
            onClick={() => void approveToolCall(approval.id, false)}
            className="rounded-lg border border-amber-300 bg-white px-3 py-1.5 text-xs font-medium text-amber-800 transition-colors hover:bg-amber-100"
          >
            拒绝
          </button>
          <button
            type="button"
            onClick={() => void approveToolCall(approval.id, true)}
            className="rounded-lg bg-gray-900 px-3 py-1.5 text-xs font-medium text-white transition-colors hover:bg-gray-800"
          >
            同意执行
          </button>
        </div>
      </div>
    </div>
  )
}
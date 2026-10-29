import { useEffect, useRef, useState } from 'react'
import { FolderOpen, Plus, RotateCcw, ShieldCheck, X } from 'lucide-react'
import { FolderPickerDialog } from './FolderPickerDialog'
import { useChatStore } from '../store/chatStore'
import type { WorkspaceExtra } from '../types'

/** 弹窗内编辑中的一行挂载（name 是挂载名，对应 @name/） */
type ExtraRow = WorkspaceExtra

const EMPTY_ROW: ExtraRow = { name: '', path: '', writable: false }

/**
 * 工作区切换弹窗：主目录 + 若干「附加目录（@name 命名挂载）」。
 * 校验全部在后端（存在性、绝对路径、系统目录黑名单、只读挂载写入），
 * 这里只负责收集输入与展示错误。
 */
export function WorkspaceDialog({ open, onClose }: { open: boolean; onClose: () => void }) {
  const workspace = useChatStore((state) => state.workspace)
  const extras = useChatStore((state) => state.workspaceExtras)
  const setWorkspace = useChatStore((state) => state.setWorkspace)
  const resetWorkspace = useChatStore((state) => state.resetWorkspace)
  const loadWorkspace = useChatStore((state) => state.loadWorkspace)

  const [value, setValue] = useState('')
  const [rows, setRows] = useState<ExtraRow[]>([])
  const [error, setError] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)
  // 目录选择器：'main' = 主目录，数字 = 第 N 个附加目录
  const [pickerTarget, setPickerTarget] = useState<'main' | number | null>(null)
  // 每次「打开」只初始化一次：否则 workspace 变化会再次触发 effect → 又发一次 GET，
  // 那个 GET 回来时会把你刚切换的值覆盖掉（竞态）。
  const initialized = useRef(false)

  // 用字符串做依赖：数组每次 store 更新都是新引用，容易把 effect 反复点燃
  const extrasKey = extras.map((extra) => `${extra.name}=${extra.path}=${extra.writable}`).join('|')

  useEffect(() => {
    if (!open) {
      initialized.current = false
      return
    }
    if (initialized.current) return
    initialized.current = true
    setValue(workspace ?? '')
    setRows(extrasKey ? extras.map((extra) => ({ ...extra })) : [])
    setError(null)
    // 首次打开时如果还没拿到工作区，补拉一次（Composer 挂载时通常已经拉过）
    if (!workspace) void loadWorkspace()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, workspace, extrasKey, loadWorkspace])

  // 首次打开时 workspace 可能还没拉到：等它到了再填进输入框（仅在用户没输入时）
  useEffect(() => {
    if (!open || !workspace) return
    setValue((current) => (current ? current : workspace))
  }, [open, workspace])

  if (!open) return null

  const updateRow = (index: number, patch: Partial<ExtraRow>) => {
    setRows((current) => current.map((row, i) => (i === index ? { ...row, ...patch } : row)))
    setError(null)
  }

  const addRow = () => setRows((current) => [...current, { ...EMPTY_ROW }])
  const removeRow = (index: number) => setRows((current) => current.filter((_, i) => i !== index))

  const validateLocal = (): string | null => {
    if (!value.trim()) return '请填写主工作区绝对路径'
    for (const [index, row] of rows.entries()) {
      if (!row.path.trim()) return `第 ${index + 1} 个附加目录还没填路径`
      if (!row.name.trim()) return `第 ${index + 1} 个附加目录还没填挂载名（例如 libs）`
      if (!/^[a-z0-9][a-z0-9_-]{0,15}$/.test(row.name.trim().replace(/^@/, ''))) {
        return `挂载名「${row.name}」不合法：小写字母/数字开头，可含 - 和 _，最长 16 个字符`
      }
    }
    return null
  }

  const submit = async () => {
    const localError = validateLocal()
    if (localError) {
      setError(localError)
      return
    }
    setSaving(true)
    setError(null)
    try {
      await setWorkspace(
        value.trim(),
        rows.map((row) => ({
          name: row.name.trim().replace(/^@/, ''),
          path: row.path.trim(),
          writable: row.writable,
        })),
      )
      onClose()
    } catch (err) {
      setError(err instanceof Error ? err.message : '切换失败')
    } finally {
      setSaving(false)
    }
  }

  const onReset = async () => {
    setSaving(true)
    setError(null)
    try {
      await resetWorkspace()
      onClose()
    } catch (err) {
      setError(err instanceof Error ? err.message : '恢复默认失败')
    } finally {
      setSaving(false)
    }
  }

  const looksAbsolute = /^[A-Za-z]:[\\/]/.test(value.trim())

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-slate-900/30 p-4">
      <div
        role="dialog"
        aria-label="切换工作区"
        className="max-h-[85vh] w-full max-w-lg overflow-y-auto rounded-xl bg-white shadow-xl"
      >
        <div className="flex items-center gap-2 border-b border-slate-200 px-4 py-3">
          <ShieldCheck className="h-4 w-4 text-emerald-600" />
          <span className="text-sm font-semibold text-slate-800">工作区与附加目录</span>
          <button
            type="button"
            onClick={onClose}
            aria-label="关闭"
            className="ml-auto rounded p-1 text-slate-400 hover:bg-slate-100"
          >
            <X className="h-4 w-4" />
          </button>
        </div>

        <div className="px-4 py-3">
          <label htmlFor="workspace-input" className="mb-1 block text-[11px] font-medium text-slate-500">
            主目录（可读写）
          </label>
          <div className="flex items-center gap-2">
            <button
              type="button"
              onClick={() => setPickerTarget('main')}
              aria-label="浏览选择主目录"
              title="点击浏览文件夹"
              className="shrink-0 rounded p-0.5 text-slate-400 hover:bg-slate-100 hover:text-sky-600"
            >
              <FolderOpen className="h-4 w-4" />
            </button>
            <input
              id="workspace-input"
              name="workspace"
              value={value}
              onChange={(event) => {
                setValue(event.target.value)
                setError(null)
              }}
              onKeyDown={(event) => {
                if (event.key === 'Enter' && !saving) void submit()
                if (event.key === 'Escape') onClose()
              }}
              placeholder="D:\ProjectA"
              spellCheck={false}
              aria-label="工作区绝对路径"
              className="w-full rounded-lg border border-slate-200 px-3 py-1.5 font-mono text-xs text-slate-800 outline-none focus:border-sky-400"
            />
          </div>
          {!looksAbsolute && value.trim().length > 0 && (
            <div className="mt-1 text-[11px] text-slate-400">看起来不是绝对路径，请以盘符开头</div>
          )}

          <div className="mt-4 flex items-center justify-between">
            <span className="text-[11px] font-medium text-slate-500">
              附加目录（用 <span className="font-mono">@名字/路径</span> 访问，默认只读）
            </span>
            <button
              type="button"
              onClick={addRow}
              className="flex items-center gap-1 rounded px-1.5 py-1 text-[11px] text-sky-600 hover:bg-sky-50"
            >
              <Plus className="h-3 w-3" /> 添加
            </button>
          </div>

          {rows.length === 0 && (
            <div className="mt-1 rounded-lg border border-dashed border-slate-200 px-3 py-2 text-[11px] text-slate-400">
              例如挂载 <span className="font-mono">@libs → D:\SharedLibs</span>，之后模型就能用
              <span className="font-mono"> read_file("@libs/http/client.py")</span> 读依赖库。
            </div>
          )}

          <div className="mt-1 space-y-2">
            {rows.map((row, index) => (
              <div key={index} className="rounded-lg border border-slate-200 p-2">
                <div className="flex items-center gap-1.5">
                  <span className="font-mono text-xs text-slate-400">@</span>
                  <input
                    id={`extra-name-${index}`}
                    name={`extra_name_${index}`}
                    value={row.name}
                    onChange={(event) => updateRow(index, { name: event.target.value })}
                    placeholder="libs"
                    aria-label={`附加目录 ${index + 1} 挂载名`}
                    spellCheck={false}
                    className="w-24 rounded border border-slate-200 px-2 py-1 font-mono text-xs outline-none focus:border-sky-400"
                  />
                  <input
                    id={`extra-path-${index}`}
                    name={`extra_path_${index}`}
                    value={row.path}
                    onChange={(event) => updateRow(index, { path: event.target.value })}
                    placeholder="D:\SharedLibs"
                    aria-label={`附加目录 ${index + 1} 路径`}
                    spellCheck={false}
                    className="min-w-0 flex-1 rounded border border-slate-200 px-2 py-1 font-mono text-xs outline-none focus:border-sky-400"
                  />
                  <button
                    type="button"
                    onClick={() => setPickerTarget(index)}
                    aria-label={`浏览选择附加目录 ${index + 1}`}
                    title="点击浏览文件夹"
                    className="shrink-0 rounded p-0.5 text-slate-400 hover:bg-slate-100 hover:text-sky-600"
                  >
                    <FolderOpen className="h-3.5 w-3.5" />
                  </button>
                  <button
                    type="button"
                    onClick={() => removeRow(index)}
                    aria-label={`删除附加目录 ${index + 1}`}
                    className="rounded p-1 text-slate-400 hover:bg-rose-50 hover:text-rose-600"
                  >
                    <X className="h-3.5 w-3.5" />
                  </button>
                </div>
                <label className="mt-1.5 flex cursor-pointer items-center gap-1.5 text-[11px] text-slate-500">
                  <input
                    id={`extra-writable-${index}`}
                    name={`extra_writable_${index}`}
                    type="checkbox"
                    checked={row.writable}
                    onChange={(event) => updateRow(index, { writable: event.target.checked })}
                    aria-label={`附加目录 ${index + 1} 允许写入`}
                    className="h-3 w-3 accent-sky-600"
                  />
                  允许写入（默认只读，建议保持只读）
                </label>
              </div>
            ))}
          </div>

          <div className="mt-3 rounded-lg bg-amber-50 px-3 py-2 text-[11px] leading-relaxed text-amber-700">
            当前 AI 将获得主目录的读写权限、附加目录的只读权限。系统核心目录（如 C:\Windows）、
            磁盘根目录、用户主目录与应用数据目录会被拒绝。
          </div>

          {workspace && (
            <div className="mt-2 truncate text-[11px] text-slate-500" title={workspace}>
              当前生效：<span className="font-mono">{workspace}</span>
              {extras.length > 0 && (
                <span className="ml-1 text-slate-400">
                  （+{extras.map((extra) => `@${extra.name}`).join('、')}）
                </span>
              )}
            </div>
          )}
          {error && (
            <div role="alert" className="mt-2 rounded-lg bg-rose-50 px-3 py-2 text-[11px] text-rose-700">
              {error}
            </div>
          )}
        </div>

        <div className="flex items-center justify-end gap-2 border-t border-slate-200 px-4 py-3">
          <button
            type="button"
            onClick={() => void onReset()}
            disabled={saving}
            className="mr-auto flex items-center gap-1 rounded-lg px-2 py-1.5 text-xs text-slate-500 hover:bg-slate-100 disabled:opacity-50"
          >
            <RotateCcw className="h-3.5 w-3.5" /> 恢复默认
          </button>
          <button
            type="button"
            onClick={onClose}
            className="rounded-lg px-3 py-1.5 text-xs text-slate-600 hover:bg-slate-100"
          >
            取消
          </button>
          <button
            type="button"
            onClick={() => void submit()}
            disabled={saving || value.trim().length === 0}
            className="rounded-lg bg-sky-600 px-3 py-1.5 text-xs font-medium text-white disabled:bg-slate-300"
          >
            {saving ? '切换中…' : '应用'}
          </button>
        </div>
      </div>

      <FolderPickerDialog
        open={pickerTarget !== null}
        initialPath={
          pickerTarget === 'main' ? value : pickerTarget !== null ? rows[pickerTarget]?.path ?? '' : ''
        }
        onClose={() => setPickerTarget(null)}
        onPick={(picked) => {
          if (pickerTarget === 'main') setValue(picked)
          else if (pickerTarget !== null) updateRow(pickerTarget, { path: picked })
          setPickerTarget(null)
        }}
      />
    </div>
  )
}
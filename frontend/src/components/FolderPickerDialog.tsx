import { useCallback, useEffect, useRef, useState } from 'react'
import { ArrowUp, Folder, FolderPlus, HardDrive, Loader2, X } from 'lucide-react'
import { api } from '../api/client'
import type { FsListing } from '../types'

/**
 * 目录选择器：点击浏览真实文件系统（后端只列目录、不读文件），支持新建文件夹。
 * 浏览器不会把绝对路径暴露给网页，所以这里走后端 `/api/fs/dirs`。
 */
export function FolderPickerDialog({
  open,
  initialPath,
  onClose,
  onPick,
}: {
  open: boolean
  initialPath?: string
  onClose: () => void
  onPick: (path: string) => void
}) {
  const [listing, setListing] = useState<FsListing | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)
  const [newName, setNewName] = useState('')
  const [creating, setCreating] = useState(false)

  const load = useCallback(async (path: string) => {
    setLoading(true)
    setError(null)
    try {
      setListing(await api.listDirs(path))
    } catch (err) {
      setError(err instanceof Error ? err.message : '读取目录失败')
    } finally {
      setLoading(false)
    }
  }, [])

  // 同一路径不重复请求：避免 prop 抖动造成的连环加载
  const lastLoadedRef = useRef<string | null>(null)

  useEffect(() => {
    if (!open) {
      lastLoadedRef.current = null
      return
    }
    const target = initialPath?.trim() ?? ''
    if (lastLoadedRef.current === target) return
    lastLoadedRef.current = target
    setNewName('')
    setError(null)
    void load(target)
  }, [open, initialPath, load])

  if (!open) return null

  const current = listing?.path ?? ''
  const segments = current ? current.split(/[\\/]/).filter(Boolean) : []

  const mkdir = async () => {
    if (!current) return
    setCreating(true)
    setError(null)
    try {
      const created = await api.makeDir(current, newName.trim())
      setNewName('')
      onPick(created.path)
      onClose()
    } catch (err) {
      setError(err instanceof Error ? err.message : '创建失败')
    } finally {
      setCreating(false)
    }
  }

  return (
    <div className="fixed inset-0 z-[60] flex items-center justify-center bg-slate-900/40 p-4">
      <div role="dialog" aria-label="选择文件夹" className="flex max-h-[80vh] w-full max-w-xl flex-col rounded-xl bg-white shadow-2xl">
        <div className="flex items-center gap-2 border-b border-slate-200 px-4 py-3">
          <Folder className="h-4 w-4 text-sky-600" />
          <span className="text-sm font-semibold text-slate-800">选择文件夹</span>
          <button
            type="button"
            onClick={onClose}
            aria-label="关闭选择器"
            className="ml-auto rounded p-1 text-slate-400 hover:bg-slate-100"
          >
            <X className="h-4 w-4" />
          </button>
        </div>

        {/* 位置：盘符 / 面包屑 */}
        <div className="flex flex-wrap items-center gap-1 border-b border-slate-100 px-4 py-2 text-[11px]">
          <button
            type="button"
            onClick={() => void load('')}
            className="rounded px-1.5 py-0.5 text-sky-600 hover:bg-sky-50"
          >
            此电脑
          </button>
          {segments.map((segment, index) => {
            const target = segments.slice(0, index + 1).join('\\')
            return (
              <span key={`${segment}-${index}`} className="flex items-center gap-1">
                <span className="text-slate-300">/</span>
                <button
                  type="button"
                  onClick={() => void load(index === 0 ? `${target}\\` : target)}
                  className="rounded px-1 py-0.5 font-mono text-slate-600 hover:bg-slate-100"
                >
                  {segment}
                </button>
              </span>
            )
          })}
        </div>

        {/* 目录列表 */}
        <div className="min-h-[180px] flex-1 overflow-y-auto px-2 py-2">
          {loading && (
            <div className="flex items-center gap-2 px-2 py-2 text-xs text-slate-400">
              <Loader2 className="h-3.5 w-3.5 animate-spin" /> 读取中…
            </div>
          )}

          {!loading && listing && (
            <>
              {listing.parent && (
                <button
                  type="button"
                  onClick={() => void load(listing.parent!)}
                  className="flex w-full items-center gap-2 rounded-lg px-2 py-1.5 text-left text-xs text-slate-500 hover:bg-slate-100"
                >
                  <ArrowUp className="h-3.5 w-3.5" /> 上一级
                </button>
              )}

              {listing.drives.length > 0 && !current && (
                <div className="px-2 py-1 text-[11px] text-slate-400">磁盘驱动器</div>
              )}
              {!current &&
                listing.drives.map((drive) => (
                  <button
                    key={drive}
                    type="button"
                    onClick={() => void load(drive)}
                    className="flex w-full items-center gap-2 rounded-lg px-2 py-1.5 text-left text-xs text-slate-700 hover:bg-sky-50"
                  >
                    <HardDrive className="h-3.5 w-3.5 text-slate-400" />
                    <span className="font-mono">{drive}</span>
                  </button>
                ))}

              {current && listing.entries.length === 0 && (
                <div className="px-2 py-2 text-xs text-slate-400">这个文件夹里没有子文件夹</div>
              )}
              {current &&
                listing.entries.map((entry) => (
                  <button
                    key={entry.path}
                    type="button"
                    onDoubleClick={() => {
                      onPick(entry.path)
                      onClose()
                    }}
                    onClick={() => void load(entry.path)}
                    title={`进入 ${entry.name}（双击直接选择）`}
                    className="flex w-full items-center gap-2 rounded-lg px-2 py-1.5 text-left text-xs text-slate-700 hover:bg-sky-50"
                  >
                    <Folder className="h-3.5 w-3.5 shrink-0 text-amber-500" />
                    <span className="truncate">{entry.name}</span>
                  </button>
                ))}

              {listing.truncated && (
                <div className="px-2 py-1 text-[11px] text-slate-400">（目录过多，只显示前 500 个）</div>
              )}
            </>
          )}

          {error && (
            <div role="alert" className="mx-2 mt-2 rounded-lg bg-rose-50 px-3 py-2 text-[11px] text-rose-700">
              {error}
            </div>
          )}
        </div>

        {/* 新建文件夹 + 确认 */}
        <div className="border-t border-slate-200 px-4 py-3">
          <div className="flex items-center gap-2">
            <FolderPlus className="h-4 w-4 shrink-0 text-slate-400" />
            <input
              id="folder-new-name"
              name="folder-name"
              value={newName}
              onChange={(event) => setNewName(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === 'Enter' && newName.trim() && current) void mkdir()
              }}
              placeholder={current ? '新建文件夹（输入名字后回车）' : '先进入一个盘符再新建文件夹'}
              disabled={!current}
              aria-label="新建文件夹名"
              className="min-w-0 flex-1 rounded border border-slate-200 px-2 py-1 text-xs outline-none focus:border-sky-400 disabled:bg-slate-50"
            />
            <button
              type="button"
              onClick={() => void mkdir()}
              disabled={!current || !newName.trim() || creating}
              className="rounded border border-slate-200 px-2 py-1 text-xs text-slate-600 disabled:opacity-40"
            >
              新建
            </button>
          </div>
          <div className="mt-2 truncate font-mono text-[11px] text-slate-500" title={current}>
            当前：{current || '（未选择）'}
          </div>
        </div>

        <div className="flex items-center justify-end gap-2 border-t border-slate-200 px-4 py-3">
          <button
            type="button"
            onClick={onClose}
            className="rounded-lg px-3 py-1.5 text-xs text-slate-600 hover:bg-slate-100"
          >
            取消
          </button>
          <button
            type="button"
            disabled={!current}
            onClick={() => {
              if (!current) return
              onPick(current)
              onClose()
            }}
            className="rounded-lg bg-sky-600 px-3 py-1.5 text-xs font-medium text-white disabled:bg-slate-300"
          >
            选择此文件夹
          </button>
        </div>
      </div>
    </div>
  )
}
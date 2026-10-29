import { useEffect, useRef, useState } from 'react'
import { ChevronDown, ChevronRight, FileText, Loader2, Trash2, UploadCloud } from 'lucide-react'
import { useChatStore } from '../store/chatStore'
import type { KnowledgeDoc } from '../types'

function humanSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(0)} KB`
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`
}

function statusText(doc: KnowledgeDoc): string {
  if (doc.status === 'parsing') return '解析中…'
  if (doc.status === 'failed') return `失败：${doc.error ?? '未知错误'}`
  return `就绪 · ${doc.chunk_count} 片段`
}

/** 侧边栏「知识库」面板：拖拽上传 + 文档列表 + 解析状态。 */
export function KnowledgePanel() {
  const docs = useChatStore((state) => state.kbDocs)
  const status = useChatStore((state) => state.kbStatus)
  const upload = useChatStore((state) => state.kbUpload)
  const error = useChatStore((state) => state.kbError)
  const loadKb = useChatStore((state) => state.loadKb)
  const uploadKbDoc = useChatStore((state) => state.uploadKbDoc)
  const deleteKbDoc = useChatStore((state) => state.deleteKbDoc)

  const [open, setOpen] = useState(true)
  const [dragging, setDragging] = useState(false)
  const [confirmId, setConfirmId] = useState<string | null>(null)
  const inputRef = useRef<HTMLInputElement | null>(null)

  useEffect(() => {
    void loadKb()
  }, [loadKb])

  const accept = status?.supported?.join(',') ?? '.pdf,.docx,.txt,.md'

  const handleFiles = (files: FileList | null) => {
    if (!files || files.length === 0) return
    // 一次允许拖多个：顺序上传（后端每个文件一个后台任务）
    Array.from(files).forEach((file) => void uploadKbDoc(file))
  }

  return (
    <div className="px-4 pb-3">
      <button
        type="button"
        onClick={() => setOpen((value) => !value)}
        className="mb-1 flex w-full items-center gap-1 text-[11px] font-medium text-gray-500 hover:text-gray-700"
        aria-expanded={open}
        aria-label="知识库"
      >
        {open ? <ChevronDown className="h-3 w-3" /> : <ChevronRight className="h-3 w-3" />}
        知识库
        <span className="ml-1 font-normal text-gray-400">
          {status ? `${status.docs} 文档 · ${status.chunks} 片段` : ''}
        </span>
      </button>

      {open && (
        <div>
          <div
            onDragOver={(event) => {
              event.preventDefault()
              setDragging(true)
            }}
            onDragLeave={() => setDragging(false)}
            onDrop={(event) => {
              event.preventDefault()
              setDragging(false)
              handleFiles(event.dataTransfer?.files ?? null)
            }}
            onClick={() => inputRef.current?.click()}
            className={`cursor-pointer rounded-lg border border-dashed px-2 py-3 text-center text-[11px] transition-colors ${
              dragging
                ? 'border-gray-900 bg-gray-100 text-gray-800'
                : 'border-gray-300 bg-white text-gray-500 hover:border-gray-400'
            }`}
          >
            <UploadCloud className="mx-auto mb-1 h-4 w-4" />
            {upload ? (
              <span>
                上传 {upload.name} · {upload.percent}%
              </span>
            ) : (
              <span>拖入文件，或点击选择</span>
            )}
            <div className="mt-0.5 text-[10px] text-gray-400">
              {status?.supported?.join(' / ') ?? '.pdf / .docx / .txt'}
              {status ? ` · ≤${status.max_file_mb}MB` : ''}
            </div>
          </div>
          <input
            ref={inputRef}
            type="file"
            multiple
            accept={accept}
            aria-label="选择文件上传"
            className="hidden"
            onChange={(event) => {
              handleFiles(event.target.files)
              event.target.value = ''
            }}
          />

          {error && <div className="mt-1 text-[11px] text-rose-600">{error}</div>}

          {docs.length > 0 && (
            <ul className="mt-2 space-y-1">
              {docs.map((doc) => (
                <li
                  key={doc.id}
                  className="group flex items-start gap-1.5 rounded-md px-1 py-1 hover:bg-gray-50"
                  data-testid={`kb-doc-${doc.id}`}
                >
                  {doc.status === 'parsing' ? (
                    <Loader2 className="mt-0.5 h-3.5 w-3.5 shrink-0 animate-spin text-gray-500" />
                  ) : (
                    <FileText
                      className={`mt-0.5 h-3.5 w-3.5 shrink-0 ${
                        doc.status === 'failed' ? 'text-rose-500' : 'text-gray-400'
                      }`}
                    />
                  )}
                  <div className="min-w-0 flex-1">
                    <div className="truncate text-[12px] text-gray-800" title={doc.name}>
                      {doc.name}
                    </div>
                    <div
                      className={`text-[10px] ${
                        doc.status === 'failed' ? 'text-rose-500' : 'text-gray-400'
                      }`}
                    >
                      {statusText(doc)} · {humanSize(doc.size)}
                    </div>
                  </div>
                  {confirmId === doc.id ? (
                    <button
                      type="button"
                      onClick={() => {
                        void deleteKbDoc(doc.id)
                        setConfirmId(null)
                      }}
                      className="shrink-0 rounded bg-rose-600 px-1 text-[10px] text-white"
                    >
                      确认
                    </button>
                  ) : (
                    <button
                      type="button"
                      onClick={() => setConfirmId(doc.id)}
                      aria-label={`删除 ${doc.name}`}
                      className="shrink-0 rounded p-0.5 text-gray-400 opacity-0 transition-opacity hover:bg-gray-100 hover:text-rose-600 group-hover:opacity-100"
                    >
                      <Trash2 className="h-3.5 w-3.5" />
                    </button>
                  )}
                </li>
              ))}
            </ul>
          )}

          {status && (
            <div className="mt-1 text-[10px] text-gray-400" data-testid="kb-pipeline">
              {status.pipeline}
            </div>
          )}
        </div>
      )}
    </div>
  )
}
import { Brain, Database, ImagePlus, Paperclip, Plus, SendHorizonal, ShieldCheck, Square, X, Zap } from 'lucide-react'
import { WorkspaceDialog } from './WorkspaceDialog'
import { ModelManagerDialog } from './ModelManagerDialog'
import { ModelMenu } from './ModelMenu'
import { formatMoney, formatRate, INPUT_PRICE_PER_1K_TOKENS, savedAmount } from '../lib/pricing'
import { useEffect, useLayoutEffect, useRef, useState } from 'react'
import type { KeyboardEvent } from 'react'
import { useChatStore } from '../store/chatStore'
import { api } from '../api/client'
import { compressImageFile } from '../lib/imageCompress'
import type { Attachment } from '../types'

/** 沙箱说明文案（徽章 + Tooltip 共用，也是测试断言的目标） */
const SANDBOX_HINT =
  'AI 文件读写工具被严格限制在当前 workspace 内，无法越权访问系统文件。' +
  '注意：run_python_code 是进程级轻量沙箱，不是 OS 隔离；run_shell 的审批也只是人工确认，不构成安全隔离。'

/** textarea 自适应高度的上限，与 class 里的 max-h-48（12rem/192px）保持一致 */
const TEXTAREA_MAX_HEIGHT = 192

/** 把 token 总量压成紧凑写法：12500 → 12.5K，591000000 → 591M */
function formatTokens(value: number): string {
  if (value >= 1_000_000_000) return `${(value / 1_000_000_000).toFixed(value >= 10_000_000_000 ? 0 : 1)}B`
  if (value >= 1_000_000) return `${(value / 1_000_000).toFixed(value >= 10_000_000 ? 0 : 1)}M`
  if (value >= 1_000) return `${(value / 1_000).toFixed(value >= 10_000 ? 0 : 1)}K`
  return String(Math.max(0, Math.round(value)))
}

export function Composer() {
  const draft = useChatStore((state) => state.draft)
  const setDraft = useChatStore((state) => state.setDraft)
  const streaming = useChatStore((state) => state.streaming)
  const sendMessage = useChatStore((state) => state.sendMessage)
  const stopStreaming = useChatStore((state) => state.stopStreaming)
  const focusSignal = useChatStore((state) => state.draftFocusSignal)
  const kbStatus = useChatStore((state) => state.kbStatus)
  const uploadKbDocs = useChatStore((state) => state.uploadKbDocs)
  const workspace = useChatStore((state) => state.workspace)
  const loadWorkspace = useChatStore((state) => state.loadWorkspace)
  const stats = useChatStore((state) => state.stats)
  const contextUsage = useChatStore((state) => state.contextUsage)
  const models = useChatStore((state) => state.models)
  const model = useChatStore((state) => state.model)
  const setModel = useChatStore((state) => state.setModel)
  const [workspaceOpen, setWorkspaceOpen] = useState(false)
  const [modelsOpen, setModelsOpen] = useState(false)
  const textareaRef = useRef<HTMLTextAreaElement | null>(null)
  const fileRef = useRef<HTMLInputElement | null>(null)
  const imageRef = useRef<HTMLInputElement | null>(null)
  const [pendingImages, setPendingImages] = useState<
    Array<{ attachment: Attachment; previewUrl: string; name: string }>
  >([])
  const [imageError, setImageError] = useState<string | null>(null)
  const [uploadingImages, setUploadingImages] = useState(false)
  const pendingImagesRef = useRef(pendingImages)

  const selectedModel = models.find((item) => item.name === model) ?? models.find((item) => item.is_default)
  const supportsVision = selectedModel?.supports_vision === true

  // 支持格式由后端 /api/kb/status 给出（缺 pypdf/python-docx 时就没有 .pdf/.docx）
  const accept = kbStatus?.supported?.length
    ? kbStatus.supported.join(',')
    : '.pdf,.docx,.txt,.md,.csv,.json,.log'

  // 盾牌 Tooltip 里要显示当前工作区；首次挂载拉一次
  useEffect(() => {
    void loadWorkspace()
  }, [loadWorkspace])

  useEffect(() => {
    pendingImagesRef.current = pendingImages
  }, [pendingImages])

  // 卸载时释放图片预览的 object URL
  useEffect(() => {
    return () => {
      pendingImagesRef.current.forEach((item) => URL.revokeObjectURL(item.previewUrl))
    }
  }, [])

  // 点了消息上的「编辑」：把焦点移回输入框，光标放到末尾
  useEffect(() => {
    if (focusSignal === 0) return
    const element = textareaRef.current
    if (!element) return
    element.focus()
    const end = element.value.length
    element.setSelectionRange(end, end)
  }, [focusSignal])

  // 输入框高度随内容自适应（上限由 CSS max-h-48 与 TEXTAREA_MAX_HEIGHT 双保险）
  useLayoutEffect(() => {
    const element = textareaRef.current
    if (!element) return
    element.style.height = 'auto'
    element.style.height = `${Math.min(element.scrollHeight || 0, TEXTAREA_MAX_HEIGHT)}px`
  }, [draft])

  const submit = async () => {
    if (streaming || uploadingImages) return
    if (draft.trim().length === 0 && pendingImages.length === 0) return
    if (pendingImages.length > 0 && !supportsVision) {
      setImageError('当前模型不支持图片，请先切换到视觉模型（如 qwen3-vl-plus）')
      return
    }
    const attachments = pendingImages.map((item) => item.attachment)
    pendingImages.forEach((item) => URL.revokeObjectURL(item.previewUrl))
    setPendingImages([])
    setImageError(null)
    await sendMessage(draft, attachments)
  }

  const onPickImages = async (files: File[]) => {
    if (files.length === 0) return
    if (!supportsVision) {
      setImageError('当前模型不支持图片，请先切换到视觉模型（如 qwen3-vl-plus）')
      return
    }
    if (pendingImages.length + files.length > 4) {
      setImageError('每条消息最多 4 张图片')
      return
    }
    setImageError(null)
    setUploadingImages(true)
    const added: Array<{ attachment: Attachment; previewUrl: string; name: string }> = []
    try {
      for (const file of files) {
        try {
          const compressed = await compressImageFile(file)
          const attachment = await api.uploadImage(compressed)
          added.push({
            attachment,
            previewUrl: URL.createObjectURL(compressed),
            name: compressed.name,
          })
        } catch (error) {
          setImageError(error instanceof Error ? error.message : '图片上传失败')
        }
      }
      if (added.length > 0) setPendingImages((current) => [...current, ...added].slice(0, 4))
    } finally {
      setUploadingImages(false)
    }
  }

  const removeImage = (previewUrl: string) => {
    URL.revokeObjectURL(previewUrl)
    setPendingImages((current) => current.filter((item) => item.previewUrl !== previewUrl))
    setImageError(null)
  }

  const onKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key === 'Enter' && !event.shiftKey) {
      event.preventDefault()
      void submit()
    }
  }

  const sandboxTitle = workspace
    ? `工作区沙箱保护中：${workspace}。${SANDBOX_HINT}点击切换工作区。`
    : `${SANDBOX_HINT}点击切换工作区。`

  // 节省金额与命中率都来自后端（分母只有 prompt tokens）；数据缺失时给「暂无数据」
  const hasStats = Boolean(stats && stats.prompt_tokens > 0)
  const savings = hasStats ? savedAmount(stats?.cache_hit_tokens ?? 0) : 0

  return (
    <div>
      <div className="mx-auto w-full max-w-3xl px-4 py-3">
        {/* 输入区 + 工具栏 + 底部提示统一收进一张卡片里；卡片之外只有页面背景 */}
        <div className="rounded-2xl border border-slate-200 bg-white shadow-sm transition-colors focus-within:border-sky-400">
          {pendingImages.length > 0 && (
            <div className="flex flex-wrap gap-2 px-3 pt-2">
              {pendingImages.map((item) => (
                <div key={item.previewUrl} className="relative">
                  <img
                    src={item.previewUrl}
                    alt={item.name}
                    className="h-16 w-16 rounded-lg border border-slate-200 object-cover"
                  />
                  <button
                    type="button"
                    onClick={() => removeImage(item.previewUrl)}
                    aria-label="移除图片"
                    title="移除图片"
                    className="absolute -right-1.5 -top-1.5 flex h-4 w-4 items-center justify-center rounded-full bg-slate-800 text-white shadow-sm hover:bg-rose-600"
                  >
                    <X className="h-2.5 w-2.5" />
                  </button>
                </div>
              ))}
            </div>
          )}

          {imageError && (
            <p className="px-4 pt-1 text-xs text-rose-600" role="alert">
              {imageError}
            </p>
          )}

          <textarea
            id="composer-input"
            name="message"
            ref={textareaRef}
            value={draft}
            onChange={(event) => setDraft(event.target.value)}
            onKeyDown={onKeyDown}
            disabled={streaming}
            rows={1}
            placeholder={
              streaming
                ? 'AI 正在回答……'
                : uploadingImages
                  ? '图片压缩 / 上传中……'
                  : '输入问题，Enter 发送，Shift+Enter 换行'
            }
            className="block max-h-48 min-h-[40px] w-full resize-none bg-transparent px-4 pb-0.5 pt-2 text-sm leading-6 text-slate-800 outline-none placeholder:text-slate-400 disabled:cursor-not-allowed disabled:text-slate-400"
          />

          <div className="flex items-center justify-between gap-2 px-2 pb-1">
            {/* 左侧工具栏：小而并排的「回形针 + 盾牌」 */}
            <div className="flex items-center gap-0.5">
              <button
                type="button"
                data-testid="image-upload-button"
                onClick={() => {
                  if (!supportsVision) {
                    setImageError('当前模型不支持图片，请先切换到视觉模型（如 qwen3-vl-plus）')
                    return
                  }
                  imageRef.current?.click()
                }}
                disabled={streaming || uploadingImages}
                aria-label="上传图片"
                title={
                  supportsVision
                    ? '上传图片（自动压缩，最多 4 张）'
                    : '当前模型不支持图片，请切换到视觉模型'
                }
                className="flex h-7 w-7 shrink-0 items-center justify-center rounded-full text-slate-400 transition-colors hover:bg-slate-100 hover:text-sky-600 disabled:cursor-not-allowed disabled:opacity-40"
              >
                <ImagePlus className="h-4 w-4" />
              </button>

              <button
                type="button"
                onClick={() => fileRef.current?.click()}
                aria-label="上传文档到知识库"
                title="上传文档到知识库（PDF / Word / 文本）"
                className="flex h-7 w-7 shrink-0 items-center justify-center rounded-full text-slate-400 transition-colors hover:bg-slate-100 hover:text-sky-600"
              >
                <Paperclip className="h-4 w-4" />
              </button>

              {/* 沙箱/工作区状态：点击切换工作区，悬浮显示完整说明（校验在后端，见 README 第 58 条） */}
              <button
                type="button"
                data-testid="sandbox-badge"
                onClick={() => setWorkspaceOpen(true)}
                title={sandboxTitle}
                aria-label={`工作区沙箱保护中${workspace ? `：${workspace}` : ''}。点击切换工作区。${SANDBOX_HINT}`}
                className="group relative flex h-7 w-7 shrink-0 items-center justify-center rounded-full text-emerald-600 transition-colors hover:bg-emerald-50"
              >
                <ShieldCheck className="h-4 w-4" />
                <span className="sr-only">工作区沙箱保护中</span>
                {workspace && (
                  <span
                    aria-hidden="true"
                    className="absolute right-1.5 top-1.5 h-1.5 w-1.5 rounded-full bg-emerald-500 ring-1 ring-white"
                  />
                )}
                <span
                  role="tooltip"
                  className="pointer-events-none absolute bottom-full left-0 z-20 mb-2 w-64 rounded-lg bg-slate-900/95 px-2.5 py-1.5 text-[11px] font-normal leading-relaxed text-white opacity-0 shadow-lg transition-opacity duration-150 group-hover:opacity-100 group-focus-visible:opacity-100"
                >
                  <span className="block font-medium">
                    工作区沙箱保护中{workspace ? `：${workspace}` : ''}
                  </span>
                  <span className="mt-0.5 block text-slate-300">{SANDBOX_HINT}</span>
                </span>
              </button>

              <input
                id="composer-image-input"
                name="images"
                ref={imageRef}
                type="file"
                multiple
                accept="image/jpeg,image/png,image/webp"
                aria-label="选择要发送的图片"
                className="hidden"
                onChange={(event) => {
                  void onPickImages(Array.from(event.target.files ?? []))
                  event.target.value = ''
                }}
              />

              <input
                id="composer-file-input"
                name="files"
                ref={fileRef}
                type="file"
                multiple
                accept={accept}
                aria-label="选择要上传的文档"
                className="hidden"
                onChange={(event) => {
                  const files = Array.from(event.target.files ?? [])
                  if (files.length > 0) void uploadKbDocs(files)
                  event.target.value = ''
                }}
              />
            </div>

            {/* 右下角控制区：模型选择（迁移自侧边栏）+ 添加模型 + 圆形发送/停止 */}
            <div className="flex items-center gap-1">
              <ModelMenu
                models={models}
                value={model}
                onChange={setModel}
                onManage={() => setModelsOpen(true)}
                disabled={streaming}
              />
              <button
                type="button"
                onClick={() => setModelsOpen(true)}
                aria-label="模型管理"
                title={streaming ? 'AI 正在回答，请稍后管理模型' : '添加 / 管理模型'}
                className="flex h-7 w-7 shrink-0 items-center justify-center rounded-full text-slate-400 transition-colors hover:bg-slate-100 hover:text-slate-700"
              >
                <Plus className="h-3.5 w-3.5" />
              </button>
              {streaming ? (
                <button
                  type="button"
                  onClick={stopStreaming}
                  aria-label="停止生成"
                  title="停止生成（已生成的内容会保留）"
                  className="flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-rose-600 text-white shadow-sm transition-colors hover:bg-rose-700"
                >
                  <Square className="h-3.5 w-3.5 fill-current" />
                </button>
              ) : (
                <button
                  type="button"
                  onClick={submit}
                  disabled={(draft.trim().length === 0 && pendingImages.length === 0) || uploadingImages}
                  aria-label="发送"
                  title="发送（Enter）"
                  className="flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-sky-600 text-white shadow-sm transition-colors hover:bg-sky-700 disabled:cursor-not-allowed disabled:bg-slate-200 disabled:text-slate-400 disabled:shadow-none"
                >
                  <SendHorizonal className="h-4 w-4" />
                </button>
              )}
            </div>
          </div>

          {/* 底部状态栏：极简线条图标 + 灰字；Token / 节省 / 上下文，不收 Emoji */}
          <div className="flex items-center justify-between gap-3 border-t border-gray-100 px-3.5 py-0.5 text-[10px] leading-4 text-gray-400">
            <span className="shrink-0 truncate">
              {streaming ? 'AI 正在回答……' : 'Enter 发送 · Shift+Enter 换行'}
            </span>
            <span className="flex min-w-0 items-center gap-3 overflow-hidden whitespace-nowrap">
              <span className="flex shrink-0 items-center gap-1" title="当前会话累计 token 消耗">
                <Database className="h-3 w-3 text-gray-400" />
                累计{' '}
                <span className="text-gray-600">{formatTokens(stats?.total_tokens ?? 0)}</span> tok
              </span>
              <span
                className="flex shrink-0 items-center gap-1"
                title={`按输入 ${INPUT_PRICE_PER_1K_TOKENS} 元/千 token 粗略估算`}
              >
                <Zap className="h-3 w-3 text-gray-400" />
                {hasStats ? (
                  <>
                    节省{' '}
                    <span className="text-gray-600">{formatRate(stats?.cache_hit_rate ?? 0)}</span>
                    <span className="text-gray-400">（约 {formatMoney(savings)}）</span>
                  </>
                ) : (
                  <>
                    节省 <span className="text-gray-500">暂无数据</span>
                  </>
                )}
              </span>
              <span className="flex shrink-0 items-center gap-1" title="当前会话上下文占用率（约值，分母为模型真实 context_window）">
                <Brain className="h-3 w-3 text-gray-400" />
                当前上下文{' '}
                <span
                  className={
                    contextUsage !== null && contextUsage >= 0.95
                      ? 'text-rose-600'
                      : contextUsage !== null && contextUsage >= 0.8
                        ? 'text-amber-600'
                        : 'text-gray-600'
                  }
                >
                  {contextUsage !== null ? formatRate(contextUsage, 0) : '—'}
                </span>
              </span>
            </span>
            <span className="hidden shrink-0 md:inline">内容由模型生成，请自行核对</span>
          </div>
        </div>

        <WorkspaceDialog open={workspaceOpen} onClose={() => setWorkspaceOpen(false)} />
        <ModelManagerDialog open={modelsOpen} onClose={() => setModelsOpen(false)} />
      </div>
    </div>
  )
}
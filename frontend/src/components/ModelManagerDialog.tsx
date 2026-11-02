import { useState } from 'react'
import { Download, Plus, Server, Trash2, X } from 'lucide-react'
import { api } from '../api/client'
import { isTextModel } from '../lib/modelFilter'
import { useChatStore } from '../store/chatStore'
import type { CustomModelPayload } from '../types'
import { ModelPicker } from './ModelPicker'

type FormState = {
  name: string
  base_url: string
  model: string
  api_key: string
  /** 字符串形式便于输入框留空；提交时转 number，空串 → null */
  max_tokens: string
  context_window: string
}
const EMPTY_FORM: FormState = {
  name: '',
  base_url: '',
  model: '',
  api_key: '',
  max_tokens: '',
  context_window: '',
}

const NAME_RE = /^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$/

function validate(form: FormState): string | null {
  if (!NAME_RE.test(form.name.trim())) {
    return '模型名只能包含字母、数字、下划线、点和连字符（不超过 64 个字符）'
  }
  if (!/^https?:\/\//i.test(form.base_url.trim())) return 'Base URL 必须以 http:// 或 https:// 开头'
  if (!form.model.trim()) return 'Model ID 不能为空'
  if (!form.api_key.trim()) return 'API Key 不能为空'
  return null
}

/** 模型管理面板：内置只读、自定义可删、底部表单可加。 */
export function ModelManagerDialog({ open, onClose }: { open: boolean; onClose: () => void }) {
  const models = useChatStore((state) => state.models)
  const addCustomModel = useChatStore((state) => state.addCustomModel)
  const deleteCustomModel = useChatStore((state) => state.deleteCustomModel)
  const streaming = useChatStore((state) => state.streaming)

  const [form, setForm] = useState<FormState>(EMPTY_FORM)
  const [formError, setFormError] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)
  const [confirmName, setConfirmName] = useState<string | null>(null)
  // 拉取模型列表：成功则把 Model ID 输入框换成下拉框；失败只是提示，仍可手动输入
  const [fetched, setFetched] = useState<string[] | null>(null)
  const [fetching, setFetching] = useState(false)
  const [fetchHint, setFetchHint] = useState<string | null>(null)

  if (!open) return null

  const builtin = models.filter((item) => !item.is_custom)
  const custom = models.filter((item) => item.is_custom)

  const onChange = (key: keyof FormState) => (event: React.ChangeEvent<HTMLInputElement>) => {
    setForm((prev) => ({ ...prev, [key]: event.target.value }))
    setFormError(null)
    if (key === 'base_url') {
      // 换了地址，之前拉到的列表就失效了
      setFetched(null)
      setFetchHint(null)
    }
  }

  const onFetchModels = async () => {
    const baseUrl = form.base_url.trim()
    const apiKey = form.api_key.trim()
    if (!baseUrl || !apiKey) {
      setFetchHint('先填好 Base URL 和 API Key，再拉取模型列表')
      return
    }
    setFetching(true)
    setFetchHint(null)
    try {
      const result = await api.fetchModels(baseUrl, apiKey) // Key 只在这一个请求里传一次
      setFetched(result.models)
      const hidden = result.models.filter((id) => !isTextModel(id)).length
      // 不自动选中任何模型：让用户自己按关键字搜、自己选，避免"搜索框被塞了一个值"
      setForm((prev) => ({ ...prev, model: '' }))
      setFetchHint(
        hidden > 0
          ? `拉到 ${result.count} 个模型（已隐藏 ${hidden} 个非纯文本），请在上方搜索并选择`
          : `拉到 ${result.count} 个模型，请在上方搜索并选择`,
      )
    } catch (error) {
      setFetched(null)
      setFetchHint(
        `${error instanceof Error ? error.message : '拉取失败'}（已保留手动输入，可继续填写 Model ID）`,
      )
    } finally {
      setFetching(false)
    }
  }

  const onSave = async () => {
    const error = validate(form)
    if (error) {
      setFormError(error)
      return
    }
    setSaving(true)
    const payload: CustomModelPayload = {
      name: form.name.trim(),
      base_url: form.base_url.trim(),
      model: form.model.trim(),
      api_key: form.api_key.trim(), // 粘贴带的空格在这里去掉
    }
    // 留空就不发这个字段，交给后端按默认值 / 模型名推断，
    // 避免前端硬塞一个可能不对的数字（也避免老后端收到 null 报错）
    const maxTokens = form.max_tokens.trim()
    if (maxTokens) payload.max_tokens = Number(maxTokens)
    const contextWindow = form.context_window.trim()
    if (contextWindow) payload.context_window = Number(contextWindow)

    const ok = await addCustomModel(payload)
    setSaving(false)
    if (ok) {
      setForm(EMPTY_FORM)
      onClose()
    }
  }

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-slate-900/40 p-4"
      onClick={onClose}
      role="presentation"
    >
      <div
        className="max-h-[88vh] w-full max-w-lg overflow-y-auto rounded-xl bg-white p-5 shadow-xl"
        onClick={(event) => event.stopPropagation()}
        role="dialog"
        aria-modal="true"
        aria-label="模型管理"
      >
        <div className="mb-3 flex items-center gap-2">
          <Server className="h-4 w-4 text-slate-500" />
          <h2 className="text-sm font-semibold text-slate-800">模型管理</h2>
          <button
            type="button"
            onClick={onClose}
            className="ml-auto rounded p-1 text-slate-400 hover:bg-slate-100"
            aria-label="关闭"
          >
            <X className="h-4 w-4" />
          </button>
        </div>

        {streaming && (
          <div className="mb-3 rounded-lg bg-amber-50 px-3 py-1.5 text-[11px] text-amber-700">
            AI 正在回答，暂不能修改模型。请等待当前回答结束。
          </div>
        )}

        <div className="mb-1 text-[11px] font-medium text-slate-500">内置模型（来自 config.yaml，只读）</div>
        <div className="mb-4 divide-y divide-slate-100 rounded-lg border border-slate-200">
          {builtin.length === 0 && <div className="px-3 py-2 text-xs text-slate-400">（无）</div>}
          {builtin.map((item) => (
            <div key={item.name} className="flex items-center gap-2 px-3 py-2 text-xs">
              <span className="w-24 shrink-0 truncate font-medium text-slate-700">{item.name}</span>
              <span className="min-w-0 flex-1 truncate font-mono text-slate-500">{item.model}</span>
              <span className={item.has_key ? 'text-emerald-600' : 'text-amber-600'}>
                {item.has_key ? '已配置密钥' : '未配置密钥'}
              </span>
            </div>
          ))}
        </div>

        <div className="mb-1 text-[11px] font-medium text-slate-500">自定义模型（存在本地数据库）</div>
        <div className="mb-4 divide-y divide-slate-100 rounded-lg border border-slate-200">
          {custom.length === 0 && (
            <div className="px-3 py-2 text-xs text-slate-400">还没有自定义模型</div>
          )}
          {custom.map((item) => (
            <div key={item.name} className="px-3 py-2 text-xs">
              <div className="flex items-center gap-2">
                <span className="w-24 shrink-0 truncate font-medium text-slate-700">{item.name}</span>
                <span className="min-w-0 flex-1 truncate font-mono text-slate-500">{item.model}</span>
                <span className="shrink-0 font-mono text-slate-400">{item.api_key_masked}</span>
                <button
                  type="button"
                  onClick={() => setConfirmName(item.name)}
                  disabled={streaming}
                  aria-label={`删除模型 ${item.name}`}
                  className="shrink-0 rounded p-1 text-slate-400 hover:bg-rose-50 hover:text-rose-600 disabled:opacity-40"
                >
                  <Trash2 className="h-3.5 w-3.5" />
                </button>
              </div>
              <div className="mt-0.5 truncate pl-24 text-[11px] text-slate-400">{item.base_url}</div>
              {confirmName === item.name && (
                <div className="mt-2 rounded-md bg-rose-50 px-2 py-1.5 text-[11px] text-rose-700">
                  删除模型 <b>{item.name}</b>？它会从下拉框移除，已有会话再发消息会提示重新选择。
                  <div className="mt-1 flex gap-2">
                    <button
                      type="button"
                      onClick={async () => {
                        const target = item.name
                        setConfirmName(null)
                        await deleteCustomModel(target)
                      }}
                      className="rounded bg-rose-600 px-2 py-0.5 text-white hover:bg-rose-700"
                    >
                      确认删除
                    </button>
                    <button
                      type="button"
                      onClick={() => setConfirmName(null)}
                      className="rounded border border-rose-200 px-2 py-0.5 text-rose-700 hover:bg-rose-100"
                    >
                      取消
                    </button>
                  </div>
                </div>
              )}
            </div>
          ))}
        </div>

        <div className="mb-2 text-[11px] font-medium text-slate-500">添加模型</div>
        <div className="space-y-2">
          <input
            id="model-name"
            name="model_name"
            value={form.name}
            onChange={onChange('name')}
            disabled={streaming}
            placeholder="模型名称，例如 moonshot"
            aria-label="模型名称"
            className="w-full rounded-lg border border-slate-200 px-3 py-1.5 text-sm outline-none focus:border-sky-400"
          />
          <input
            id="model-base-url"
            name="base_url"
            value={form.base_url}
            onChange={onChange('base_url')}
            disabled={streaming}
            placeholder="Base URL，例如 https://api.moonshot.cn/v1"
            aria-label="Base URL"
            className="w-full rounded-lg border border-slate-200 px-3 py-1.5 text-sm outline-none focus:border-sky-400"
          />
          <div className="flex items-start gap-2">
            {fetched ? (
              <ModelPicker
                models={fetched}
                value={form.model}
                disabled={streaming}
                onChange={(modelId) => {
                  setForm((prev) => ({ ...prev, model: modelId }))
                  setFormError(null)
                }}
              />
            ) : (
              <input
                id="model-id"
                name="model_id"
                value={form.model}
                onChange={onChange('model')}
                disabled={streaming}
                placeholder="Model ID，例如 moonshot-v1-8k"
                aria-label="Model ID"
                className="min-w-0 flex-1 rounded-lg border border-slate-200 px-3 py-1.5 text-sm outline-none focus:border-sky-400"
              />
            )}
            <button
              type="button"
              onClick={() => void onFetchModels()}
              disabled={fetching || streaming}
              className="flex shrink-0 items-center gap-1 rounded-lg border border-sky-200 px-2 py-1.5 text-xs text-sky-700 hover:bg-sky-50 disabled:border-slate-200 disabled:text-slate-400"
              title="按 OpenAI 协议请求 {Base URL}/models"
            >
              <Download className="h-3.5 w-3.5" />
              {fetching ? '拉取中…' : '获取模型列表'}
            </button>
          </div>
          {fetched && (
            <button
              type="button"
              onClick={() => {
                setFetched(null)
                setFetchHint(null)
              }}
              className="text-[11px] text-slate-400 underline hover:text-slate-600"
            >
              手动输入 Model ID
            </button>
          )}
          {fetchHint && (
            <div
              className={`text-[11px] ${fetched ? 'text-emerald-600' : 'text-amber-600'}`}
              role="status"
            >
              {fetchHint}
            </div>
          )}
          <input
            id="model-api-key"
            name="api_key"
            value={form.api_key}
            onChange={onChange('api_key')}
            disabled={streaming}
            type="password"
            placeholder="API Key（只用于本机调用，不回传到前端）"
            aria-label="API Key"
            autoComplete="off"
            className="w-full rounded-lg border border-slate-200 px-3 py-1.5 text-sm outline-none focus:border-sky-400"
          />
          <div className="grid grid-cols-2 gap-3">
            <label className="flex flex-col gap-1">
              <span className="text-[11px] text-slate-500">
                max_tokens（思考与正文共用）
              </span>
              <input
                name="max_tokens"
                value={form.max_tokens}
                onChange={onChange('max_tokens')}
                disabled={streaming}
                type="number"
                min={1}
                placeholder="8192"
                aria-label="max_tokens"
                className="w-full rounded-lg border border-slate-200 px-3 py-1.5 text-sm outline-none focus:border-sky-400"
              />
            </label>
            <label className="flex flex-col gap-1">
              <span className="text-[11px] text-slate-500">上下文窗口</span>
              <input
                name="context_window"
                value={form.context_window}
                onChange={onChange('context_window')}
                disabled={streaming}
                type="number"
                min={1024}
                placeholder="按模型名推断"
                aria-label="上下文窗口"
                className="w-full rounded-lg border border-slate-200 px-3 py-1.5 text-sm outline-none focus:border-sky-400"
              />
            </label>
          </div>
          <p className="text-[11px] leading-4 text-slate-400">
            推理模型的思考也走 max_tokens：填小了会出现「思考完就没正文」。
            常见 1M 窗口模型（deepseek-flash / deepseek-v4-pro 等）留空即可自动识别。
          </p>
        </div>

        {formError && <div className="mt-2 text-[11px] text-rose-600">{formError}</div>}

        <div className="mt-3 flex justify-end gap-2">
          <button
            type="button"
            onClick={onClose}
            className="rounded-lg border border-slate-200 px-3 py-1.5 text-sm text-slate-600 hover:bg-slate-50"
          >
            取消
          </button>
          <button
            type="button"
            onClick={() => void onSave()}
            disabled={saving || streaming}
            className="flex items-center gap-1 rounded-lg bg-sky-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-sky-700 disabled:bg-slate-300"
          >
            <Plus className="h-3.5 w-3.5" />
            {saving ? '保存中…' : '保存'}
          </button>
        </div>
      </div>
    </div>
  )
}
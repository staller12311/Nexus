import type { ApiMessage, ChatMessage, ToolCallView, Usage } from '../types'

/** agent 侧工具错误的统一前缀（agent/registry.py 的 ERROR_PREFIX）。 */
const ERROR_PREFIX = '错误：'

/** 后端 usage 可能是旧格式（没有 cache_hit_tokens），这里统一补齐默认值。 */
function normalizeUsage(raw: Usage): Usage {
  return {
    prompt_tokens: Number(raw.prompt_tokens) || 0,
    completion_tokens: Number(raw.completion_tokens) || 0,
    total_tokens: Number(raw.total_tokens) || 0,
    cache_hit_tokens: Number(raw.cache_hit_tokens) || 0,
  }
}

/**
 * 把后端返回的 OpenAI 原生消息序列，还原成 UI 用的「气泡 + 工具卡片」结构。
 *
 * 原生形态：`assistant(tool_calls=[...])` 紧跟若干条 `role="tool"`（按 tool_call_id 对应）。
 * 这里把 tool 消息**合并进**对应 assistant 消息的卡片里，所以 UI 层不再有独立的气泡。
 *
 * - `system` 消息直接丢弃（界面上不显示）
 * - 历史里的工具卡片默认 `success`；只有当结果文本以「错误：」开头时才标 `error`
 * - 找不到归属的孤儿 tool 消息忽略；始终没有结果的卡片标 `error`，避免永远转圈
 */
export function reconstructMessages(messages: ApiMessage[], idPrefix = 'h'): ChatMessage[] {
  const result: ChatMessage[] = []
  let index = 0

  for (const message of messages) {
    if (message.role === 'system') continue

    if (message.role === 'tool') {
      const owner = result.find((item) =>
        item.toolCalls?.some((call) => call.id === message.tool_call_id),
      )
      const call = owner?.toolCalls?.find((item) => item.id === message.tool_call_id)
      if (call) {
        const text = message.content ?? ''
        call.status = text.startsWith(ERROR_PREFIX) ? 'error' : 'success'
        call.content = text
      }
      continue
    }

    index += 1

    if (message.role === 'user') {
      result.push({
        id: `${idPrefix}-${index}`,
        role: 'user',
        content: message.content ?? '',
      })
      continue
    }

    if (message.role === 'assistant') {
      const toolCalls: ToolCallView[] = (message.tool_calls ?? []).map((call) => ({
        id: call.id,
        name: call.function.name,
        arguments: call.function.arguments,
        status: 'pending',
      }))
      const reasoning = (message.reasoning_content ?? '').trim()
      result.push({
        id: `${idPrefix}-${index}`,
        role: 'assistant',
        content: message.content ?? null,
        ...(toolCalls.length > 0 ? { toolCalls } : {}),
        // 历史里的思维链是已完成的（不是流式中），折叠面板默认收起
        ...(reasoning ? { reasoning, reasoningStreaming: false } : {}),
        // 单轮 usage（含缓存命中）：旧库/旧消息没有这一项
        ...(message.usage ? { usage: normalizeUsage(message.usage) } : {}),
      })
    }
  }

  for (const message of result) {
    for (const call of message.toolCalls ?? []) {
      if (call.status === 'pending') {
        call.status = 'error'
        call.content = '（没有拿到结果：可能被中断或失败）'
      }
    }
  }

  return result
}
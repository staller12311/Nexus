/**
 * 模型 id 的过滤与排序（纯函数，便于单测）。
 *
 * 背景：DashScope 之类的厂商在 `/v1/models` 里会返回 180+ 个模型，其中大量是
 * audio / image / tts / embedding / realtime 这类**不能用于纯文本对话**的模型，
 * 直接全列出来既难找又容易选错。
 */

/** 命中这些关键字的一律隐藏（大小写不敏感） */
export const EXCLUDED_KEYWORDS = [
  'audio',
  'image',
  'tts',
  'vision',
  'realtime',
  'embedding',
  'speech',
  'ocr',
] as const

/** 主流纯文本模型：按这个顺序排在前面 */
export const POPULAR_KEYWORDS = [
  'qwen-max',
  'qwen-plus',
  'qwen-turbo',
  'deepseek',
  'gpt',
  'kimi',
] as const

export function isTextModel(modelId: string): boolean {
  const lowered = modelId.toLowerCase()
  return !EXCLUDED_KEYWORDS.some((keyword) => lowered.includes(keyword))
}

/** 命中的主流关键字下标（越小越靠前）；不命中返回 -1 */
export function popularRank(modelId: string): number {
  const lowered = modelId.toLowerCase()
  return POPULAR_KEYWORDS.findIndex((keyword) => lowered.includes(keyword))
}

export type FilterResult = {
  /** 过滤 + 排序后的可见模型 */
  visible: string[]
  /** 因"非纯文本"被隐藏的数量 */
  hiddenCount: number
}

/**
 * 先按关键字过滤，再按「主流优先」排序，最后用搜索词做子串匹配。
 *
 * 排序规则：
 * 1. 命中 POPULAR_KEYWORDS 的排前面，组间按该数组顺序；
 * 2. 同一组内，「名字正好等于关键字」的（如 `qwen-max`）排在带后缀的（`qwen-max-1201`）前面；
 * 3. 其余保持上游返回的原顺序（sort 稳定）。
 */
export function filterModels(modelIds: string[], query = ''): FilterResult {
  const textModels = modelIds.filter(isTextModel)
  const hiddenCount = modelIds.length - textModels.length

  const score = (modelId: string): [number, number] => {
    const rank = popularRank(modelId)
    if (rank === -1) return [POPULAR_KEYWORDS.length, 1]
    const exact = modelId.toLowerCase() === POPULAR_KEYWORDS[rank] ? 0 : 1
    return [rank, exact]
  }

  const sorted = [...textModels].sort((a, b) => {
    const [rankA, exactA] = score(a)
    const [rankB, exactB] = score(b)
    return rankA - rankB || exactA - exactB
  })

  const keyword = query.trim().toLowerCase()
  const visible = keyword ? sorted.filter((id) => id.toLowerCase().includes(keyword)) : sorted
  return { visible, hiddenCount }
}
/**
 * Token 成本估算：把「缓存命中」换算成用户更直观的「省了多少钱」。
 *
 * 这是一个**粗略的默认单价**（元 / 千 token），只用于状态栏的静默提示：
 * 真实价格随厂商、模型、时段变化，这里不联网、不读配置，避免把展示逻辑复杂化。
 */
export const INPUT_PRICE_PER_1K_TOKENS = 0.001

/** 缓存命中省下的预估金额（元）= 命中 tokens ÷ 1000 × 单价；非法输入按 0 处理。 */
export function savedAmount(cacheHitTokens: number): number {
  const tokens = Number.isFinite(cacheHitTokens) ? Math.max(0, cacheHitTokens) : 0
  return (tokens / 1000) * INPUT_PRICE_PER_1K_TOKENS
}

/** 金额格式化：永远是 ¥0.00 形式，不出现 NaN / 负数。 */
export function formatMoney(amount: number): string {
  const safe = Number.isFinite(amount) ? Math.max(0, amount) : 0
  return `¥${safe.toFixed(2)}`
}

/** 0–1 比例 → 百分比；NaN、负数、超过 1 都夹到合法区间，不出现 NaN%。 */
export function formatRate(rate: number, digits = 1): string {
  const safe = Number.isFinite(rate) ? Math.min(1, Math.max(0, rate)) : 0
  return `${(safe * 100).toFixed(digits)}%`
}
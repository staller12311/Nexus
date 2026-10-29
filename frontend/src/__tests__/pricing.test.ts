import { describe, expect, it } from 'vitest'
import {
  formatMoney,
  formatRate,
  INPUT_PRICE_PER_1K_TOKENS,
  savedAmount,
} from '../lib/pricing'

describe('Token 成本估算', () => {
  it('省金额 = 命中 tokens ÷ 1000 × 默认单价', () => {
    expect(INPUT_PRICE_PER_1K_TOKENS).toBe(0.001)
    expect(savedAmount(282_600)).toBeCloseTo(0.2826, 6)
    expect(savedAmount(1000)).toBeCloseTo(0.001, 6)
    expect(savedAmount(0)).toBe(0)
  })

  it('非法输入一律按 0：不产生 NaN / 负数 / Infinity', () => {
    expect(savedAmount(Number.NaN)).toBe(0)
    expect(savedAmount(Number.POSITIVE_INFINITY)).toBe(0)
    expect(savedAmount(-500)).toBe(0)

    expect(formatMoney(Number.NaN)).toBe('¥0.00')
    expect(formatMoney(-1)).toBe('¥0.00')
    expect(formatMoney(0.2826)).toBe('¥0.28')
  })

  it('命中率夹在 0–100%，格式稳定', () => {
    expect(formatRate(Number.NaN)).toBe('0.0%')
    expect(formatRate(-0.2)).toBe('0.0%')
    expect(formatRate(0)).toBe('0.0%')
    expect(formatRate(0.942)).toBe('94.2%')
    expect(formatRate(1.5)).toBe('100.0%')
    expect(formatRate(0.05, 0)).toBe('5%')
  })
})
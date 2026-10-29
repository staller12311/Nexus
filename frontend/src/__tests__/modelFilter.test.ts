import { describe, expect, it } from 'vitest'
import { filterModels, isTextModel, popularRank } from '../lib/modelFilter'

// 取自真实 dashscope 返回的一小段（182 个里的代表项）
const REAL_IDS = [
  'qwen-audio-3.1-realtime-flash',
  'cmaas-qwen3.8-flash',
  'qwen-image-2.1-pro',
  'qwen3.8-omni-flash-realtime',
  'qwen-audio-3.1-realtime-plus',
  'qwen-mt-uni',
  'qwen3.8-omni-flash',
  'qwen3.8-livetranslate-flash-realtime',
  'qwen3.8-max-0902',
  'qwen3.7-text-embedding-flash',
  'qwen3.7-text-rerank',
  'qwen3.8-flash',
  'qwen-plus',
  'qwen-max',
  'qwen-turbo',
  'deepseek-chat',
  'gpt-4o-mini',
  'kimi-k2-0905-preview',
  'qwen-vl-ocr',
  'qwen-tts-v2',
  'speech-synthesis-x',
  'vision-pro-1',
]

describe('isTextModel', () => {
  it('隐藏 audio / image / tts / vision / realtime / embedding / speech / ocr', () => {
    for (const id of [
      'qwen-audio-3.1-realtime-flash',
      'qwen-image-2.1-pro',
      'qwen-tts-v2',
      'vision-pro-1',
      'qwen3.8-omni-flash-realtime',
      'qwen3.7-text-embedding-flash',
      'speech-synthesis-x',
      'qwen-vl-ocr',
    ]) {
      expect(isTextModel(id), id).toBe(false)
    }
  })

  it('保留纯文本模型（大小写不敏感）', () => {
    for (const id of ['qwen-plus', 'qwen3.8-flash', 'DeepSeek-Chat', 'gpt-4o-mini', 'qwen-mt-uni']) {
      expect(isTextModel(id), id).toBe(true)
    }
  })
})

describe('popularRank', () => {
  it('主流关键字命中顺序：qwen-max > qwen-plus > qwen-turbo > deepseek > gpt > kimi', () => {
    const order = ['qwen-max', 'qwen-plus', 'qwen-turbo', 'deepseek-chat', 'gpt-4o', 'kimi-k2']
    const ranks = order.map(popularRank)
    expect(ranks).toEqual([...ranks].sort((a, b) => a - b))
    expect(popularRank('qwen3.8-flash')).toBe(-1)
  })
})

describe('filterModels', () => {
  it('过滤掉非纯文本模型并统计隐藏数量', () => {
    const { visible, hiddenCount } = filterModels(REAL_IDS)
    expect(visible).not.toContain('qwen-audio-3.1-realtime-flash')
    expect(visible).not.toContain('qwen-image-2.1-pro')
    expect(visible).not.toContain('qwen-tts-v2')
    expect(visible).toContain('qwen3.8-flash')
    expect(hiddenCount).toBe(10) // 上面 22 个里 10 个非纯文本（22 - 12 = 10）
    expect(visible.length).toBe(REAL_IDS.length - hiddenCount)
  })

  it('主流模型排在最前，且按优先级顺序', () => {
    const { visible } = filterModels(REAL_IDS)
    expect(visible.slice(0, 3)).toEqual(['qwen-max', 'qwen-plus', 'qwen-turbo'])
    expect(visible[3]).toBe('deepseek-chat')
    expect(visible[4]).toBe('gpt-4o-mini')
    expect(visible[5]).toBe('kimi-k2-0905-preview')
    // 非主流模型保持上游原顺序
    expect(visible.slice(6)).toEqual([
      'cmaas-qwen3.8-flash',
      'qwen-mt-uni',
      'qwen3.8-omni-flash',
      'qwen3.8-max-0902',
      'qwen3.7-text-rerank',
      'qwen3.8-flash',
    ])
  })

  it('搜索词实时过滤（子串、大小写不敏感）', () => {
    // 过滤后仍保持「主流优先 + 其余按原顺序」
    expect(filterModels(REAL_IDS, 'qwen3.8').visible).toEqual([
      'cmaas-qwen3.8-flash',
      'qwen3.8-omni-flash',
      'qwen3.8-max-0902',
      'qwen3.8-flash',
    ])
    expect(filterModels(REAL_IDS, 'DEEPSEEK').visible).toEqual(['deepseek-chat'])
    expect(filterModels(REAL_IDS, 'zzz').visible).toEqual([])
    // 搜索也不能把非纯文本模型捞回来
    expect(filterModels(REAL_IDS, 'audio').visible).toEqual([])
    expect(filterModels(REAL_IDS, 'embedding').visible).toEqual([])
  })
})
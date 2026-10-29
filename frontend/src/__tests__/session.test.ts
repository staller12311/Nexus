import { describe, expect, it } from 'vitest'
import { displayTitle, formatRelativeTime } from '../lib/session'

describe('displayTitle', () => {
  it('后端有 title 时优先用 title', () => {
    expect(displayTitle({ id: 'a', title: '我的会话', first_user_message: '别的' })).toBe('我的会话')
  })

  it('title 为空时用第一条 user 消息，超过 15 字截断加省略号', () => {
    // 「读一下 readme.txt 的内容」的前 15 个字是「读一下 readme.txt 」，去掉尾部空格再补省略号
    expect(displayTitle({ id: 'a', title: '', first_user_message: '读一下 readme.txt 的内容' })).toBe(
      '读一下 readme.txt…',
    )
    expect(displayTitle({ id: 'a', title: '', first_user_message: '现在几点' })).toBe('现在几点')
  })

  it('恰好 15 字不加省略号', () => {
    const text = '一二三四五六七八九十一二三四五'
    expect(text).toHaveLength(15)
    expect(displayTitle({ id: 'a', title: '', first_user_message: text })).toBe(text)
  })

  it('没有 user 消息（或只有空白）显示「新会话」', () => {
    expect(displayTitle({ id: 'a', title: '', first_user_message: '' })).toBe('新会话')
    expect(displayTitle({ id: 'a', title: '   ', first_user_message: '   ' })).toBe('新会话')
    expect(displayTitle({ id: 'a', title: '' })).toBe('新会话')
  })

  it('把消息里的换行压成空格再截断', () => {
    expect(displayTitle({ id: 'a', title: '', first_user_message: '第一行\n第二行' })).toBe(
      '第一行 第二行',
    )
  })

  it('不再显示 UUID', () => {
    const session = { id: '932e80d8-1234-5678', title: '', first_user_message: '' }
    expect(displayTitle(session)).toBe('新会话')
    expect(displayTitle(session)).not.toContain('932e80d8')
  })
})

describe('formatRelativeTime', () => {
  const now = new Date('2026-10-27T17:20:00+08:00')

  it('一分钟内显示「刚刚」', () => {
    expect(formatRelativeTime('2026-10-27T09:19:30+00:00', now)).toBe('刚刚')
  })

  it('一小时内显示「N 分钟前」', () => {
    expect(formatRelativeTime('2026-10-27T09:15:00+00:00', now)).toBe('5 分钟前')
  })

  it('今天更早显示「今天 HH:MM」', () => {
    expect(formatRelativeTime('2026-10-27T01:08:00+00:00', now)).toBe('今天 09:08')
  })

  it('昨天显示「昨天 HH:MM」', () => {
    expect(formatRelativeTime('2026-10-26T01:08:00+00:00', now)).toBe('昨天 09:08')
  })

  it('更早显示「MM-DD HH:MM」，跨年带年份', () => {
    expect(formatRelativeTime('2026-10-20T01:08:00+00:00', now)).toBe('10-20 09:08')
    expect(formatRelativeTime('2025-12-31T01:08:00+00:00', now)).toBe('2025-12-31')
  })

  it('非法时间串返回空字符串，不抛异常', () => {
    expect(formatRelativeTime('不是时间', now)).toBe('')
  })
})
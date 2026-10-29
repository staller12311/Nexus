import type { SessionInfo } from '../types'

const TITLE_LIMIT = 15

/**
 * 侧边栏标题：后端 title 为空时，用会话里第一条 user 消息的前 15 个字符兜底；
 * 连 user 消息都没有就显示「新会话」。避免列表里出现一排 UUID。
 */
export function displayTitle(session: Pick<SessionInfo, 'id' | 'title' | 'first_user_message'>): string {
  const title = (session.title ?? '').trim()
  if (title) return title

  const first = (session.first_user_message ?? '').replace(/\s+/g, ' ').trim()
  if (!first) return '新会话'

  return first.length > TITLE_LIMIT ? `${first.slice(0, TITLE_LIMIT).trimEnd()}…` : first
}

const pad = (value: number) => String(value).padStart(2, '0')

/** 相对时间：刚刚 / N 分钟前 / 今天 HH:MM / 昨天 HH:MM / MM-DD HH:MM */
export function formatRelativeTime(iso: string, now: Date = new Date()): string {
  const time = new Date(iso)
  if (Number.isNaN(time.getTime())) return ''

  const diffMs = now.getTime() - time.getTime()
  if (diffMs < 60_000) return '刚刚'
  if (diffMs < 3_600_000) return `${Math.floor(diffMs / 60_000)} 分钟前`

  const clock = `${pad(time.getHours())}:${pad(time.getMinutes())}`
  const sameDay = (a: Date, b: Date) =>
    a.getFullYear() === b.getFullYear() && a.getMonth() === b.getMonth() && a.getDate() === b.getDate()

  if (sameDay(time, now)) return `今天 ${clock}`

  const yesterday = new Date(now)
  yesterday.setDate(now.getDate() - 1)
  if (sameDay(time, yesterday)) return `昨天 ${clock}`

  const date = `${pad(time.getMonth() + 1)}-${pad(time.getDate())}`
  return time.getFullYear() === now.getFullYear() ? `${date} ${clock}` : `${time.getFullYear()}-${date}`
}
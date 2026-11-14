const LOCALE = 'en-GB'

const MINUTE = 60
const HOUR = 60 * MINUTE
const DAY = 24 * HOUR

export function duration(seconds: number | null | undefined): string {
  if (seconds == null || !Number.isFinite(seconds) || seconds < 0) return '—'
  const total = Math.round(seconds)
  if (total < MINUTE) return `${total} s`
  if (total < HOUR) return `${Math.floor(total / MINUTE)} min`
  if (total < DAY) {
    const h = Math.floor(total / HOUR)
    const m = Math.floor((total % HOUR) / MINUTE)
    return m === 0 ? `${h} h` : `${h} h ${m} min`
  }
  const d = Math.floor(total / DAY)
  const h = Math.floor((total % DAY) / HOUR)
  return h === 0 ? `${d} d` : `${d} d ${h} h`
}

function parseStored(iso: string): Date | null {
  const d = new Date(iso.endsWith('Z') || iso.includes('+') ? iso : iso + 'Z')
  return Number.isNaN(d.getTime()) ? null : d
}

function clock(date: Date, timeZone?: string): string {
  return date.toLocaleTimeString(LOCALE, {
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
    ...(timeZone ? { timeZone } : {}),
  })
}

function calendarDay(date: Date, timeZone?: string): number {
  const parts = new Intl.DateTimeFormat('en-CA', {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    ...(timeZone ? { timeZone } : {}),
  }).formatToParts(date)
  const get = (type: string) => Number(parts.find((p) => p.type === type)?.value)
  return Date.UTC(get('year'), get('month') - 1, get('day'))
}

export function relative(
  iso: string | null | undefined,
  now: Date = new Date(),
  timeZone?: string,
): string {
  if (!iso) return '—'
  const d = parseStored(iso)
  if (!d) return '—'
  const seconds = Math.round((now.getTime() - d.getTime()) / 1000)
  if (seconds < 0) return absolute(iso, now, timeZone)
  const days = Math.round((calendarDay(now, timeZone) - calendarDay(d, timeZone)) / DAY)
  if (days === 0) {
    if (seconds < MINUTE) return 'just now'
    if (seconds < HOUR) return `${Math.floor(seconds / MINUTE)} min ago`
    return `${Math.floor(seconds / HOUR)} h ago`
  }
  if (days === 1) return `yesterday ${clock(d, timeZone)}`
  if (days < 7) return `${days} days ago`
  return d.toLocaleDateString(LOCALE, { day: 'numeric', month: 'short', timeZone })
}

export function absolute(
  iso: string | null | undefined,
  now: Date = new Date(),
  timeZone?: string,
): string {
  if (!iso) return '—'
  const d = parseStored(iso)
  if (!d) return '—'
  const sameYear =
    new Intl.DateTimeFormat('en-CA', { year: 'numeric', timeZone }).format(d) ===
    new Intl.DateTimeFormat('en-CA', { year: 'numeric', timeZone }).format(now)
  const date = d.toLocaleDateString(LOCALE, {
    day: 'numeric',
    month: 'short',
    ...(sameYear ? {} : { year: 'numeric' }),
    ...(timeZone ? { timeZone } : {}),
  })
  return `${date}, ${clock(d, timeZone)}`
}

export function bytes(n: number | null | undefined): string {
  if (n == null || !Number.isFinite(n) || n <= 0) return '—'
  const units = ['B', 'KiB', 'MiB', 'GiB', 'TiB']
  let value = n
  let unit = 0
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024
    unit += 1
  }
  return unit === 0 ? `${value} B` : `${value.toFixed(2)} ${units[unit]}`
}

export function percent(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value)) return '—'
  return `${value.toFixed(1)}%`
}

export function vmaf(score: number | null | undefined, target?: number | null): string {
  if (score == null) return '—'
  const rounded = Number(score.toFixed(1))
  if (target == null) return rounded.toFixed(1)
  if (score < target && rounded >= target) return (rounded - 0.1).toFixed(1)
  if (score > target && rounded <= target) return (rounded + 0.1).toFixed(1)
  return rounded.toFixed(1)
}

export function crf(value: number | null | undefined): string {
  return value == null ? '—' : String(value)
}

export function bitrate(kbps: number | null | undefined): string {
  if (kbps == null || !Number.isFinite(kbps) || kbps <= 0) return '—'
  return kbps >= 1000 ? `${(kbps / 1000).toFixed(1)} Mb/s` : `${Math.round(kbps)} kb/s`
}

export function eta(seconds: number | null | undefined): string {
  if (seconds == null || !Number.isFinite(seconds) || seconds <= 0) return ''
  return `about ${duration(seconds)} left`
}

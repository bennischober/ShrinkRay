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

function clock(date: Date): string {
  return date.toLocaleTimeString(LOCALE, {
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  })
}

function calendarDay(date: Date): number {
  return Date.UTC(date.getFullYear(), date.getMonth(), date.getDate())
}

export function relative(iso: string | null | undefined, now: Date = new Date()): string {
  if (!iso) return '—'
  const d = parseStored(iso)
  if (!d) return '—'
  const seconds = Math.round((now.getTime() - d.getTime()) / 1000)
  if (seconds < 0) return absolute(iso, now)
  const days = Math.round((calendarDay(now) - calendarDay(d)) / (DAY * 1000))
  if (days === 0) {
    if (seconds < MINUTE) return 'just now'
    if (seconds < HOUR) return `${Math.floor(seconds / MINUTE)} min ago`
    return `${Math.floor(seconds / HOUR)} h ago`
  }
  if (days === 1) return `yesterday ${clock(d)}`
  if (days < 7) return `${days} days ago`
  return d.toLocaleDateString(LOCALE, { day: 'numeric', month: 'short' })
}

export function absolute(iso: string | null | undefined, now: Date = new Date()): string {
  if (!iso) return '—'
  const d = parseStored(iso)
  if (!d) return '—'
  const sameYear = d.getFullYear() === now.getFullYear()
  const date = d.toLocaleDateString(LOCALE, {
    day: 'numeric',
    month: 'short',
    ...(sameYear ? {} : { year: 'numeric' }),
  })
  return `${date}, ${clock(d)}`
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
  return kbps >= 1000 ? `${(kbps / 1000).toFixed(1)} Mbps` : `${Math.round(kbps)} kb/s`
}

export function eta(seconds: number | null | undefined): string {
  if (seconds == null || !Number.isFinite(seconds) || seconds <= 0) return ''
  return `about ${duration(seconds)} left`
}

export function span(
  startIso: string | null | undefined,
  endIso: string | null | undefined,
): string {
  if (!startIso || !endIso) return '—'
  const start = parseStored(startIso)
  const end = parseStored(endIso)
  if (!start || !end) return '—'
  return duration(Math.max(0, Math.round((end.getTime() - start.getTime()) / 1000)))
}

export function savedPercent(value: number | null | undefined): {
  text: string
  grew: boolean
} {
  if (value == null || !Number.isFinite(value)) return { text: '—', grew: false }
  if (value === 0) return { text: '0.0%', grew: false }
  if (value < 0) return { text: `+${Math.abs(value).toFixed(1)}% larger`, grew: true }
  return { text: `${value.toFixed(1)}%`, grew: false }
}

const STATE_WORDS: Record<string, string> = {
  queued: 'Queued',
  running: 'Running',
  done: 'Done',
  partial: 'Partial',
  failed: 'Failed',
  cancelled: 'Cancelled',
  interrupted: 'Interrupted',
}

/** The screen word for a job state (DESIGN.md Glossary): one vocabulary,
 * sentence case, never the raw enum. An unknown state keeps its own word so
 * a newly added state is visible rather than blank. */
export function stateWord(state: string): string {
  return STATE_WORDS[state] ?? state
}

const KIND_WORDS: Record<string, string> = {
  scan: 'Library scan',
  plan: 'Plan',
  decision: 'Decision',
  'crf-search': 'CRF search',
  encode: 'Encode',
  legacy: 'Legacy',
}

/** The screen word for a job kind (DESIGN.md Glossary): the enum never
 * reaches the screen, and an unknown kind keeps its own word so a new one is
 * visible rather than blank. */
export function kindWord(kind: string): string {
  return KIND_WORDS[kind] ?? kind
}

const STAGE_WORDS: Record<string, string> = {
  scan: 'scanning',
  probe: 'probing',
  encode: 'encoding',
  paused: 'paused',
}

/** The step a multi-step job is on, in one voice (issue #631): an operator
 * can tell a CRF probe from the final encode. An unknown stage passes
 * through rather than disappearing, so a new server stage shows up as itself. */
export function stageWord(stage: string | null | undefined): string {
  if (!stage) return ''
  return STAGE_WORDS[stage] ?? stage
}

/** What a running job's progress bar says (issue #631): the step it is
 * on, the file, and what the engine reported — never a bare percentage
 * with no explanation of what is being measured. Falls back to a plain
 * state word only when the payload carries nothing else. */
export function progressLabel(
  stage: string | null | undefined,
  movie: string | null | undefined,
  detail: string | null | undefined,
  percent: number | null | undefined,
): string {
  const rest = [movie, detail].filter(Boolean)
  // the engine's own detail sometimes names the step already
  // ("scanning library", "probing a.mkv"): never say it twice
  const saysStepAlready = (detail ?? '').toLowerCase().startsWith(stageWord(stage))
  const parts = stageWord(stage) && !saysStepAlready ? [stageWord(stage), ...rest] : rest
  if (parts.length > 0) return parts.join(' · ')
  return percent != null ? 'working' : 'starting…'
}

/** The three questions the jobs page answers (issue #632) — what is
 * running now, what comes next, what happened — as three groups rather
 * than one id-ordered list, so the running job and the next one can never
 * scroll out of view behind a backlog. "Next" is the oldest queued job, the
 * one the worker will pick up first. */
export function groupJobs<T extends { id: number | null; state: string }>(
  jobs: readonly T[],
): { running: T[]; next: T | null; history: T[] } {
  const running = jobs.filter((j) => j.state === 'running')
  const queued = jobs
    .filter((j) => j.state === 'queued' && j.id !== null)
    .sort((a, b) => (a.id as number) - (b.id as number))
  return {
    running,
    next: queued[0] ?? null,
    history: jobs.filter((j) => j.state !== 'running' && j.state !== 'queued'),
  }
}

const VERDICT_WORDS: Record<string, string> = {
  encode: 'Encode',
  skip: 'Skip',
  failed: 'Failed',
}

/** The screen word for a plan's verdict on one file. */
export function verdictWord(verdict: string): string {
  return VERDICT_WORDS[verdict] ?? verdict
}

/** What a rail count counts (issue #630): the number alone says nothing, so
 * it carries its own words. One job is "1 job", not "1 jobs". */
export function queueLabel(count: number): string {
  return `${count} job${count === 1 ? '' : 's'} in the queue`
}

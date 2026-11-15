import type { JobOutcome } from './api'

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

/** The wall time a step has left, from the fraction of its work it has finished
 * and how long IT has been running: the engine sends no ETA, so a proportional
 * estimate is the only honest one. `elapsed` must be the STEP's own — a probe's, not
 * the job's — or a fresh probe divides the job's elapsed by a tiny fraction and
 * swings the estimate by an order of magnitude (issue #690 review). Null when there
 * is no usable fraction or elapsed time, so a row says `…` rather than inventing a
 * number. */
export function remainingSeconds(
  elapsed: number | null | undefined,
  fraction: number | null | undefined,
): number | null {
  if (elapsed == null || !(elapsed > 0)) return null
  if (fraction == null || !(fraction > 0) || fraction >= 1) return null
  const total = elapsed / fraction
  return Math.max(0, total - elapsed)
}

/** Seconds a running job has been running, from its start timestamp. Null when
 * the timestamp is missing or unparseable. */
export function elapsedSeconds(
  startedIso: string | null | undefined,
  now: Date = new Date(),
): number | null {
  if (!startedIso) return null
  const iso =
    startedIso.endsWith('Z') || startedIso.includes('+') ? startedIso : `${startedIso}Z`
  const elapsed = (now.getTime() - Date.parse(iso)) / 1000
  if (!Number.isFinite(elapsed) || elapsed <= 0) return null
  return elapsed
}

/** The same estimate for a table cell, where the column header already says
 * what it is: `~9 h 56 min`, never `about ... left`. */
export function etaShort(seconds: number | null | undefined): string {
  if (seconds == null || !Number.isFinite(seconds) || seconds <= 0) return ''
  return `~${duration(seconds)}`
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
 * scroll out of view behind a backlog. "Next" is the oldest queued job; every
 * queued job is in `queued`, so ordering the page can never drop one. */
export function groupJobs<T extends { id: number | null; state: string }>(
  jobs: readonly T[],
): { running: T[]; queued: T[]; next: T | null; history: T[] } {
  const running = jobs.filter((j) => j.state === 'running')
  const queued = jobs
    .filter((j) => j.state === 'queued')
    .sort((a, b) => (a.id ?? 0) - (b.id ?? 0))
  return {
    running,
    queued,
    next: queued[0] ?? null,
    history: jobs.filter((j) => j.state !== 'running' && j.state !== 'queued'),
  }
}

/** What a finished job did, in one line (issue #632): the operator reads
 * the result instead of clicking DETAIL and parsing raw JSON. Empty when the
 * payload carries no per-movie outcomes (scans, legacy rows). */
export function resultSummary(outcomes: readonly JobOutcome[]): string {
  if (outcomes.length === 0) return ''
  const encoded = outcomes.filter((o) => o.state === 'encoded')
  const skipped = outcomes.filter((o) => o.state === 'skipped' && !isFailedOutcome(o))
  const already = outcomes.filter((o) => o.state === 'already_encoded')
  const failed = outcomes.filter(isFailedOutcome)
  const parts: string[] = []
  if (encoded.length > 0) parts.push(`${encoded.length} encoded`)
  // the saving comes from the sizes the server actually writes on an encode
  // outcome (input_size − output_size), not from `saved_percent`, which only
  // plan entries carry
  const savings = encoded
    .filter((o) => typeof o.input_size === 'number' && typeof o.output_size === 'number')
    .map((o) => ((o.input_size as number) - (o.output_size as number)) / (o.input_size as number) * 100)
  if (savings.length > 0) {
    const avg = savings.reduce((sum, p) => sum + p, 0) / savings.length
    parts.push(`${savedPercent(avg).text} size`)
  }
  if (skipped.length > 0) parts.push(`${skipped.length} skipped`)
  if (already.length > 0) parts.push(`${already.length} already encoded`)
  if (failed.length > 0) parts.push(`${failed.length} failed`)
  return parts.join(' · ')
}

/** A job outcome that is work lost, not work done: a failed movie, or a
 * swap the operator declined — which the server reports as `skipped` plus a
 * decline error (`src/shrinkray/server/jobs.py`). A busy or HDR skip is
 * neither. One definition, so no surface can disagree with another. */
export function isFailedOutcome(o: JobOutcome): boolean {
  return (
    o.state === 'failed' ||
    (o.state === 'skipped' && (o.error ?? '').includes('declined'))
  )
}

/** Which of the time columns a row has (issue #632): a queued row has
 * no start and no duration, a running row no duration, so the row must not
 * print a column it cannot fill. Read from the timestamps themselves, not
 * from a table of states, so a state the UI has not heard of (a cancelled
 * job keeps both) still shows what it has. */
export function timeColumns(job: {
  started_at?: string | null
  finished_at?: string | null
}): { started: boolean; duration: boolean } {
  return {
    started: job.started_at != null,
    duration: job.started_at != null && job.finished_at != null,
  }
}

/** How a library's titles divide, so the band adds up (issue #633): the
 * issue found `1 encoded · 4 untouched` against 7 titles with no
 * explanation of the other two. Every title is in exactly one bucket, so
 * the four always sum to `total`. */
export function titleBuckets(
  movies: readonly {
    encode?: { outcome?: string | null; preset?: string | null } | null
  }[],
): {
  encoded: number
  discarded: number
  skipped: number
  untouched: number
  total: number
} {
  let encoded = 0
  let discarded = 0
  let skipped = 0
  let untouched = 0
  for (const m of movies) {
    if (m.encode?.outcome === 'discarded') discarded++
    else if (m.encode && (m.encode.preset ?? '').startsWith('skipped')) skipped++
    else if (m.encode) encoded++
    else untouched++
  }
  return { encoded, discarded, skipped, untouched, total: movies.length }
}

/** The one line the Overview answers "is everything OK?" with, first on the
 * page (issue #633): the state, what it is doing, how long is left and how
 * much is waiting — never the poll mode, the database path or a count of
 * sources. The operator reads one sentence and knows whether to act. */
export function statusAnswer(input: {
  running: { movie?: string | null } | null
  etaSeconds: number | null
  queued: number
  watching: boolean
}): string {
  if (input.running) {
    const parts = [`encoding ${input.running.movie ?? 'a file'}`]
    const left = eta(input.etaSeconds)
    if (left) parts.push(left)
    let line = `● Working — ${parts.join(', ')}`
    if (input.queued > 0) {
      line += ` · ${input.queued} file${input.queued === 1 ? '' : 's'} waiting`
    }
    return line
  }
  const tail = input.watching ? 'watching for new files' : 'the watcher is off'
  return `● Idle — all caught up · ${tail}`
}

/** A per-file stage row is the running folder job's own work, not a job of
 * its own (issue #602): the folder job stays the runner and writes these rows
 * so the page can follow each stage. Showing both at the top level is how one
 * CRF search looked like two running jobs. */
export function isStageRow(job: { payload?: unknown }): boolean {
  const p = job.payload as { pipeline?: unknown } | null
  return typeof p?.pipeline === 'string'
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

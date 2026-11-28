import type { JobOutcome, Movie } from './api'

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

/** A clock-string duration as the engine stores it ("02:41:41") in
 * seconds, so the one `duration` voice covers stored clock times too;
 * null when the string is not a clock time. */
export function durationFromClock(raw: string | null | undefined): number | null {
  if (!raw) return null
  const parts = raw.split(':').map(Number)
  if (parts.length !== 3 || parts.some(Number.isNaN)) return null
  return parts[0] * 3600 + parts[1] * 60 + parts[2]
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

/** A frame rate as the engine reports it: one decimal, always labelled. */
export function fps(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value)) return '—'
  return `${value.toFixed(1)} fps`
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

/** `finishes ≈ HH:MM` when more than 10 min are left (issue #631): the
 * wall time the operator can plan around, next to `about X left`. */
export function finishesAt(seconds: number | null | undefined, now: number): string {
  if (seconds == null || !Number.isFinite(seconds) || seconds <= 600) return ''
  const at = new Date(now + seconds * 1000)
  return `finishes ≈ ${clock(at)}`
}

/** A stored measurement time as the reused wording shows it: `14 Nov 08:12`.
 * SQLite writes `YYYY-MM-DD HH:MM:SS` in UTC; an already-ISO value is accepted. */
export function measuredAtLabel(value: string | null | undefined): string {
  if (!value) return 'unknown time'
  const iso = value.includes('T') ? value : `${value.replace(' ', 'T')}Z`
  const at = new Date(iso)
  if (Number.isNaN(at.getTime())) return value
  return at
    .toLocaleString('en-GB', {
      day: '2-digit',
      month: 'short',
      hour: '2-digit',
      minute: '2-digit',
      hour12: false,
    })
    .replace(',', '')
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

/** A signed percent delta in the console's diff form (DESIGN.md's Signed
 * Delta Rule): `−41.2% size`, `+3.8% vs encode` — the sign leads so the
 * polarity reads without parsing, and `good` marks a favorable delta (a
 * negative one: smaller is the point). `unit` names what changed. */
export function signedPercent(
  value: number | null | undefined,
  unit: string,
): { text: string; good: boolean } {
  if (value == null || !Number.isFinite(value)) return { text: '—', good: false }
  return {
    text: `${value <= 0 ? '−' : '+'}${Math.abs(value).toFixed(1)}${unit}`,
    good: value <= 0,
  }
}

/** The bytes a title reclaimed for the dashboard (issue #646): the server's
 * per-title library delta plus output-directory savings. The server applies #625
 * per row (a discarded attempt reclaimed nothing) and both halves of #647 (a
 * missing file, or a restored original, has nothing on disk), and clamps every
 * term at 0. The client keeps only the missing-file guard for what remains
 * client-side; it must never zero the whole title on a discarded latest row,
 * because an earlier corroborated chain delta is still real. */
export function reclaimedBytes(movie: Movie): number {
  if (movie.missing) return 0
  return Math.max(movie.reclaimed_bytes ?? 0, 0)
}

/** The dashboard's total reclaimed bytes (issue #646): the one sum the
 * RECLAIMED stat shows, over the same per-title values the category bars
 * group. */
export function reclaimedTotalBytes(movies: Movie[]): number {
  return movies.reduce((sum, movie) => sum + reclaimedBytes(movie), 0)
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

const REASON_WORDS: Record<string, string> = {
  'hdr not supported': 'HDR — not supported yet',
  'too short': 'Too short to be worth it',
  'resolution too low': 'Resolution below 1080p class',
  'below min size': 'Below the size floor',
  'already AV1 (the output format)': 'Already AV1 — the output format',
  'source codec not a candidate': 'Source codec not a candidate',
  'no size gain': 'No size gain',
  'target unreachable': 'Quality target unreachable',
  'saving below minimum': 'Predicted saving below the minimum',
  // not in DESIGN.md's glossary table — plain operator wording
  'output not smaller': 'Output not smaller',
}

/** The screen wording for an engine skip/outcome reason (DESIGN.md
 * Glossary): the engine's strings never reach the operator. The
 * completeness witness's reasons (watch.py's "not complete: …" /
 * "appears incomplete: …") are already operator sentences, and an
 * unknown reason keeps its own wording — visible, never blank. */
export function reasonText(reason: string): string {
  return REASON_WORDS[reason] ?? reason
}

/** How many files are waiting in the queue (issue #631): one queued job can
 * name several paths, and a job with no paths still names one unit of work.
 * The running job is not counted. */
export function queuedFileCount(
  jobs: readonly { state: string; payload?: { paths?: string[]; path?: string } }[],
): number {
  return jobs
    .filter((job) => job.state === 'queued')
    .reduce((sum, job) => {
      const paths = job.payload?.paths?.length ?? 0
      return sum + Math.max(1, paths)
    }, 0)
}

/** A rough queue time from the last real encodes (issue #631): null without
 * history. `recent` is the server's encodes ordered by recency (newest first);
 * the estimate is the mean measured encode time of those rows times the files
 * waiting — the size term algebraically cancels, so it is not carried. The library
 * list is category/name-ordered, so it is never the pace source (a verifier probe
 * measured 2047.8 s from an alphabetical window whose newest five real encodes
 * averaged 60 s). */
export const QUEUE_PACE_SAMPLE = 5
export function queuePaceSeconds(
  files: number,
  recent: readonly { encode_seconds: number; input_size: number }[],
): number | null {
  if (files <= 0) return 0
  const sample = recent
    .slice(0, QUEUE_PACE_SAMPLE)
    .filter((row) => row.encode_seconds > 0 && row.input_size > 0)
  if (sample.length === 0) return null
  const meanSeconds =
    sample.reduce((sum, row) => sum + row.encode_seconds, 0) / sample.length
  return files * meanSeconds
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
 * payload carries no per-movie outcomes (scans, legacy rows). Plan entries use
 * their own stored states (`encode` / `skip` / `error`, issue #685), so the plan
 * buckets below key on those states, never on the display words. */
export function resultSummary(outcomes: readonly JobOutcome[]): string {
  if (outcomes.length === 0) return ''
  const encoded = outcomes.filter((o) => o.state === 'encoded')
  const wouldEncode = outcomes.filter((o) => o.state === 'encode')
  const skipped = outcomes.filter((o) => o.state === 'skipped' && !isFailedOutcome(o))
  const already = outcomes.filter((o) => o.state === 'already_encoded')
  const efficient = outcomes.filter((o) => o.state === 'skip')
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
  if (wouldEncode.length > 0) parts.push(`${wouldEncode.length} would encode`)
  if (skipped.length > 0) parts.push(`${skipped.length} skipped`)
  if (already.length > 0) parts.push(`${already.length} already encoded`)
  if (efficient.length > 0) parts.push(`${efficient.length} already efficient`)
  if (failed.length > 0) parts.push(`${failed.length} failed`)
  return parts.join(' · ')
}

/** A job outcome that is work lost, not work done: a failed movie (an
 * encode outcome's `failed`, a plan entry's `error`), or a swap the operator
 * declined — which the server reports as `skipped` plus a decline error
 * (`src/shrinkray/server/jobs.py`). A busy or HDR skip is neither. One
 * definition, so no surface can disagree with another. */
export function isFailedOutcome(o: JobOutcome): boolean {
  return (
    o.state === 'failed' ||
    o.state === 'error' ||
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
 * page (issues #633, #679): the state, what it is doing, how long is left and
 * what is waiting — never the poll mode, the database path or a count of
 * sources. The operator reads one sentence and knows whether to act.
 *
 * `needYou` counts the jobs holding lost work (a failure, or a swap the
 * operator declined — issue #665); `failed` counts those that failed outright.
 * A failure is named in both the working and the idle branch, and
 * `snapshotPresent: false` (loading or stale) makes no claim about the queue
 * or the watcher — an all-clear is only drawn from a snapshot that is here. */
export function statusAnswer(input: {
  running: { movie?: string | null } | null
  etaSeconds: number | null
  queued: number
  needYou: number
  failed: number
  watching: boolean
  snapshotPresent: boolean
}): string {
  const failure =
    input.needYou > 0
      ? input.failed > 0
        ? `${input.needYou} need you — ${input.failed} failed`
        : `${input.needYou} need you`
      : ''
  if (input.running) {
    const parts = [`encoding ${input.running.movie ?? 'a file'}`]
    const left = eta(input.etaSeconds)
    if (left) parts.push(left)
    let line = `● Working — ${parts.join(', ')}`
    if (failure) line += ` · ${failure}`
    if (input.queued > 0) {
      line += ` · ${input.queued} file${input.queued === 1 ? '' : 's'} waiting`
    }
    return line
  }
  if (!input.snapshotPresent) {
    return failure ? `● Connecting… · ${failure}` : '● Connecting…'
  }
  if (failure) {
    const tail = input.watching ? 'the watcher is watching' : 'the watcher is off'
    return `● ${failure} · ${tail}`
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

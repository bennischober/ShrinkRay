/** Typed client for the resident app's API. */

export type JobState = 'queued' | 'running' | 'done' | 'failed' | 'cancelled'
export type JobKind = 'scan' | 'plan' | 'encode' | 'legacy'

export interface Job {
  id: number | null
  kind: JobKind
  payload: Record<string, unknown>
  state: JobState
  result: Record<string, unknown> | null
  error: string | null
  created_at?: string | null
  started_at?: string | null
  finished_at?: string | null
}

/** Extract the movie targets a job operated on (multi-directory aware).
 * The payload form the UI submits is `path` = first target plus `paths`
 * = the remaining targets, so both keys union — preferring `paths` dropped
 * the first target of every multi-target batch (issue #100: the per-movie
 * history filter showed that movie "no jobs"). */
export function jobPaths(job: Job): string[] {
  const payload = job.payload as { paths?: string[]; path?: string }
  const resolved: string[] = []
  const seen = new Set<string>()
  for (const value of [payload.path, ...(payload.paths ?? [])]) {
    if (!value) continue
    const key = value.replace(/[\\/]+/g, '/').replace(/\/$/, '').toLowerCase()
    if (seen.has(key)) continue
    seen.add(key)
    resolved.push(value)
  }
  return resolved
}

/** One per-movie entry inside a job's result payload (encode `outcomes`,
 * plan `plans`). The detail trail reads these to show THIS movie's
 * outcome inside a batch job — the job-level state alone hides it. */
export interface JobOutcome {
  movie: string
  state: string
  error?: string
  crf?: number | null
  vmaf?: number | null
  estimated_size?: number | null
  saved_percent?: number | null
  reason?: string
}

/** The movie's own entry in a job's per-movie result list (null when the
 * job didn't touch this movie individually — scans, legacy rows). */
export function movieOutcome(job: Job, movieName: string): JobOutcome | null {
  if (job.kind !== 'encode' && job.kind !== 'plan') return null
  const r = job.result as { outcomes?: JobOutcome[]; plans?: JobOutcome[] } | null
  if (!r) return null
  const list = job.kind === 'encode' ? r.outcomes : r.plans
  if (!Array.isArray(list)) return null
  return list.find((o) => o.movie === movieName) ?? null
}

/** A DONE job whose per-movie outcomes failed is not a clean done: the
 * lifecycle completed, but the work did not. Shared by the jobs page and
 * the detail trail — two audit trails must speak one language. */
export function failedOutcomeNote(job: Job): string {
  if (job.state !== 'done' || job.kind !== 'encode') return ''
  const r = job.result as { outcomes?: JobOutcome[] } | null
  if (!r?.outcomes) return ''
  const failed = r.outcomes.filter((o) => o.state === 'failed')
  if (failed.length === 0) return ''
  return `__FAILED_OUTCOMES__${failed.length} of ${r.outcomes.length} failed: ${failed
    .map((o) => o.movie)
    .join(', ')}`
}

export interface EncodeInfo {
  preset: string | null
  crf: number | null
  vmaf: number | null
  input_size: number | null
  output_size: number | null
  saved_percent: number | null
  /** schema v5 benchmark metrics */
  encoder_preset?: string | null
  /** schema v11: the PROBE's speed preset (measurement, issue #22) */
  probe_encoder_preset?: string | null
  encode_seconds?: number | null
  probe_count?: number | null
  probe_seconds?: number | null
  tool_versions?: string | null
  /** schema v7 drift fields (null for pre-v7 rows); present in the
   * library list, snapshot and /api/movie alike since 3f77d08 */
  generation?: number | null
  input_bitrate?: number | null
  output_bitrate?: number | null
}

/** One historical encode of a movie (GET /api/movie history entry).
 * The schema-v7 fields (generation, bitrates, chain links) make the row
 * a provenance-chain entry; bitrates are None for pre-v7 rows. */
export interface EncodeRecord extends EncodeInfo {
  /** the encodes row's primary key — the only safe each-key (timestamps
   * collide when two runs finish in the same second) */
  id?: number
  encoded_at: string | null
  input_bitrate?: number | null
  output_bitrate?: number | null
  generation?: number
  source_file?: string | null
  output_file?: string | null
  /** the library file this row belongs to — chain rows may belong to the
   * parent encode's file, not the viewed one (schema v7 chain) */
  file_path?: string | null
  file_name?: string | null
}

export interface MovieDetail {
  file: Omit<Movie, 'encode'> & { generation?: number }
  history: EncodeRecord[]
  /** the stored plan ("quicksave"): the prediction display's data —
   * what a future encode would do with this file. `stale` = the file's
   * mtime/size changed since the plan was probed. */
  plan?: MoviePlan | null
}

/** One stored plan decision (GET /api/movie `plan` key). */
export interface MoviePlan {
  crf: number | null
  achieved_vmaf: number | null
  skipped_reason: string | null
  estimated_size: number | null
  created_at: string | null
  encoder: string | null
  encoder_preset: string | null
  /** the plan was probed at this preset (a measurement setting, issue #22) */
  probe_encoder_preset?: string | null
  target_vmaf: number | null
  stale: boolean
  /** the HDR estimate bias note (issue #98): present only for HDR titles with
   * a live estimate — the prediction comes from one easy-content window */
  estimate_note?: string | null
}

export interface Movie {
  path: string
  name: string
  category: string
  duration: string | null
  codec: string | null
  resolution: [number, number] | null
  size: number | null
  hdr: boolean
  missing?: boolean
  missing_since?: string | null
  encode: EncodeInfo | null
}

export interface RetainedFile {
  source_path: string
  retained_path: string
  size_before: number
  size_after: number
  created_at: string
  /** false when the operator already deleted it: the row stays as the
   * record of what was swapped, the file itself is their business. */
  present: boolean
}

export interface Status {
  queue: Job[]
  watcher: 'running' | 'stopped'
  database: string
  /** the resident app's own memory (MiB), self-measured every 5 min —
   * staying lean is a measured fact (4534e07); null when unmeasurable */
  rss_mib?: number | null
}

export interface Config {
  codec?: { hdr?: { color_space?: string[]; color_transfer?: string[] } }
  file?: { extensions?: string[]; output?: { seperator?: string } }
  ffmpeg?: { black_bars?: { MIN_WIDTH?: number; MIN_HEIGHT?: number } }
  handbrake?: { preset?: string; encoder?: string; max_threads?: number }
  resources?: { idle_priority?: boolean }
  language?: string
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    ...init,
    headers: { 'Content-Type': 'application/json', ...authHeaders(), ...(init?.headers ?? {}) },
  })
  if (!response.ok) {
    const detail = await response.text().catch(() => response.statusText)
    throw new Error(humanizeError(response.status, detail))
  }
  return response.json() as Promise<T>
}

/** The dashboard's bearer token (issue #36).
 *
 * Auth is opt-in on the server: with $SHRINKRAY_TOKEN set, every route
 * requires the token, and the operator pastes it once — it is kept in
 * localStorage so the prompt appears once per browser, not per request.
 * With no token stored the header is omitted entirely, which is the
 * loopback development flow (the server does not ask for one). */
const TOKEN_KEY = 'shrinkray.token'

export function storedToken(): string | null {
  try {
    return localStorage.getItem(TOKEN_KEY)
  } catch {
    return null // private mode / storage disabled: fall back to no auth
  }
}

export function storeToken(token: string | null): void {
  try {
    if (token) localStorage.setItem(TOKEN_KEY, token)
    else localStorage.removeItem(TOKEN_KEY)
  } catch {
    // storage unavailable: the token lives for this page view only
  }
}

function authHeaders(): Record<string, string> {
  const token = storedToken()
  return token ? { Authorization: `Bearer ${token}` } : {}
}

/** True when a 401 came back: the UI shows the token prompt instead of an
 * error strip, because a missing token is not a failure to report as one. */
export function isAuthFailure(error: unknown): boolean {
  return error instanceof Error && error.message.includes('401')
}

/** Raw error text → operator language. The audit trail keeps the raw
 * exception (diagnosis needs it); the surfaces that INTERRUPT (the red
 * strip, the preflight) translate known cases into what happened and what
 * to do. Unknown text passes through untouched — never invented. */
const ERROR_TRANSLATIONS: [RegExp, (m: RegExpMatchArray) => string][] = [  [
    /API (\d+): [\s\S]*?"detail"\s*:\s*"((?:[^"\\]|\\.)*)"/,
    (m) => `${m[1]} — ${m[2].replace(/\\(.)/g, '$1')}`,
  ],
  [/API 503:/, () => 'the app is still starting — try again in a moment'],
  [
    /'(\w+)' object has no attribute '(\w+)'/,
    (m) => `a stored record is missing a field (${m[2]}) — likely a pre-v7 database row; rescan the title`,
  ],
  [
    /\[Errno 22\] Invalid argument/,
    () => 'a file could not be read (invalid path or unreadable source) — check the title in FOLDER',
  ],
  [
    /\[Errno 2\] No such file or directory: '([^']+)'/,
    (m) => `file not found: ${m[1].split(/[\\/]/).pop()} — rescan the library`,
  ],
  [
    /\[WinError 32\] The process cannot access the file/,
    () => 'the file is locked by another program — close it and retry',
  ],
  [
    /HTTPConnectionPool|ReadTimeout|ConnectionError/,
    () => 'an external tool did not answer in time — retry; if it persists, check the tool install',
  ],
  [
    /interrupted by app restart/,
    () => 'the run was interrupted by an app restart — RETRY resumes it',
  ],
]

export function humanizeError(status: number | null, raw: string): string {
  const text = stripErrorTelemetry(raw)
  for (const [pattern, say] of ERROR_TRANSLATIONS) {
    const m = text.match(pattern)
    if (m) return say(m)
  }
  if (status != null) return `API ${status}: ${text}`
  return text
}

/** Strips the noise that is not a diagnosis: ffmpeg/HandBrake banner
 * telemetry ("[11:59:15] Compile-time hardening features are enabled")
 * used to pass through and print AS the diagnosis (round-6 P2). Leading
 * `[HH:MM:SS]` timestamps are removed; known banner lines dropped; when
 * several lines remain, the last one wins (exceptions print last). */
export function stripErrorTelemetry(raw: string): string {
  const lines = raw
    .split(/[\r\n]+/)
    .map((l) => l.replace(/^\[\d{1,2}:\d{2}:\d{2}(\.\d+)?\]\s*/, '').trim())
    .filter(
      (l) =>
        l.length > 0 &&
        !/compile-time hardening/i.test(l) &&
        !/^ffmpeg version /i.test(l) &&
        !/^built with /i.test(l) &&
        !/^configuration: /i.test(l) &&
        !/^libav|^\s+libav/i.test(l) &&
        !/^handbrake/i.test(l) &&
        !/^press \[q\]/i.test(l)
    )
  if (lines.length === 0) return raw.trim()
  return lines[lines.length - 1]
}

/** The raw exception for surfaces that DIAGNOSE (the jobs DETAIL JSON) —
 * deliberately not humanized; translation is for the interrupting strip,
 * the raw text is for the operator reading a log. */
export function rawErrorText(raw: string): string {
  return raw.trim()
}

/** Known collection ETags: kept across polls, sent as If-None-Match (a
 * comma list — standard syntax); an unchanged collection comes back as
 * null and the client keeps its copy (the poll becomes nearly free). */
let libraryEtag: string | null = null
let jobsEtag: string | null = null

export interface Snapshot {
  status: Status
  /** null when unchanged since the last poll (client keeps its copy). */
  jobs: Job[] | null
  jobs_etag: string
  /** null when the library is unchanged (client keeps its copy). */
  library: Movie[] | null
  library_etag: string
}

export const api = {
  /** One round-trip for the whole poll: status + jobs + library, both
   * collections ETag-cached. */
  snapshot: async (): Promise<Snapshot> => {
    const tags = [libraryEtag, jobsEtag].filter((t) => t != null).join(', ')
    const response = await fetch('/api/snapshot', {
      headers: { ...authHeaders(), ...(tags ? { 'If-None-Match': tags } : {}) },
    })
    if (!response.ok) throw new Error(`API ${response.status}: snapshot`)
    const snap = (await response.json()) as Snapshot
    libraryEtag = snap.library_etag
    jobsEtag = snap.jobs_etag
    return snap
  },
  status: () => request<Status>('/api/status'),
  /** Fetches the library with ETag caching: returns null on 304 (unchanged). */
  libraryChanged: async (): Promise<Movie[] | null> => {
    const response = await fetch('/api/library', {
      headers: { ...authHeaders(), ...(libraryEtag ? { 'If-None-Match': libraryEtag } : {}) },
    })
    if (response.status === 304) return null
    if (!response.ok) throw new Error(`API ${response.status}: library`)
    libraryEtag = response.headers.get('ETag')
    return (await response.json()) as Movie[]
  },
  library: () => request<Movie[]>('/api/library'),
  /** The originals a swap retained (issue #35): what the operator may
   * delete themselves, in their own file manager. */
  retained: () => request<RetainedFile[]>('/api/retained'),
  jobs: (limit = 50) => request<Job[]>(`/api/jobs?limit=${limit}`),
  job: (id: number) => request<Job>(`/api/jobs/${id}`),
  /** One movie's full record: file facts + the COMPLETE encode history. */
  movie: (path: string) => {
    const params = new URLSearchParams({ path })
    return request<MovieDetail>(`/api/movie?${params}`)
  },
  config: () => request<Config>('/api/config'),
  reveal: (path: string) =>
    request<{ revealed: string }>('/api/reveal', { method: 'POST', body: JSON.stringify({ path }) }),
}

/** Formats a byte count as a GB string ('—' for empty). */
export function fmtGb(bytes: number | null | undefined): string {
  if (!bytes || bytes <= 0) return '—'
  return `${(bytes / 1024 ** 3).toFixed(2)} GB`
}

/** Formats a saved percentage ('—' for none, +X% for growth). */
/** Formats an ISO timestamp as a short "when" (time today, date+time before).
 * The backend stores UTC ('datetime now' in sqlite is UTC); naive strings
 * get Z appended here at the API boundary — the one place that decides. */
export function fmtWhen(iso: string | null | undefined): string {
  if (!iso) return '—'
  const d = new Date(iso.endsWith('Z') || iso.includes('+') ? iso : iso + 'Z')
  if (Number.isNaN(d.getTime())) return '—'
  const time = d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
  if (d.toDateString() === new Date().toDateString()) return time
  return `${d.toLocaleDateString([], { day: '2-digit', month: 'short' })} ${time}`
}

/** Formats the span between two ISO timestamps as a human duration. */
export function fmtSpan(startIso: string | null | undefined, endIso: string | null | undefined): string | null {
  if (!startIso || !endIso) return null
  const s = Date.parse(startIso.endsWith('Z') || startIso.includes('+') ? startIso : startIso + 'Z')
  const e = Date.parse(endIso.endsWith('Z') || endIso.includes('+') ? endIso : endIso + 'Z')
  if (Number.isNaN(s) || Number.isNaN(e)) return null
  const seconds = Math.max(0, Math.round((e - s) / 1000))
  if (seconds < 60) return `${seconds}s`
  const m = Math.floor(seconds / 60)
  if (m < 60) return `${m}m ${seconds % 60}s`
  return `${Math.floor(m / 60)}h ${m % 60}m`
}

/** Formats a duration in seconds as a human string ('—' for none). */
export function fmtSeconds(seconds: number | null | undefined): string {
  if (seconds == null || seconds <= 0) return '—'
  if (seconds < 60) return `${seconds.toFixed(0)}s`
  const m = Math.floor(seconds / 60)
  const s = Math.round(seconds % 60)
  if (m < 60) return `${m}m ${s}s`
  const h = Math.floor(m / 60)
  return `${h}h ${m % 60}m`
}

/** Formats a saved-percentage-like value honestly: growth is not a gain. */
export function fmtSavedHonest(percent: number | null | undefined): {
  text: string
  grew: boolean
} {
  if (percent === null || percent === undefined) return { text: '—', grew: false }
  if (percent <= 0) return { text: `+${Math.abs(percent).toFixed(1)}% larger`, grew: true }
  return { text: `${percent.toFixed(1)}%`, grew: false }
}

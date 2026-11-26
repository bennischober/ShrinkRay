/**
 * Live job progress — flow, never persisted.
 *
 * One EventSource per app subscribes to `/api/events` (SSE); the snapshot
 * poll stays the only STATE channel (jobs history, library). The server's
 * `hello` frame seeds the MERGED latest event per running job on connect and
 * on every EventSource auto-reconnect; `data` frames carry one engine tick
 * each. Entries for jobs that terminate while the tab is open linger
 * harmlessly — every render site gates on `state === 'running'` from the poll,
 * so stale entries are never read.
 *
 * The view model (issue #631) accumulates events per running FILE: the stage,
 * the stepper position, the probe trail, the chosen CRF and the encode
 * percent/fps. `run` and `file` merge, so a percent tick never erases the
 * folder's file counter; a new file identity drops the previous file's
 * probe/chosen/encode/sample/detail before the new frame merges, mirroring the
 * server's `engine/progress.merge`. Every page renders the same facts through
 * `WorkProgress.svelte`.
 */

import type {
  ProgressChosen,
  ProgressData,
  ProgressEncode,
  ProgressProbe,
  ProgressRun,
  ProgressSample,
  ProgressSource,
  ProgressStage,
} from './api'
import { storedToken } from './api'
import { bytes, crf, duration, finishesAt, fps, measuredAtLabel, percent, vmaf } from './format'

/** The server's schema version. An event from a different version is ignored
 * (the poll stays the state channel), so an old server's flat `probe` event is
 * never rendered as the new schema. Mirrors engine/progress.py. */
export const PROGRESS_VERSION = 1

/** The stepper's order (issue #631's requirement 4). `scan` and `paused` are
 * stages but not steps: a scan has no per-file pipeline, and a pause holds the
 * step the file already reached. */
export const STAGE_STEPS: Record<string, number> = {
  decision: 1,
  bars: 2,
  samples: 3,
  'crf-search': 4,
  encode: 5,
  verify: 6,
  swap: 7,
}

export const STAGE_COUNT = 7

/** The one-line labels every page shows for a running file. */
export const STAGE_LABELS: Record<string, string> = {
  scan: 'Scanning library',
  decision: 'Decision',
  bars: 'Black bars',
  samples: 'Samples',
  'crf-search': 'CRF search',
  encode: 'Encode',
  verify: 'Verify',
  swap: 'Swap',
  paused: 'Paused',
}

export interface EncodeTick {
  percent: number
  at: number
}

export interface ProbeTick {
  index: number
  at: number
}

/** One running file's accumulated view model. */
export interface FileProgressView {
  jobId: number
  stage: ProgressStage | null
  movie: string | null
  path: string | null
  step: number | null
  steps: number | null
  run: ProgressRun | null
  probe: ProgressProbe | null
  probes: ProgressProbe[]
  targetVmaf: number | null
  chosen: ProgressChosen | null
  encode: ProgressEncode | null
  sample: ProgressSample | null
  detail: string | null
  /** measured | cache | plan, the stage result's provenance (issue #631) */
  source: ProgressSource | null
  /** the original measurement time when the source is cache/plan */
  measuredAt: string | null
  /** encoder-reported ETA after the EMA smoothing; null when absent */
  etaSmoothed: number | null
  encodeTicks: EncodeTick[]
  probeTicks: ProbeTick[]
}

let progressByJob = $state<Record<number, ProgressData>>({})
let fileByJob = $state<Record<number, FileProgressView>>({})
let connected = $state(false)
let es: EventSource | null = null

/** Returns the latest merged event for a job (null: none known). */
export function progressFor(jobId: number | null): ProgressData | null {
  if (jobId === null) return null
  return progressByJob[jobId] ?? null
}

/** Returns the accumulated view model for a running folder job (null: none). */
export function fileProgressFor(jobId: number | null): FileProgressView | null {
  if (jobId === null) return null
  return fileByJob[jobId] ?? null
}

/** Whether the SSE stream is currently connected (UI indicator). */
export function eventsConnected(): boolean {
  return connected
}

/** EMA smoothing for the encoder-reported ETA: a new number moves the shown
 * one by a third, so a noisy HandBrake line does not make the countdown
 * jump (issue #631's requirement 5). */
export function smoothEta(previous: number | null, next: number, alpha = 0.3): number {
  if (previous == null) return next
  return alpha * next + (1 - alpha) * previous
}

/** The encoder's ETA when present, else #604's rate method: percent delta
 * over wall-time delta, at least two advancing ticks, null when neither is
 * measured. */
export function encodeEta(view: FileProgressView | null): number | null {
  if (!view || view.stage !== 'encode') return null
  if (view.etaSmoothed != null) return Math.max(0, view.etaSmoothed)
  const ticks = (view.encodeTicks ?? []).filter((t) => Number.isFinite(t.percent))
  if (ticks.length < 2) return null
  const last = ticks[ticks.length - 1]
  let previous: EncodeTick | null = null
  for (let i = ticks.length - 2; i >= 0; i -= 1) {
    if (ticks[i].percent < last.percent) {
      previous = ticks[i]
      break
    }
  }
  if (previous == null) return null
  const seconds = (last.at - previous.at) / 1000
  const percent = last.percent - previous.percent
  if (!(seconds > 0) || !(percent > 0)) return null
  if (last.percent >= 100) return 0
  return Math.max(0, ((100 - last.percent) * seconds) / percent)
}

/** The CRF search's time-left from measured probe pace: two advancing probes
 * and a known count are required; otherwise null (never a fabricated total). */
export function probeEta(view: FileProgressView | null): number | null {
  if (!view || view.stage !== 'crf-search') return null
  const count = view.probe?.count ?? null
  if (count == null || count <= 0) return null
  const ticks = (view.probeTicks ?? [])
    .filter((t) => Number.isInteger(t.index))
    .sort((a, b) => a.index - b.index)
  if (ticks.length < 2) return null
  const first = ticks[0]
  const last = ticks[ticks.length - 1]
  if (last.index <= first.index) return null
  const pace = (last.at - first.at) / (last.index - first.index)
  if (!(pace > 0)) return null
  const remaining = Math.max(0, count - last.index)
  return (remaining * pace) / 1000
}

/** The one ETA estimator every page reads. */
export function progressEta(view: FileProgressView | null): number | null {
  if (!view) return null
  if (view.stage === 'crf-search') return probeEta(view)
  return encodeEta(view)
}

/** The probe position the summary states: `probe 3 of 6`, `probe 3`, or empty. */
export function probePosition(view: FileProgressView | null): string {
  const probe = view?.probe
  if (!probe || probe.index == null) return ''
  if (probe.count != null) return `probe ${probe.index} of ${probe.count}`
  return `probe ${probe.index}`
}

/** The search's completion line, in the issue's exact wording (issue #631). */
export function chosenLine(
  view: FileProgressView | null,
  targetVmaf: number,
  sourceBytes: number | null,
): string {
  const chosen = view?.chosen
  if (!chosen) return ''
  if (chosen.reason) {
    return chosen.reason === 'no size gain'
      ? 'Will not encode — no setting saves space at this quality'
      : `Will not encode — ${chosen.reason}`
  }
  let line = `Found CRF ${crf(chosen.crf)}`
  if (chosen.vmaf != null) line += ` — VMAF ${vmaf(chosen.vmaf, targetVmaf)}`
  if (chosen.predicted_bytes != null) {
    let predicted = `predicted ${bytes(chosen.predicted_bytes)}`
    if (sourceBytes != null && sourceBytes > 0) {
      const saved = (1 - chosen.predicted_bytes / sourceBytes) * 100
      if (saved > 0) predicted += ` (−${saved.toFixed(0)} %)`
    }
    line += ` · ${predicted}`
  }
  return line
}

/** The reused wording the UI must show instead of a fresh-search story (issue
 * #631): a cached replay names the probe cache; a stored plan names the plan. */
export function reusedLabel(view: FileProgressView | null): string {
  if (!view) return ''
  if (view.source === 'cache') {
    return `reused from the plan cache (measured ${measuredAtLabel(view.measuredAt)})`
  }
  if (view.source === 'plan') {
    return `reused from the plan (measured ${measuredAtLabel(view.measuredAt)})`
  }
  return ''
}

/** The same words every page renders for a running file (issue #631). */
export function workSummary(
  view: FileProgressView | null,
  now: number,
  stale: boolean,
): string {
  if (!view || !view.stage) return 'starting…'
  if (view.stage === 'scan') {
    const done = view.run?.done
    return done != null ? `Scanning library · ${done} scanned` : 'Scanning library'
  }
  if (view.stage === 'paused') {
    return view.detail ? `Paused — ${view.detail}` : 'Paused'
  }
  const parts = [STAGE_LABELS[view.stage] ?? view.stage]
  const reused = reusedLabel(view)
  if (reused) parts.push('reused')
  if (view.stage === 'crf-search') {
    // the folder job's file counter, stated first for a search so the
    // operator sees the folder position beside the search position
    const run = view.run
    if (run) {
      parts.unshift(run.total != null ? `file ${run.done} of ${run.total}` : `file ${run.done}`)
    }
    const position = probePosition(view)
    if (position) parts.push(position)
  } else if (view.stage === 'samples' && view.sample) {
    parts.push(`sample ${view.sample.index} of ${view.sample.count}`)
  } else if (view.stage === 'encode') {
    if (view.encode?.percent != null) parts.push(percent(view.encode.percent))
    if (view.encode?.fps != null) parts.push(fps(view.encode.fps))
  } else if (view.step != null) {
    parts.push(`step ${view.step} of ${view.steps ?? STAGE_COUNT}`)
  }
  const eta = progressEta(view)
  if (eta != null) {
    parts.push(`about ${duration(eta)} left`)
    const finish = finishesAt(eta, now)
    if (finish) parts.push(finish)
  }
  if (stale) parts.push('stale')
  return parts.join(' · ')
}

function mergeProbes(previous: ProgressProbe[], probe: ProgressProbe): ProgressProbe[] {
  if (probe.index == null || probe.vmaf == null) return previous
  const probes = previous.filter((p) => p.index !== probe.index)
  probes.push(probe)
  return probes.sort((a, b) => (a.index ?? 0) - (b.index ?? 0))
}

function applyFileEvent(jobId: number, data: ProgressData, at: number): void {
  const previous = fileByJob[jobId] ?? null
  const movie = data.file?.movie ?? previous?.movie ?? null
  const path = data.file?.path ?? previous?.path ?? null
  const newFile =
    previous != null &&
    ((movie != null && previous.movie != null && movie !== previous.movie) ||
      (path != null && previous.path != null && path !== previous.path))
  const stage = (data.stage ?? data.file?.stage ?? previous?.stage ?? null) as
    | ProgressStage
    | null
  const stageChanged = stage !== previous?.stage
  const step =
    data.file?.step ??
    (stageChanged && stage != null ? STAGE_STEPS[stage] ?? null : previous?.step ?? null)
  const steps = data.file?.steps ?? previous?.steps ?? STAGE_COUNT

  let probes = newFile ? [] : [...(previous?.probes ?? [])]
  if (Array.isArray(data.probes)) {
    probes = [...data.probes]
  } else if (data.probe) {
    probes = mergeProbes(probes, data.probe)
  }

  let encodeTicks =
    newFile || (stageChanged && stage !== 'encode') ? [] : [...(previous?.encodeTicks ?? [])]
  let probeTicks =
    newFile || (stageChanged && stage !== 'crf-search')
      ? []
      : [...(previous?.probeTicks ?? [])]
  let etaSmoothed =
    newFile || (stageChanged && stage !== 'encode') ? null : previous?.etaSmoothed ?? null

  const encode =
    newFile ? null : data.encode != null
      ? { ...(previous?.encode ?? {}), ...data.encode }
      : previous?.encode ?? null
  if (data.encode?.eta_seconds != null) {
    etaSmoothed = smoothEta(etaSmoothed, data.encode.eta_seconds)
  }
  if (data.encode?.percent != null) {
    const last = encodeTicks[encodeTicks.length - 1]
    if (last == null || data.encode.percent > last.percent) {
      encodeTicks = [...encodeTicks, { percent: data.encode.percent, at }].slice(-32)
    }
  }
  if (data.probe?.vmaf != null && data.probe.index != null) {
    const last = probeTicks[probeTicks.length - 1]
    if (last == null || data.probe.index > last.index) {
      probeTicks = [...probeTicks, { index: data.probe.index, at }].slice(-16)
    }
  }

  const cachedProbes = probes.filter((p) => p.cached === true && p.measured_at)
  const source: ProgressSource | null =
    data.source ??
    data.chosen?.source ??
    (probes.length > 0 && probes.every((p) => p.cached === true) ? 'cache' : null)
  const measuredAt =
    data.measured_at ??
    data.chosen?.measured_at ??
    (source === 'cache' && cachedProbes.length > 0
      ? cachedProbes.map((p) => p.measured_at!).sort()[0]
      : null)

  fileByJob[jobId] = {
    jobId,
    stage,
    movie,
    path,
    step,
    steps,
    run: data.run ? { ...(previous?.run ?? { done: 0, total: null }), ...data.run } : previous?.run ?? null,
    probe: newFile
      ? data.probe ?? null
      : data.probe
        ? ({ ...(previous?.probe ?? {}), ...data.probe } as ProgressProbe)
        : previous?.probe ?? null,
    probes,
    targetVmaf: data.target_vmaf ?? previous?.targetVmaf ?? null,
    chosen: newFile ? data.chosen ?? null : data.chosen ?? previous?.chosen ?? null,
    encode,
    sample: newFile ? data.sample ?? null : data.sample ?? previous?.sample ?? null,
    detail: newFile ? data.detail ?? null : data.detail ?? previous?.detail ?? null,
    source: newFile ? source : source ?? previous?.source ?? null,
    measuredAt: newFile ? measuredAt : measuredAt ?? previous?.measuredAt ?? null,
    etaSmoothed,
    encodeTicks,
    probeTicks,
  }
}

function applyEvent(jobId: number, data: ProgressData): void {
  if (data.v !== PROGRESS_VERSION) return
  const previous = progressByJob[jobId]
  const merged = mergeRaw(previous, data)
  progressByJob[jobId] = merged
  // the stage row's own event keeps its own job_id (#639); the folder job's
  // event carries stage_job_id, and only that one feeds the file model
  if (
    data.stage === 'scan' ||
    (data.file && (data.stage_job_id == null || data.stage_job_id !== jobId))
  ) {
    applyFileEvent(jobId, merged, Date.now())
  }
}

/** The server's file-transition rule, mirrored (issue #631): a new movie/path
 * identity resets the previous file's per-file detail. Without this, the raw
 * spread below resurrects A's probes/chosen in B's frames, and `applyFileEvent`
 * reads the stale raw state even though its `newFile` branch asks for a reset. */
function fileIdentityChanged(
  previous: ProgressData | undefined,
  data: ProgressData,
): boolean {
  const before = previous?.file
  const after = data.file
  if (!before || !after) return false
  return (
    (after.movie != null && before.movie != null && after.movie !== before.movie) ||
    (after.path != null && before.path != null && after.path !== before.path)
  )
}

function dropFileDetail(state: ProgressData): ProgressData {
  const base: ProgressData = { ...state }
  delete base.probe
  delete base.probes
  delete base.chosen
  delete base.encode
  delete base.sample
  delete base.detail
  return base
}

function mergeRaw(previous: ProgressData | undefined, data: ProgressData): ProgressData {
  const newFile = fileIdentityChanged(previous, data)
  const base = newFile && previous ? dropFileDetail(previous) : previous
  const merged: ProgressData = { ...(base ?? {}), ...data }
  if (data.run) merged.run = { ...(base?.run ?? { done: 0, total: null }), ...data.run }
  if (data.file) merged.file = { ...(base?.file ?? {}), ...data.file }
  if (data.encode) merged.encode = { ...(base?.encode ?? {}), ...data.encode }
  if (data.sample) merged.sample = { ...(base?.sample ?? {}), ...data.sample }
  if (data.probe) merged.probe = { ...(base?.probe ?? {}), ...data.probe } as ProgressProbe
  if (Array.isArray(data.probes)) merged.probes = data.probes
  return merged
}

/** Rebuilds the file model from the hello seed (a reload mid-search must
 * rebuild the probe list, not only the latest tick). */
function seed(jobs: Record<string, ProgressData>): void {
  progressByJob = Object.fromEntries(
    Object.entries(jobs).map(([id, data]) => [Number(id), data]),
  )
  fileByJob = {}
  for (const [id, data] of Object.entries(jobs)) {
    if (data.v !== PROGRESS_VERSION) continue
    const jobId = Number(id)
    if (
      data.stage === 'scan' ||
      (data.file && (data.stage_job_id == null || data.stage_job_id !== jobId))
    ) {
      applyFileEvent(jobId, data, Date.now())
    }
  }
}

/** The SSE stream's token query parameter (issue #36).
 *
 * EventSource cannot set an Authorization header, so the token travels as a
 * query parameter on this one route. The server accepts it only for
 * /api/events. With no token stored the parameter is omitted entirely.
 */
function sseTokenParam(): string {
  const token = storedToken()
  return token ? `?token=${encodeURIComponent(token)}` : ''
}

/** Closes the stream so the next `connectEvents` carries a new token (issue #629):
 * the stream is opened once, so an unlock after the first mount never reached it. */
export function resetEvents(): void {
  if (es !== null) {
    es.close()
    es = null
  }
  connected = false
  progressByJob = {}
  fileByJob = {}
}

/** Opens the SSE stream once (idempotent); reconnects are automatic. */
export function connectEvents(): void {
  if (es !== null) return
  es = new EventSource(`/api/events${sseTokenParam()}`)
  es.addEventListener('hello', (e) => {
    // the hello is authoritative: replace the whole map (it carries the
    // merged latest state of every currently running job — an empty map after a
    // quiet stretch correctly clears stale entries)
    const frame = JSON.parse((e as MessageEvent).data) as {
      jobs: Record<string, ProgressData>
    }
    seed(frame.jobs)
  })
  es.onopen = () => {
    connected = true
  }
  es.onmessage = (e) => {
    const frame = JSON.parse(e.data) as { job_id: number } & ProgressData
    applyEvent(frame.job_id, frame)
  }
  es.onerror = () => {
    // EventSource retries on its own (server sent retry: 3000); the poll
    // keeps the UI correct regardless
    connected = false
  }
}

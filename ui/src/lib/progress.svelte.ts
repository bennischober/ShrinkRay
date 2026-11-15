/**
 * Live job progress — flow, never persisted.
 *
 * One EventSource per app subscribes to `/api/events` (SSE); the snapshot
 * poll stays the only STATE channel (jobs history, library). The server's
 * `hello` frame seeds the latest known progress per running job on connect
 * and on every EventSource auto-reconnect; `data` frames carry one engine
 * tick each. Entries for jobs that terminate while the tab is open linger
 * harmlessly — every render site gates on `state === 'running'` from the
 * poll, so stale entries are never read.
 */

import { storedToken } from './api'

export interface ProgressData {
  stage?: string
  detail?: string
  movie?: string
  percent?: number
  done?: number
  total?: number | null
  /** seconds the current STEP has run: a probe reports its own, so an estimate
   * never divides the job's elapsed time by a probe's fraction (issue #690) */
  elapsed?: number
}

let progressByJob = $state<Record<number, ProgressData>>({})
let connected = $state(false)
let es: EventSource | null = null

/** Returns the latest live progress for a job (null: none known). */
export function progressFor(jobId: number | null): ProgressData | null {
  if (jobId === null) return null
  return progressByJob[jobId] ?? null
}

/** Whether the SSE stream is currently connected (UI indicator). */
export function eventsConnected(): boolean {
  return connected
}

// NOTE: intentionally exported for future status-bar use (e.g. a
// "stream disconnected" indicator); svelte-check does not flag it, this
// comment exists so a future audit does not mistake it for a leftover.

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

/** Opens the SSE stream once (idempotent); reconnects are automatic. */
export function connectEvents(): void {
  if (es !== null) return
  es = new EventSource(`/api/events${sseTokenParam()}`)
  es.addEventListener('hello', (e) => {
    // the hello is authoritative: replace the whole map (it carries the
    // latest event of every currently running job — an empty map after a
    // quiet stretch correctly clears stale entries)
    const frame = JSON.parse((e as MessageEvent).data) as {
      jobs: Record<string, ProgressData>
    }
    progressByJob = Object.fromEntries(
      Object.entries(frame.jobs).map(([id, d]) => [Number(id), d])
    )
  })
  es.onopen = () => {
    connected = true
  }
  es.onmessage = (e) => {
    const frame = JSON.parse(e.data) as { job_id: number } & ProgressData
    progressByJob[frame.job_id] = frame
  }
  es.onerror = () => {
    // EventSource retries on its own (server sent retry: 3000); the poll
    // keeps the UI correct regardless
    connected = false
  }
}

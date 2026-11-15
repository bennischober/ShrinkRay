import { duration } from './format'

export type ConnectionState = 'loading' | 'locked' | 'live' | 'stale'

/** The shell's three poll cadences: fast while work runs, relaxed when idle,
 * and slow while the tab is hidden (the browser throttles a hidden timer anyway). */
export const POLL_ACTIVE = 1000
export const POLL_IDLE = 3000
export const POLL_HIDDEN = 10_000

/** Two hidden-tab poll intervals plus slack: the stale threshold has to outlast
 * the SLOWEST cadence, or a healthy hidden tab calls itself stale. */
export const STALE_AFTER_MS = 22_000

export interface ConnectionInput {
  hasSnapshot: boolean
  locked: boolean
  lastOkAt: number | null
  eventsUp: boolean
  now: number
}

/** The rule issue #629 encodes: a claim — a count, an "all clear", a running
 * state, an ETA — is only drawn while `live`, because `loading` and `locked` are
 * unknown rather than zero and `stale` holds data that is no longer current. */
export function deriveConnection(input: ConnectionInput): ConnectionState {
  if (input.locked) return 'locked'
  if (!input.hasSnapshot || input.lastOkAt === null) return 'loading'
  if (input.now - input.lastOkAt > STALE_AFTER_MS) return 'stale'
  // a frozen progress bar keeps looking current, so a dropped stream is stale
  if (!input.eventsUp) return 'stale'
  return 'live'
}

export function freshnessText(
  state: ConnectionState,
  lastOkAt: number | null,
  now: number,
): string {
  if (state === 'live') return 'Live'
  if (state === 'locked') return 'Locked'
  if (state === 'loading') return 'Connecting…'
  if (lastOkAt === null) return 'Reconnecting…'
  const seconds = Math.max(0, (now - lastOkAt) / 1000)
  return `Updated ${duration(seconds)} ago · reconnecting…`
}

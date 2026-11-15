import { duration } from './format'

export type ConnectionState = 'loading' | 'locked' | 'live' | 'stale'

/** Two hidden-tab poll intervals (10 s) plus slack: the shell polls at 1 s, 3 s
 * and 10 s (hidden), so anything shorter than the slowest cadence would call a
 * healthy hidden tab stale. */
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

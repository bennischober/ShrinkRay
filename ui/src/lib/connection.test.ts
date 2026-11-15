import { describe, expect, it } from 'vitest'
import { deriveConnection, freshnessText, STALE_AFTER_MS, type ConnectionInput } from './connection'

const NOW = 1_000_000

function input(overrides: Partial<ConnectionInput> = {}): ConnectionInput {
  return {
    hasSnapshot: true,
    locked: false,
    lastOkAt: NOW - 1000,
    eventsUp: true,
    now: NOW,
    ...overrides,
  }
}

describe('deriveConnection', () => {
  it('is loading until the first snapshot arrives', () => {
    expect(deriveConnection(input({ hasSnapshot: false, lastOkAt: null }))).toBe('loading')
    expect(deriveConnection(input({ hasSnapshot: false, lastOkAt: NOW }))).toBe('loading')
  })

  it('is locked whenever the server refused the token', () => {
    expect(deriveConnection(input({ locked: true }))).toBe('locked')
    expect(deriveConnection(input({ locked: true, hasSnapshot: false, lastOkAt: null }))).toBe('locked')
  })

  it('is live only while the snapshot is fresh and the stream is up', () => {
    expect(deriveConnection(input())).toBe('live')
  })

  it('is stale once the snapshot is older than two poll intervals', () => {
    expect(deriveConnection(input({ lastOkAt: NOW - STALE_AFTER_MS - 1 }))).toBe('stale')
  })

  it('is stale when the event stream drops, so a frozen bar never looks live', () => {
    expect(deriveConnection(input({ eventsUp: false }))).toBe('stale')
  })

  it('never reports live before a snapshot, even with a fresh timestamp', () => {
    expect(deriveConnection(input({ hasSnapshot: false, lastOkAt: NOW }))).toBe('loading')
  })
})

describe('freshnessText', () => {
  it('says Live only in the live state', () => {
    expect(freshnessText('live', NOW, NOW)).toBe('Live')
  })

  it('states the age of the data in the stale state', () => {
    expect(freshnessText('stale', NOW - 120_000, NOW)).toBe('Updated 2 min ago · reconnecting…')
  })

  it('says nothing about freshness when there is no data yet', () => {
    expect(freshnessText('loading', null, NOW)).toBe('Connecting…')
    expect(freshnessText('stale', null, NOW)).toBe('Reconnecting…')
    expect(freshnessText('locked', null, NOW)).toBe('Locked')
  })
})

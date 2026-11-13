import { describe, expect, it } from 'vitest'
import {
  failedOutcomeNote,
  fmtSavedHonest,
  fmtVmaf,
  fmtSeconds,
  fmtSpan,
  humanizeError,
  isAuthFailure,
  jobPaths,
  movieOutcome,
  storeToken,
  storedToken,
  type Job,
} from './api'

/** Builds a minimal Job; tests only touch the fields the helpers read. */
function job(overrides: Partial<Job> = {}): Job {
  return {
    id: 1,
    kind: 'encode',
    payload: {},
    state: 'done',
    result: null,
    error: null,
    ...overrides,
  }
}

describe('jobPaths (issue #100 regression)', () => {
  it('unions path and paths — the first target of a multi-folder batch is never dropped', () => {
    const j = job({ payload: { path: '/m/a', paths: ['/m/b', '/m/c'] } })
    expect(jobPaths(j)).toEqual(['/m/a', '/m/b', '/m/c'])
  })

  it('returns paths alone when no path key exists', () => {
    const j = job({ payload: { paths: ['/m/b'] } })
    expect(jobPaths(j)).toEqual(['/m/b'])
  })

  it('falls back to path when paths is empty', () => {
    const j = job({ payload: { path: '/m/a', paths: [] } })
    expect(jobPaths(j)).toEqual(['/m/a'])
  })

  it('deduplicates case- and separator-insensitively', () => {
    const j = job({ payload: { path: 'D:\\Movies\\A', paths: ['d:/movies/a/'] } })
    expect(jobPaths(j)).toEqual(['D:\\Movies\\A'])
  })

  it('returns empty for a payload without targets', () => {
    expect(jobPaths(job({ payload: {} }))).toEqual([])
  })
})

describe('fmtVmaf (issue #599)', () => {
  it('keeps a score below the target below it after rounding', () => {
    expect(fmtVmaf(95.96, 96)).toBe('95.9')
    expect(fmtVmaf(95.99, 96)).toBe('95.9')
  })

  it('keeps a score above the target above it after rounding', () => {
    expect(fmtVmaf(96.01, 96)).toBe('96.1')
    expect(fmtVmaf(96.04, 96)).toBe('96.1')
  })

  it('leaves a score away from the target at its one-decimal form', () => {
    expect(fmtVmaf(95.94, 96)).toBe('95.9')
    expect(fmtVmaf(96.06, 96)).toBe('96.1')
    expect(fmtVmaf(95.5, 96)).toBe('95.5')
  })

  it('formats without a target and renders null as a dash', () => {
    expect(fmtVmaf(96.04)).toBe('96.0')
    expect(fmtVmaf(null, 96)).toBe('—')
    expect(fmtVmaf(undefined, 96)).toBe('—')
  })
})

describe('movieOutcome', () => {
  it('finds the movie in an encode job outcomes list', () => {
    const j = job({
      kind: 'encode',
      result: { outcomes: [{ movie: '/m/a/file.mkv', state: 'done', crf: 30 }] },
    })
    expect(movieOutcome(j, '/m/a/file.mkv')?.crf).toBe(30)
  })

  it('reads plans for plan jobs', () => {
    const j = job({ kind: 'plan', result: { plans: [{ movie: '/m/a', state: 'skip', reason: 'efficient' }] } })
    expect(movieOutcome(j, '/m/a')?.reason).toBe('efficient')
  })

  it('returns null for scan/legacy jobs and missing movies', () => {
    expect(movieOutcome(job({ kind: 'scan' }), '/m/a')).toBeNull()
    expect(movieOutcome(job({ kind: 'legacy' }), '/m/a')).toBeNull()
    expect(movieOutcome(job({ kind: 'encode', result: { outcomes: [] } }), '/m/a')).toBeNull()
    expect(movieOutcome(job({ kind: 'encode', result: null }), '/m/a')).toBeNull()
  })
})

describe('failedOutcomeNote', () => {
  it('names the failed movies of a partially-done encode', () => {
    const j = job({
      kind: 'encode',
      result: {
        outcomes: [
          { movie: '/m/a', state: 'done' },
          { movie: '/m/b', state: 'failed', error: 'boom' },
        ],
      },
    })
    expect(failedOutcomeNote(j)).toContain('1 of 2 failed')
    expect(failedOutcomeNote(j)).toContain('/m/b')
  })

  it('is empty for clean done, failed jobs, non-encodes', () => {
    expect(failedOutcomeNote(job({ state: 'done' }))).toBe('')
    expect(failedOutcomeNote(job({ state: 'failed' }))).toBe('')
    expect(failedOutcomeNote(job({ kind: 'plan', state: 'done' }))).toBe('')
  })
})

describe('humanizeError', () => {
  it('extracts status + detail from API error bodies', () => {
    const raw = 'API 400: {"detail":"output directory is not inside an allowlisted root"}'
    expect(humanizeError(400, raw)).toContain('400')
    expect(humanizeError(400, raw)).toContain('allowlisted root')
  })

  it('translates known failures into operator language', () => {
    expect(humanizeError(null, 'API 503: Service Unavailable')).toContain('still starting')
    expect(humanizeError(null, "[Errno 2] No such file or directory: '/m/x/y.mkv'")).toContain('y.mkv')
  })

  it('passes unknown text through untouched', () => {
    const raw = 'something completely unexpected'
    expect(humanizeError(null, raw)).toBe(raw)
  })
})

describe('fmtSavedHonest (growth is not a gain)', () => {
  it('formats savings plainly', () => {
    expect(fmtSavedHonest(64.3).text).toBe('64.3%')
    expect(fmtSavedHonest(64.3).grew).toBe(false)
  })

  it('labels growth as larger, never as a positive saving', () => {
    const r = fmtSavedHonest(-5)
    expect(r.text).toBe('+5.0% larger')
    expect(r.grew).toBe(true)
  })

  it('renders an em dash for unknown values', () => {
    expect(fmtSavedHonest(null).text).toBe('—')
    expect(fmtSavedHonest(undefined).text).toBe('—')
  })
})

describe('time formatters', () => {
  it('fmtSeconds renders human durations', () => {
    expect(fmtSeconds(null)).toBe('—')
    expect(fmtSeconds(0)).toBe('—')
    expect(fmtSeconds(45)).toBe('45s')
    expect(fmtSeconds(125)).toBe('2m 5s')
    expect(fmtSeconds(3700)).toBe('1h 1m')
  })

  it('fmtSpan computes a duration between ISO stamps', () => {
    expect(fmtSpan('2026-10-24T10:00:00Z', '2026-10-24T10:01:30Z')).toBe('1m 30s')
    expect(fmtSpan('2026-10-24T10:00:00Z', null)).toBeNull()
  })
})

describe('token storage (issue #36 auth)', () => {
  it('stores and clears the token in localStorage', () => {
    storeToken('secret')
    expect(storedToken()).toBe('secret')
    storeToken(null)
    expect(storedToken()).toBeNull()
  })

  it('isAuthFailure only fires on 401 errors', () => {
    expect(isAuthFailure(new Error('API 401: unauthorized'))).toBe(true)
    expect(isAuthFailure(new Error('API 500: boom'))).toBe(false)
    expect(isAuthFailure('401')).toBe(false)
  })
})

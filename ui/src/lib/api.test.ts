import { describe, expect, it, vi } from 'vitest'
import {
  failedOutcomeNote,
  isFailedOutcome,
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

describe('isFailedOutcome', () => {
  // the rail's roll-up and the note must agree on what counts as work lost
  it('counts a failed movie and a declined swap, never a busy or HDR skip', () => {
    expect(isFailedOutcome({ movie: 'a', state: 'failed' })).toBe(true)
    expect(
      isFailedOutcome({ movie: 'b', state: 'skipped', error: 'the swap was declined: subtitles' }),
    ).toBe(true)
    expect(isFailedOutcome({ movie: 'c', state: 'skipped', error: 'hdr not supported' })).toBe(false)
    expect(isFailedOutcome({ movie: 'd', state: 'skipped', error: 'the file was busy' })).toBe(false)
    expect(isFailedOutcome({ movie: 'e', state: 'encoded' })).toBe(false)
  })
})

describe('failedOutcomeNote', () => {
  it('leaves a busy or HDR skip clean, because nothing was thrown away (issue #630)', () => {
    const j = job({
      kind: 'encode',
      result: {
        outcomes: [
          { movie: '/m/a', state: 'skipped', error: 'hdr not supported' },
          { movie: '/m/b', state: 'skipped', error: 'the file was busy' },
        ],
      },
    })
    expect(failedOutcomeNote(j)).toBe('')
  })

  it('counts a discarded encode as work thrown away, not a clean done (issue #630)', () => {
    const j = job({
      kind: 'encode',
      result: {
        outcomes: [
          { movie: '/m/a', state: 'done' },
          { movie: '/m/b', state: 'skipped', output_size: null, error: 'the swap was declined: subtitles (the encode is discarded)' },
        ],
      },
    })
    expect(failedOutcomeNote(j)).toContain('1 of 2')
    expect(failedOutcomeNote(j)).toContain('/m/b')
  })

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

describe('snapshot conditional requests (issue #661)', () => {
  it('sends the known ETags and passes unchanged collections through as null', async () => {
    vi.resetModules()
    const { api } = await import('./api')
    const calls: { url: string; headers: Record<string, string> }[] = []
    const bodies = [
      {
        status: { watcher: 'running' },
        jobs: [{ id: 1, kind: 'encode', payload: {}, state: 'running', result: null, error: null }],
        jobs_etag: '"jobs-1"',
        library: [
          {
            path: '/media/a.mkv',
            name: 'a.mkv',
            category: 'C',
            duration: null,
            codec: 'h264',
            resolution: [1920, 1080],
            size: 1,
            reclaimed_bytes: 0,
            hdr: false,
            encode: null,
          },
        ],
        library_etag: '"lib-1"',
      },
      {
        status: { watcher: 'running' },
        jobs: null,
        jobs_etag: '"jobs-1"',
        library: null,
        library_etag: '"lib-1"',
      },
    ]
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        calls.push({
          url: String(input),
          headers: (init?.headers ?? {}) as Record<string, string>,
        })
        return {
          ok: true,
          status: 200,
          json: async () => bodies[calls.length - 1],
        } as Response
      }),
    )
    try {
      const first = await api.snapshot()
      const second = await api.snapshot()
      // the second poll names both known tags; the server answers null for
      // both collections, which is the contract that keeps the arrays intact
      expect(calls[1].headers['If-None-Match']).toBe('"lib-1", "jobs-1"')
      expect(first.library).not.toBeNull()
      expect(first.jobs).not.toBeNull()
      expect(second.library).toBeNull()
      expect(second.jobs).toBeNull()
    } finally {
      vi.unstubAllGlobals()
    }
  })
})

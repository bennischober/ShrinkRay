import { describe, expect, it, vi } from 'vitest'
import { render, screen } from '@testing-library/svelte'
import OverviewPage from './OverviewPage.svelte'

// the running card's stage comes from the live progress store: a mutable stub
// lets one test seed it without a stream (issue #630)
let progressStub: { percent?: number; movie?: string | null; detail?: string | null } | null = null
vi.mock('../progress.svelte', () => ({ progressFor: () => progressStub }))
import type { Job, Movie, Status } from '../api'

/** The overview is the status window's front page. Issue #239: it rendered
 * a CANCEL button wired to nothing (oncancel was a prop App.svelte never
 * passed, cancelling a state nothing assigned) — a control that looks armed
 * and does nothing on a 4-hour encode. The render test pins the absence. */

function movie(overrides: Partial<Movie> = {}): Movie {
  return {
    path: '/m/a.mkv',
    name: 'a.mkv',
    category: 'C',
    size: 1024 ** 3,
    duration: '01:00:00',
    codec: 'h264',
    hdr: false,
    ...overrides,
  } as Movie
}

function job(overrides: Partial<Job> = {}): Job {
  return {
    id: 1,
    kind: 'encode',
    payload: { path: '/m/a', quality: true, target_vmaf: 96 },
    state: 'running',
    result: null,
    error: null,
    created_at: '2026-10-25T10:00:00Z',
    started_at: '2026-10-25T10:00:01Z',
    finished_at: null,
    ...overrides,
  }
}

const status: Status = {
  queue: [],
  watcher: 'running',
  database: '/data/metadata.db',
  rss_mib: 120,
  reveal_available: false,
}

function renderOverview(runningJobs: Job[] = [], movies: Movie[] = [movie()], history?: Job[]) {
  return render(OverviewPage, {
    movies,
    history: history ?? (runningJobs.length ? runningJobs : [job()]),
    status,
    targetVmaf: 96,
    savedTotalGb: 0.5,
    measuredCount: 0,
    missingMovies: [],
    runningJobs,
    failedJobs: [],
    scanPaths: ['/m'],
    outputDir: '/out',
    ondismiss: () => {},
    ongoto: () => {},
    onopenmovie: () => {},
  })
}

describe('OverviewPage render (issue #239)', () => {
  it('a running job shows progress but no cancel affordance', () => {
    renderOverview([job()])
    expect(screen.getByText(/Working · /)).toBeTruthy()
    // the control that did nothing: gone (the dashboard observes, it does
    // not steer — PRODUCT.md; the routes behind cancel were removed with
    // issue #134 and the button outlived them)
    expect(screen.queryByText('CANCEL')).toBeNull()
    expect(screen.queryByText('CANCELLING…')).toBeNull()
  })

  it('no encode-all ceremony affordance remains', () => {
    renderOverview()
    // the ceremony's entry point was the removed enqueue flow; neither the
    // button nor the dialog may exist
    expect(screen.queryByText(/ENCODE ALL/i)).toBeNull()
    expect(screen.queryByText(/ENCODE THE ENTIRE LIBRARY/i)).toBeNull()
  })
})

describe('OverviewPage VMAF display (issue #599)', () => {
  it('a measured score just below the target is not printed as the target', () => {
    renderOverview(
      [],
      [
        movie({
          encode: {
            preset: 'svt_av1',
            crf: 30,
            vmaf: 95.96,
            input_size: 1024 ** 3,
            output_size: 512 ** 2,
            saved_percent: 99,
          },
        }),
      ],
    )
    expect(screen.getAllByText('95.9').length).toBeGreaterThan(0)
    expect(screen.queryByText('96.0')).toBeNull()
  })

  it('answers "is everything OK?" before its internals (issue #633)', () => {
    const running = job({
      id: 9,
      kind: 'encode',
      state: 'running',
      started_at: '2026-10-24T10:00:00Z',
      finished_at: null,
      payload: { paths: ['/m/a.mkv'] },
    })
    renderOverview([running])
    expect(screen.getAllByText(/● Working — encoding/).length).toBeGreaterThan(0)
  })

  it('the stat band says where every title went (issue #633)', () => {
    // a skip record is neither an encode nor untouched: the band used to
    // account for five of seven titles and say nothing about the rest
    const movies = [
      movie({ path: '/m/a.mkv', encode: { preset: 'skipped (hdr not supported)' } as never }),
      movie({ path: '/m/b.mkv', encode: null }),
    ]
    renderOverview([], movies)
    expect(screen.getAllByText(/1 skipped/).length).toBeGreaterThan(0)
    expect(screen.getAllByText(/1 untouched/).length).toBeGreaterThan(0)
  })

  it('says how long ago the plan finished (issue #630, case 8)', () => {
    // pin the clock: relative() counts calendar days, so a real Date.now()
    // crosses a DST boundary twice a year and flips 3 to 2 or 4
    vi.useFakeTimers()
    vi.setSystemTime(new Date('2026-06-15T12:00:00Z'))
    try {
      const planJob = job({
        kind: 'plan',
        state: 'done',
        finished_at: '2026-06-12T12:00:00Z',
        payload: { path: '/m', quality: true, target_vmaf: 96 },
        result: {
          plans: [{ movie: 'a.mkv', state: 'encode', crf: 30, vmaf: 96.1, estimated_size: 1024 ** 2, saved_percent: 50 }],
        },
      })
      renderOverview([], [movie()], [planJob])
      expect(screen.getAllByText(/finished 3 days ago/).length).toBeGreaterThan(0)
    } finally {
      vi.useRealTimers()
    }
  })

  it('does not count a skip record as an encoded title (issue #630, case 6)', () => {
    // a skip record means nothing was written, so the plan entry is still
    // open: the summary must say "to encode", never "encoded since"
    const planJob = job({
      kind: 'plan',
      state: 'done',
      payload: { path: '/m', quality: true, target_vmaf: 96 },
      result: {
        plans: [{ movie: 'a.mkv', state: 'encode', crf: 30, vmaf: 97.04, estimated_size: 1024 ** 2, saved_percent: 50 }],
      },
    })
    renderOverview([], [movie({ encode: { preset: 'skipped (no size gain)' } as never })], [planJob])
    expect(screen.queryByText(/encoded since/)).toBeNull()
    expect(screen.getAllByText(/to encode/).length).toBeGreaterThan(0)
  })

  it('reads a plan score against the target its job was run with', () => {
    // 95.04 rounds onto 95.0 under the job's 95 target: the display must
    // step up to 95.1, which only the payload target can produce (the live
    // 96 target would leave 95.0 untouched).
    const planJob = job({
      kind: 'plan',
      state: 'done',
      payload: { path: '/m/a', quality: true, target_vmaf: 95 },
      result: {
        plans: [{ movie: 'a.mkv', state: 'encode', crf: 30, vmaf: 95.04, estimated_size: 1024 ** 2, saved_percent: 50 }],
      },
    })
    renderOverview([], [movie()], [planJob])
    expect(screen.getAllByText('95.1').length).toBeGreaterThan(0)
    expect(screen.queryByText('95.0')).toBeNull()
  })

  it('the running card at 30 % says working, not working… (issue #630)', async () => {
    progressStub = { percent: 30.46, movie: 'Titel', detail: null }
    const running = { id: 4, kind: 'encode', state: 'running', created_at: '2026-10-24T10:00:00Z', started_at: '2026-10-24T10:00:00Z', finished_at: null, payload: {}, result: { plans: [] }, error: null } as unknown as Job
    const { container } = render(OverviewPage, { movies: [] as Movie[], history: [running], status: null, targetVmaf: 96, savedTotalGb: 0, measuredCount: 0, missingMovies: [], runningJobs: [running], failedJobs: [], scanPaths: [], outputDir: '', ondismiss: () => {}, ongoto: () => {}, onopenmovie: () => {} })
    const text = (container.textContent ?? '').replace(/\s+/g, ' ')
    expect(text).toContain('working')
    expect(text).not.toContain('working…')
    progressStub = null
  })
})

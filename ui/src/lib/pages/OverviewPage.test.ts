import { describe, expect, it, vi } from 'vitest'
import { render, screen } from '@testing-library/svelte'
import OverviewPage from './OverviewPage.svelte'

// the running card's stage comes from the live progress store: a mutable stub
// lets one test seed it without a stream (issue #630)
let progressStub: Record<string, unknown> | null = null
vi.mock('../progress.svelte', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../progress.svelte')>()
  return { ...actual, fileProgressFor: () => progressStub }
})
import type { Job, Movie, Status } from '../api'
import { reclaimedTotalBytes } from '../format'

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
    reclaimed_bytes: 0,
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

function renderOverview(
  runningJobs: Job[] = [],
  movies: Movie[] = [movie()],
  history?: Job[],
  savedTotalGb = 0.5,
) {
  return render(OverviewPage, {
    movies,
    history: history ?? (runningJobs.length ? runningJobs : [job()]),
    status,
    targetVmaf: 96,
    savedTotalGb,
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

  it('does not show the Setup panel: its facts are elsewhere or not actionable (issue #633)', () => {
    // the scan root and write target are the Config page's, the watcher is in
    // the status line, the database path is in the internals line — so the
    // panel repeated all four without giving the operator an action
    renderOverview()
    expect(screen.queryByText('SETUP')).toBeNull()
    expect(screen.queryByText('SCAN FROM')).toBeNull()
    expect(screen.queryByText('WRITE TO')).toBeNull()
    expect(screen.queryByText('RESIDENT MEMORY')).toBeNull()
    expect(screen.getAllByText(/POLL ADAPTIVE/).length).toBeGreaterThan(0)
  })

  it('does not chart a category that holds one title (issue #633)', () => {
    // a category with one title is that title's own file size, not a category
    const movies = [
      movie({ path: '/m/a.mkv', name: 'a.mkv', category: 'Same' }),
      movie({ path: '/m/b.mkv', name: 'b.mkv', category: 'Same' }),
      movie({ path: '/m/c.mkv', name: 'c.mkv', category: 'Alone' }),
    ]
    const { container } = renderOverview([], movies)
    const names = [...container.querySelectorAll('.bar-name')].map((e) => e.textContent?.trim())
    expect(names).toContain('Same')
    expect(names).not.toContain('Alone')
  })

  it('the RECLAIMED stat and the category bars read the same server field and guard (issue #646)', () => {
    // the server's reclaimed_bytes is the true library delta plus
    // output-dir savings. The encode sizes below describe a DIFFERENT
    // quantity (one hop); neither surface may re-derive them.
    const chain = movie({
      path: '/m/chain.mkv',
      name: 'chain.mkv',
      category: 'C',
      size: 400,
      reclaimed_bytes: 600,
      encode: {
        preset: 'VMAF>95',
        crf: 20,
        vmaf: 95.5,
        input_size: 500,
        output_size: 400,
        saved_percent: 20,
        outcome: 'applied',
      },
    })
    const outDir = movie({
      path: '/m/out.mkv',
      name: 'out.mkv',
      category: 'C',
      size: 1000,
      reclaimed_bytes: 600,
      encode: {
        preset: 'VMAF>95',
        crf: 20,
        vmaf: 95.5,
        input_size: 1000,
        output_size: 400,
        saved_percent: 60,
        outcome: 'applied',
      },
    })
    // a stale positive on a missing file is dropped by the client guard,
    // exactly as the server sends 0 for it
    const missing = movie({
      path: '/m/gone.mkv',
      name: 'gone.mkv',
      category: 'C',
      missing: true,
      reclaimed_bytes: 5000,
    })
    // a positive field with saved_percent 0 still counts: the server's
    // clamp is the authority, not a re-derived percentage guard
    const rounded = movie({
      path: '/m/rounded.mkv',
      name: 'rounded.mkv',
      category: 'C',
      size: 1000,
      reclaimed_bytes: 1,
      encode: {
        preset: 'VMAF>95',
        crf: 20,
        vmaf: 95.5,
        input_size: 1000,
        output_size: 999,
        saved_percent: 0,
        outcome: 'applied',
      },
    })
    const movies = [chain, outDir, missing, rounded]
    const total = reclaimedTotalBytes(movies)
    expect(total).toBe(1201)
    const { container, unmount } = renderOverview([], movies, undefined, total / 1024 ** 3)
    try {
      const reclaimedStat = [...container.querySelectorAll('.stat')].find((el) =>
        el.textContent?.includes('RECLAIMED'),
      )
      expect(reclaimedStat?.textContent).toContain('1.17 KiB')
      expect(reclaimedStat?.textContent).toContain('library delta + output-dir')
      const nums = container.querySelector('.bar-nums')?.textContent ?? ''
      expect(nums).toContain('1.17 KiB')
      // the one-hop sizes (100 + 600 + 1) are not the figure
      expect(nums).not.toContain('701 B')
    } finally {
      unmount()
    }
  })

  it('counts a corroborated chain when its latest attempt was discarded (issue #646)', () => {
    // the discarded ROW's sizes are gone (#625), but the earlier applied
    // chain's delta is real and the server's field carries it. The old
    // title-level discarded guard zeroed the whole title - exactly the
    // one-hop behaviour #646 removes.
    const chain = movie({
      path: '/m/chain-discarded.mkv',
      name: 'chain-discarded.mkv',
      category: 'C',
      size: 400,
      reclaimed_bytes: 600,
      encode: {
        preset: 'VMAF>95',
        crf: 20,
        vmaf: 95.5,
        input_size: 400,
        output_size: 390,
        saved_percent: 2.5,
        outcome: 'discarded',
      },
    })
    const other = movie({
      path: '/m/other.mkv',
      name: 'other.mkv',
      category: 'C',
      size: 600,
    })
    const movies = [chain, other]
    const total = reclaimedTotalBytes(movies)
    expect(total).toBe(600)
    const { container, unmount } = renderOverview([], movies, undefined, total / 1024 ** 3)
    try {
      const reclaimedStat = [...container.querySelectorAll('.stat')].find((el) =>
        el.textContent?.includes('RECLAIMED'),
      )
      expect(reclaimedStat?.textContent).toContain('600 B')
      const nums = container.querySelector('.bar-nums')?.textContent ?? ''
      expect(nums).toContain('600 B')
    } finally {
      unmount()
    }
  })

  it('excludes a missing file from the category bar (issue #647)', () => {
    // the server already sends 0 for a missing file; the client keeps this
    // one guard for what remains client-side. TWO titles chart the category
    // at all (a single title is never charted). The bar's SIZE total still
    // includes the missing title's size - only the reclaimed figure is
    // excluded.
    const encode = {
      preset: 'VMAF>95',
      crf: 21,
      vmaf: 95.5,
      input_size: 1000,
      output_size: 400,
      saved_percent: 60,
      outcome: 'applied',
      outcome_reason: null,
    }
    const movies = [
      movie({
        path: '/m/gone.mkv',
        name: 'gone.mkv',
        category: 'C',
        size: 1000,
        missing: true,
        reclaimed_bytes: 5000,
        encode,
      }),
      movie({ path: '/m/here.mkv', name: 'here.mkv', category: 'C', size: 900 }),
    ]
    const { container } = renderOverview([], movies)
    const nums = container.querySelector('.bar-nums')?.textContent ?? ''
    // the reclaimed figure prints with a minus (fmtReclaimed): the missing
    // title's positive 5000 B field must not appear - only the sizes do
    expect(nums).toContain('1.86 KiB')
    expect(nums).not.toContain('−')
  })

  it('does not repeat a single title\u2019s value as a mean (issue #633)', () => {
    // the head never repeats the single row beneath it (DESIGN.md): with one
    // measured title the panel shows that title's own row and no head figure
    const { container } = renderOverview([], [movie({ encode: { vmaf: 96.1 } as never })])
    expect(screen.queryByText('MEAN')).toBeNull()
    expect(screen.queryByText('MEASURED')).toBeNull()
    expect(container.querySelectorAll('.q-row').length).toBe(1)
  })

  it('says what needs you before its own internals (issue #633)', () => {
    // the page's order is the operator's: is everything OK, then what needs
    // you, then the rest — the poll mode and database path come last
    const { container } = renderOverview()
    const text = container.textContent ?? ''
    expect(text.indexOf('ATTENTION')).toBeGreaterThan(-1)
    expect(text.indexOf('ATTENTION')).toBeLessThan(text.indexOf('POLL ADAPTIVE'))
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

  it('the running card names the encode stage, never starting… (issue #631)', async () => {
    progressStub = {
      stage: 'encode',
      movie: 'Titel',
      path: '/m/a.mkv',
      step: 5,
      steps: 7,
      encode: { percent: 30.46, fps: null, avg_fps: null, eta_seconds: null },
      encodeTicks: [],
      probeTicks: [],
    }
    const running = { id: 4, kind: 'encode', state: 'running', created_at: '2026-10-24T10:00:00Z', started_at: '2026-10-24T10:00:00Z', finished_at: null, payload: {}, result: { plans: [] }, error: null } as unknown as Job
    const { container } = render(OverviewPage, { movies: [] as Movie[], history: [running], status: null, targetVmaf: 96, savedTotalGb: 0, measuredCount: 0, missingMovies: [], runningJobs: [running], runningProgress: progressStub as never, failedJobs: [], scanPaths: [], outputDir: '', ondismiss: () => {}, ongoto: () => {}, onopenmovie: () => {} })
    const text = (container.textContent ?? '').replace(/\s+/g, ' ')
    expect(text).toContain('Encode')
    expect(text).toContain('30.5%')
    expect(text).not.toContain('starting…')
    progressStub = null
  })
  it('freezes its ETA when the data goes stale (issue #629)', async () => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date('2026-10-25T12:00:00Z'))
    const running = job({ started_at: '2026-10-25T10:00:00Z' })
    progressStub = {
      stage: 'encode',
      movie: 'a.mkv',
      path: '/m/a.mkv',
      encode: { percent: 50, fps: null, avg_fps: null, eta_seconds: 7200 },
      etaSmoothed: 7200,
      encodeTicks: [],
      probeTicks: [],
    }
    const props = {
      movies: [movie()],
      history: [running],
      status,
      targetVmaf: 96,
      savedTotalGb: 0,
      measuredCount: 0,
      missingMovies: [],
      runningJobs: [running],
      runningProgress: progressStub as never,
      failedJobs: [],
      scanPaths: ['/m'],
      outputDir: '/out',
      ondismiss: () => {},
      ongoto: () => {},
      onopenmovie: () => {},
    }
    const { container, rerender } = render(OverviewPage, { ...props, stale: false })
    const live = container.querySelector('.running')!.textContent
    vi.setSystemTime(new Date('2026-10-25T14:00:00Z'))
    // a fresh job object forces the estimate to recompute against the new clock:
    // with the freeze removed it would count down, and this assertion would fail
    await rerender({
      ...props,
      stale: true,
      runningJobs: [job({ started_at: '2026-10-25T10:00:00Z' })],
    })
    const stale = container.querySelector('.running')!.textContent
    expect(stale).toContain('stale')
    // the ETA text and the wall-clock finish are frozen: neither moved
    // while stale (the real estimate, not a mocked constant)
    const left = (text: string | null) => text?.match(/about [^·]+ left/)?.[0]
    const finish = (text: string | null) => text?.match(/finishes ≈ \d{2}:\d{2}/)?.[0]
    expect(left(live)).toBe('about 2 h left')
    expect(left(stale)).toBe(left(live))
    expect(finish(live)).toMatch(/^finishes ≈ \d{2}:\d{2}$/)
    expect(finish(stale)).toBe(finish(live))
    progressStub = null
    vi.useRealTimers()
  })
})

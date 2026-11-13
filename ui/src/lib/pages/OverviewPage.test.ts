import { describe, expect, it } from 'vitest'
import { render, screen } from '@testing-library/svelte'
import OverviewPage from './OverviewPage.svelte'
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
    expect(screen.getByText(/IN WORK/)).toBeTruthy()
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
})

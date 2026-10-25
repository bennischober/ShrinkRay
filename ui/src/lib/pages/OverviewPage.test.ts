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

function renderOverview(runningJobs: Job[] = []) {
  return render(OverviewPage, {
    movies: [movie()],
    history: runningJobs.length ? runningJobs : [job()],
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

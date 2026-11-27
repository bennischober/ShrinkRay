import { describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/svelte'
import JobsPage from './JobsPage.svelte'

// the running row's stage comes from the live progress store: a mutable stub
// lets one test seed it without a stream (issue #630)
let progressStub: Record<string, unknown> | null = null
vi.mock('../progress.svelte', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../progress.svelte')>()
  return {
    ...actual,
    fileProgressFor: () => progressStub,
    progressEta: (view: { etaSeconds?: number | null } | null) => view?.etaSeconds ?? null,
  }
})
import type { Job, Movie } from '../api'

/** The jobs page is the audit trail — the render test pins the contract a
 * reader depends on: the legend count, the absence of steering controls, the
 * expandable detail with payload JSON, and the PARTIAL state for a done job
 * with failed outcomes. Callbacks are spies; assertions are on the DOM. */

function movie(path: string): Movie {
  return {
    path,
    name: (path.split('/').pop() ?? path).replace(/\.[^.]+$/, ''),
    category: 'Filme',
    duration: null,
    codec: null,
    resolution: null,
    size: 1_000_000_000,
    reclaimed_bytes: 0,
    hdr: false,
    encode: null,
  }
}

function job(overrides: Partial<Job> = {}): Job {
  return {
    id: 1,
    kind: 'encode',
    payload: { path: '/m/a', quality: true, target_vmaf: 95 },
    state: 'done',
    result: { outcomes: [{ movie: '/m/a', state: 'done', crf: 30, vmaf: 96.1 }] },
    error: null,
    created_at: '2026-10-24T10:00:00Z',
    started_at: '2026-10-24T10:00:01Z',
    finished_at: '2026-10-24T10:05:00Z',
    ...overrides,
  }
}

describe('JobsPage render', () => {
  it('renders the audit-trail legend and every history row', () => {
    render(JobsPage, {
      history: [job(), job({ id: 2, kind: 'scan', state: 'running', result: null })],
      onopenMovie: () => {},
    })
    expect(screen.getByText(/2 of 2 entries/)).toBeTruthy()
    // issue #134: the dashboard observes, it does not steer - no CANCEL
    expect(screen.queryByText('CANCEL')).toBeNull()
    expect(screen.getAllByText('DETAIL').length).toBeGreaterThan(0)
    expect(screen.getAllByText('DETAIL').length).toBe(2)
  })

  it('marks a done job with failed outcomes as PARTIAL; the dashboard renders no RETRY', () => {
    const j = job({
      state: 'done',
      result: {
        outcomes: [
          { movie: '/m/a', state: 'done' },
          { movie: '/m/b', state: 'failed', error: 'boom' },
        ],
      },
    })
    const { container } = render(JobsPage, {
      history: [j],
      onopenMovie: () => {},
    })
    // the state span says DONE · PARTIAL (the amber state text is the
    // assertion); the dashboard renders no RETRY - the absence pin below
    const stateSpan = container.querySelector('.state')!
    expect(stateSpan.textContent).toContain('Partial')
    expect(screen.queryByText('RETRY')).toBeNull()
  })

  it('a failed job says so; the dashboard renders no RETRY', () => {
    render(JobsPage, {
      history: [job({ state: 'failed', error: 'API 503: unavailable' })],
      onopenMovie: () => {},
    })
    expect(screen.queryByText('RETRY')).toBeNull()
    expect(screen.getByText(/still starting/)).toBeTruthy()
  })

  it('an interrupted job says so in amber, and its filter exists (issue #622)', () => {
    const { container } = render(JobsPage, {
      history: [job({ state: 'interrupted', error: null })],
      onopenMovie: () => {},
    })
    const stateSpan = container.querySelector('.state')!
    expect(stateSpan.textContent).toContain('Interrupted')
    // DESIGN.md's glossary: interrupted is amber, a half dot
    expect(stateSpan.querySelector('.badge.work')).toBeTruthy()
    const option = container.querySelector<HTMLOptionElement>('option[value="interrupted"]')!
    expect(option.textContent).toBe('Interrupted')
  })

  it('expands a row to show what the job did, not its raw payload (issue #632)', async () => {
    const movie = 'Legend (2015) - WEBDL-2160p HEVC AAC.mkv'
    const j = job({
      payload: { paths: [`/media/Legend (2015)/${movie}`], quality: true },
      result: {
        outcomes: [
          {
            movie,
            state: 'encoded',
            crf: 33,
            vmaf: 96.4,
            input_size: 20_000_000_000,
            output_size: 5_000_000_000,
          },
        ],
      },
    })
    const { container } = render(JobsPage, {
      history: [j],
      onopenMovie: () => {},
    })
    const toggle = container.querySelector<HTMLButtonElement>('button[aria-expanded]')!
    expect(toggle.textContent?.trim()).toBe('DETAIL')
    fireEvent.click(toggle)
    await expect.poll(() => container.querySelector('.d-table'), { timeout: 1000 }).toBeTruthy()
    const detail = container.querySelector('.detail')!
    // the operator reads the outcome, not a JSON dump
    expect(detail.textContent).toContain('CRF 33')
    expect(detail.textContent).toContain('VMAF 96.4')
    expect(detail.querySelector('.d-table')!.textContent).not.toContain('{')
    // the raw JSON is still reachable, behind the disclosure the issue asks for
    const raw = detail.querySelector('details')!
    expect(raw.open).toBe(false)
    expect(raw.textContent).toContain('"paths"')
    const expandedToggle = container.querySelector<HTMLButtonElement>('button[aria-expanded]')!
    expect(expandedToggle.getAttribute('aria-expanded')).toBe('true')
    expect(expandedToggle.textContent?.trim()).toBe('HIDE')
  })

  it('a running row states its ETA, not an ellipsis (issue #632)', () => {
    // 30 min elapsed at 25% implies 90 min left; the engine sends no ETA,
    // so the column carries the proportional estimate
    const started = new Date(Date.now() - 30 * 60_000).toISOString()
    const j = job({ state: 'running', started_at: started, finished_at: null })
    progressStub = {
      stage: 'encode',
      movie: 'a.mkv',
      path: '/m/a.mkv',
      step: 5,
      steps: 7,
      encode: { percent: 25, fps: null, avg_fps: null, eta_seconds: null },
      etaSeconds: 90 * 60,
    }
    const { container } = render(JobsPage, {
      history: [j],
      onopenMovie: () => {},
    })
    const dur = container.querySelector('.dur')!
    expect(dur.textContent).toContain('about 1 h 30 min left')
    expect(dur.textContent?.trim()).not.toBe('…')
    progressStub = null
  })

  it('a plan row shows its plans, not an empty table (issue #632 review)', async () => {
    // a plan writes `plans` where an encode writes `outcomes`; reading only
    // `outcomes` made every plan's reason unreachable
    const j = job({
      kind: 'plan',
      payload: { paths: ['/media'] },
      result: {
        plans: [
          {
            movie: 'Legend (2015) - WEBDL-2160p HEVC AAC.mkv',
            state: 'skip',
            reason: 'already efficient',
            estimated_size: 5_000_000_000,
          },
        ],
      },
    })
    const { container } = render(JobsPage, {
      history: [j],
      onopenMovie: () => {},
    })
    fireEvent.click(container.querySelector<HTMLButtonElement>('button[aria-expanded]')!)
    await expect.poll(() => container.querySelector('.d-table'), { timeout: 1000 }).toBeTruthy()
    const detail = container.querySelector('.detail')!
    expect(detail.textContent).toContain('already efficient')
    expect(detail.textContent).toContain('4.66 GiB')
  })

  it('a plan row states what the plan found, not a bare DONE (issue #685)', () => {
    const j = job({
      kind: 'plan',
      payload: { paths: ['/media'] },
      result: {
        plans: [
          { movie: '/m/a', state: 'encode', crf: 30, estimated_size: 1_000_000_000, saved_percent: 40 },
          { movie: '/m/b', state: 'encode', crf: 28, estimated_size: 2_000_000_000, saved_percent: 20 },
          { movie: '/m/c', state: 'skip', reason: 'already efficient' },
        ],
      },
    })
    const { container } = render(JobsPage, { history: [j], onopenMovie: () => {} })
    const note = container.querySelector('.note')!
    expect(note.textContent?.trim()).toBe('2 would encode · 1 already efficient')
    const state = container.querySelector('.state')!
    expect(state.textContent).toContain('Done')
    expect(state.textContent).not.toContain('Partial')
  })

  it('a plan with a failed entry is PARTIAL, with the failure named (issue #685)', () => {
    const j = job({
      kind: 'plan',
      payload: { paths: ['/media'] },
      result: {
        plans: [
          { movie: '/m/a', state: 'encode' },
          { movie: '/m/b', state: 'error', error: 'boom' },
        ],
      },
    })
    const { container } = render(JobsPage, { history: [j], onopenMovie: () => {} })
    const state = container.querySelector('.state')!
    expect(state.textContent).toContain('Partial')
    const note = container.querySelector('.error')!
    expect(note.textContent).toContain('1 of 2 failed')
    expect(note.textContent).toContain('/m/b')
  })

  it('a scan row lists the paths it could not read (issue #632 review)', async () => {
    const j = job({
      kind: 'scan',
      payload: { paths: ['/media'] },
      result: {
        movies: 3,
        categories: 1,
        errors: ['/media/broken.mkv: invalid data'],
        cancelled: false,
      },
    })
    const { container } = render(JobsPage, {
      history: [j],
      onopenMovie: () => {},
    })
    fireEvent.click(container.querySelector<HTMLButtonElement>('button[aria-expanded]')!)
    await expect.poll(() => screen.queryByText('COULD NOT READ'), { timeout: 1000 }).toBeTruthy()
    expect(container.querySelector('.detail')!.textContent).toContain('/media/broken.mkv: invalid data')
  })

  it('a search states its ETA from the samples it finished (issue #690)', () => {
    // a crf-search reports done/total, never a percent: the row states an
    // estimate from the samples already probed
    const started = new Date(Date.now() - 20 * 60_000).toISOString()
    const j = job({ kind: 'crf-search', state: 'running', started_at: started, finished_at: null })
    progressStub = {
      stage: 'crf-search',
      movie: 'a.mkv',
      path: '/m/a.mkv',
      step: 4,
      steps: 7,
      probe: { index: 1, count: 3, crf: 30, vmaf: 95.0, predicted_bytes: null, passed: false },
      probes: [],
      etaSeconds: 40 * 60,
    }
    const { container } = render(JobsPage, {
      history: [j],
      onopenMovie: () => {},
    })
    // 20 min for 1 of 3 samples implies 40 min left
    expect(container.querySelector('.dur')!.textContent).toContain('about 40 min left')
    progressStub = null
  })

  it('single-movie jobs link to the detail page', () => {
    const path = '/media/Filme/Titel 0 (2000).mkv'
    const opened: string[] = []
    const j = job({ payload: { paths: [path] } })
    const { container } = render(JobsPage, {
      history: [j],
      movies: [movie(path)],
      onopenMovie: (p) => opened.push(p),
    })
    const link = container.querySelector<HTMLButtonElement>('.file button.movie-link')
    expect(link).toBeTruthy()
    fireEvent.click(link!)
    expect(opened).toEqual([path])
  })

  it('a running per-movie stage row links to the title it names (issue #703)', () => {
    const path = '/media/Filme/Titel 0 (2000).mkv'
    progressStub = {
      stage: 'crf-search',
      movie: 'Titel 0 (2000)',
      path,
      step: 4,
      steps: 7,
      probe: { index: 1, count: 3, crf: 30, vmaf: 95.0, predicted_bytes: null, passed: false },
      probes: [],
      etaSeconds: 40 * 60,
    }
    const folder = job({
      id: 3,
      kind: 'encode',
      state: 'running',
      started_at: '2026-10-24T10:00:00Z',
      finished_at: null,
      result: null,
      payload: { paths: ['/media/Filme'], quality: true },
    })
    const { container } = render(JobsPage, {
      history: [folder],
      movies: [movie(path)],
      onopenMovie: () => {},
    })
    const link = container.querySelector<HTMLAnchorElement>('.entry.running a.movie-link')
    expect(link).toBeTruthy()
    expect(link!.getAttribute('href')).toBe(`#/movie/${encodeURIComponent(path)}`)
    expect(link!.textContent).toContain('Titel 0 (2000).mkv')
    progressStub = null
  })

  it('the folder-level row keeps showing the folder and stays unlinked (issue #703)', () => {
    const path = '/media/Filme/Titel 0 (2000).mkv'
    progressStub = {
      stage: 'crf-search',
      movie: 'Titel 0 (2000)',
      path,
      step: 4,
      steps: 7,
      probes: [],
    }
    const folder = job({
      id: 3,
      kind: 'encode',
      state: 'running',
      started_at: '2026-10-24T10:00:00Z',
      finished_at: null,
      result: null,
      payload: { paths: ['/media/Filme'], quality: true },
    })
    const { container } = render(JobsPage, {
      history: [folder],
      movies: [movie(path)],
      onopenMovie: () => {},
    })
    const file = container.querySelector('.entry.running .file')!
    expect(file.textContent).toContain('Filme')
    expect(file.querySelector('a, button')).toBeNull()
    progressStub = null
  })

  it('a directory path never links (issue #703)', () => {
    const folder = job({
      id: 4,
      kind: 'plan',
      state: 'done',
      payload: { paths: ['/media/Filme'], quality: true },
      result: { plans: [] },
    })
    const { container } = render(JobsPage, {
      history: [folder],
      movies: [movie('/media/Filme/Titel 0 (2000).mkv')],
      onopenMovie: () => {},
    })
    expect(container.querySelector('.movie-link')).toBeNull()
  })

  it('folder-scoped jobs do not claim a single movie link', () => {
    const j = job({ payload: { paths: ['/m/a', '/m/b'] } })
    const { container } = render(JobsPage, {
      history: [j],
      onopenMovie: () => {},
    })
    expect(container.querySelector('button.movie-link')).toBeNull()
  })

  it('does not list a stage row as a second job doing the same work (issue #632)', () => {
    // #602 made the folder job the runner: the CRF search writes its progress
    // to the parent (#639), so listing it too read as two jobs on one file
    const parent = job({
      id: 3,
      kind: 'encode',
      state: 'running',
      started_at: '2026-10-24T10:00:00Z',
      finished_at: null,
      payload: { paths: ['/media'] },
    })
    const stage = job({
      id: 17,
      kind: 'crf-search',
      state: 'running',
      started_at: '2026-10-24T10:00:00Z',
      finished_at: null,
      payload: { paths: ['/media/Legend (2015)/Legend.mkv'], pipeline: 'encode' },
    })
    const { container } = render(JobsPage, { history: [parent, stage], onopenMovie: () => {} })
    const ids = [...container.querySelectorAll('.entry .id')].map((el) => (el.textContent ?? '').trim())
    expect(ids).toContain('3')
    expect(ids).not.toContain('17')
  })

  it('a queued scan row claims no encode mode it cannot have (issue #630)', async () => {
    const scan = job({ id: 3, kind: 'scan', state: 'queued', started_at: null, finished_at: null, result: null, payload: { paths: ['/m/a'] } })
    const { container } = render(JobsPage, { history: [scan], onopenMovie: () => {} })
    const note = [...container.querySelectorAll('.note')].map((el) => (el.textContent ?? '').trim()).join(' ')
    expect(note).not.toContain('preset')
  })

  it('a running row at 30 % says it is working, not starting (issue #630)', () => {
    progressStub = {
      stage: 'encode',
      movie: 'a.mkv',
      path: '/m/a.mkv',
      step: 5,
      steps: 7,
      encode: { percent: 30.46, fps: null, avg_fps: null, eta_seconds: null },
    }
    const running = job({ id: 4, state: 'running', started_at: '2026-10-24T10:00:00Z', finished_at: null, result: null })
    const { container } = render(JobsPage, { history: [running], onopenMovie: () => {} })
    const text = (container.textContent ?? '').replace(/\s+/g, ' ')
    expect(text).toContain('Encode')
    expect(text).toContain('30.5%')
    expect(text).not.toContain('starting…')
    progressStub = null
  })

  it('a queued row does not invent a VMAF target the payload does not carry (issue #630)', async () => {
    // the payload has no target_vmaf: printing 95 would be a number the
    // operator reads as fact while the configured target is 96
    const queued = job({ id: 2, state: 'queued', started_at: null, finished_at: null, result: null, payload: { path: '/m/a', quality: true } })
    const { container } = render(JobsPage, { history: [queued], onopenMovie: () => {} })
    const note = [...container.querySelectorAll('.note')].map((el) => (el.textContent ?? '').trim()).join(' ')
    expect(note).toContain('quality')
    expect(note).not.toContain('VMAF>95')
  })

  it('gives the filename its own cell, never the KIND column (issue #630, case 13)', () => {
    // a long name inside the 104 px KIND column wrapped character by character
    // and drew over the start time: it needs a cell that can shrink
    const path = '/m/a-very-long-movie-name-that-would-wrap.mkv'
    const encode = job({
      id: 5,
      kind: 'encode',
      state: 'done',
      payload: { paths: [path] },
    })
    const { container } = render(JobsPage, {
      history: [encode],
      movies: [movie(path)],
      onopenMovie: () => {},
    })
    const row = container.querySelector('.entry')!
    expect(row.querySelector('.kind .movie-link')).toBeNull()
    expect(row.querySelector('.file .movie-link')).not.toBeNull()
  })

  it('a queued job shows no start time; running and done rows keep theirs (issue #603)', async () => {
    const queued = job({ id: 2, state: 'queued', started_at: null, finished_at: null, result: null })
    const running = job({ id: 3, state: 'running', started_at: '2026-10-24T10:00:01Z', finished_at: null })
    const done = job({ id: 1, state: 'done' })
    const { container } = render(JobsPage, {
      history: [queued, running, done],
      onopenMovie: () => {},
    })
    const text = (el: Element) => (el.textContent ?? '').trim()
    const rowFor = (state: string) =>
      [...container.querySelectorAll('.entry')].find((el) =>
        (el.querySelector('.state')?.textContent ?? '').trim().startsWith(state),
      )!
    // the enqueue time must not be presented as the moment work began, and
    // a queued row has no duration either — the cells stay empty rather than
    // printing a dash for a column the state cannot fill (issue #632)
    expect(text(rowFor('Queued').querySelector('.when')!)).toBe('')
    expect(text(rowFor('Queued').querySelector('.dur')!)).toBe('')
    expect(rowFor('Queued').querySelector('.when')!.getAttribute('title')).toBe('not started yet')
    expect(text(rowFor('Running').querySelector('.when')!)).toMatch(/\d/)
    expect(text(rowFor('Done').querySelector('.when')!)).toMatch(/\d/)
    // the expansion still shows all three timestamps, labeled - selected by
    // the row itself, not by fixture order
    const queuedRow = [...container.querySelectorAll('.entry')].find(
      (el) => el.querySelector('.when')?.getAttribute('title') === 'not started yet',
    )!
    fireEvent.click(queuedRow.querySelector<HTMLButtonElement>('button[aria-expanded]')!)
    await expect.poll(() => container.querySelector('.detail')).toBeTruthy()
    const stamps = [...container.querySelectorAll('.detail pre')].map((p) => p.textContent ?? '')
    const row = stamps.find((t) => t.startsWith('created ')) ?? ''
    expect(row).toContain('started —')
    expect(row).toContain('finished —')
  })
})

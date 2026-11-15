import { describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/svelte'
import JobsPage from './JobsPage.svelte'

// the running row's stage comes from the live progress store: a mutable stub
// lets one test seed it without a stream (issue #630)
let progressStub: { percent?: number; movie?: string | null; detail?: string | null } | null = null
vi.mock('../progress.svelte', () => ({ progressFor: () => progressStub }))
import type { Job } from '../api'

/** The jobs page is the audit trail — the render test pins the contract a
 * reader depends on: the legend count, the absence of steering controls, the
 * expandable detail with payload JSON, and the PARTIAL state for a done job
 * with failed outcomes. Callbacks are spies; assertions are on the DOM. */

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

  it('expands a row to show the payload JSON (the audit trail)', async () => {
    const j = job({ payload: { path: '/m/a', quality: true, target_vmaf: 95 } })
    const { container } = render(JobsPage, {
      history: [j],
      onopenMovie: () => {},
    })
    const toggle = container.querySelector<HTMLButtonElement>('button[aria-expanded]')!
    expect(toggle.textContent?.trim()).toBe('DETAIL')
    fireEvent.click(toggle)
    await expect.poll(() => screen.queryByText('PAYLOAD'), { timeout: 1000 }).toBeTruthy()
    const expandedToggle = container.querySelector<HTMLButtonElement>('button[aria-expanded]')!
    expect(expandedToggle.getAttribute('aria-expanded')).toBe('true')
    expect(expandedToggle.textContent?.trim()).toBe('HIDE')
  })

  it('single-movie jobs link to the detail page', () => {
    const j = job({ payload: { paths: ['/m/a'] } })
    const { container } = render(JobsPage, {
      history: [j],
      onopenMovie: () => {},
    })
    const link = container.querySelector<HTMLButtonElement>('button.movie-link')
    expect(link).toBeTruthy()
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
    progressStub = { percent: 30.46, movie: null, detail: null }
    const running = job({ id: 4, state: 'running', started_at: '2026-10-24T10:00:00Z', finished_at: null, result: null })
    const { container } = render(JobsPage, { history: [running], onopenMovie: () => {} })
    const text = (container.textContent ?? '').replace(/\s+/g, ' ')
    expect(text).toContain('working')
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
    const encode = job({
      id: 5,
      kind: 'encode',
      state: 'done',
      payload: { paths: ['/m/a-very-long-movie-name-that-would-wrap.mkv'] },
    })
    const { container } = render(JobsPage, { history: [encode], onopenMovie: () => {} })
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

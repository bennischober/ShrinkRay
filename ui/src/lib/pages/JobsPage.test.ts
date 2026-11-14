import { describe, expect, it } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/svelte'
import JobsPage from './JobsPage.svelte'
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
    // the enqueue time must not be presented as the moment work began
    expect(text(rowFor('Queued').querySelector('.when')!)).toBe('—')
    expect(rowFor('Queued').querySelector('.when')!.getAttribute('title')).toBe('not started yet')
    expect(text(rowFor('Running').querySelector('.when')!)).toMatch(/\d/)
    expect(text(rowFor('Done').querySelector('.when')!)).toMatch(/\d/)
    // the expansion still shows all three timestamps, labeled - selected by
    // the row itself, not by fixture order
    const queuedRow = [...container.querySelectorAll('.entry')].find(
      (el) => (el.querySelector('.when')?.textContent ?? '').trim() === '—',
    )!
    fireEvent.click(queuedRow.querySelector<HTMLButtonElement>('button[aria-expanded]')!)
    await expect.poll(() => container.querySelector('.detail')).toBeTruthy()
    const stamps = [...container.querySelectorAll('.detail pre')].map((p) => p.textContent ?? '')
    const row = stamps.find((t) => t.startsWith('created ')) ?? ''
    expect(row).toContain('started —')
    expect(row).toContain('finished —')
  })
})

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
    expect(screen.getByText(/2 OF 2 ENTRIES/)).toBeTruthy()
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
    // the state span says DONE · PARTIAL; the dashboard renders no RETRY
    // (the resident app owns job control) - the red note is the pin
    const stateSpan = container.querySelector('.state')!
    expect(stateSpan.textContent).toContain('PARTIAL')
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
})

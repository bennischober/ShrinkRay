import { describe, expect, it, vi } from 'vitest'
import { render, screen } from '@testing-library/svelte'

let mode: 'pending' | 'locked' | 'live' = 'pending'

vi.mock('./lib/api', () => ({
  api: {
    snapshot: () => {
      if (mode === 'live')
        return Promise.resolve({
          status: { watcher: 'running', worker: 'running' },
          jobs: [],
          library: [
            {
              path: '/media/Legend (2015)/Legend.mkv',
              name: 'Legend.mkv',
              category: 'C',
              size: 1024 ** 3,
              duration: '02:00:00',
              codec: 'h264',
              hdr: false,
            },
          ],
        })
      if (mode === 'locked') return Promise.reject(Object.assign(new Error('401'), { status: 401 }))
      return new Promise(() => {})
    },
    retained: () => Promise.resolve([]),
    config: () => Promise.resolve({}),
    movie: () => Promise.resolve({}),
  },
  isAuthFailure: (e: unknown) => (e as { status?: number })?.status === 401,
  isFailedOutcome: () => false,
  jobPaths: () => [],
  storeToken: () => {},
  storedToken: () => null,
  TOKEN_KEY: 'shrinkray.token',
}))

vi.mock('./lib/progress.svelte', async (importOriginal) => {
  const actual = await importOriginal<typeof import('./lib/progress.svelte')>()
  return {
    ...actual,
    connectEvents: () => {},
    progressFor: () => null,
    fileProgressFor: () => null,
    eventsConnected: () => mode === 'live',
    resetEvents: () => {},
  }
})

import App from './App.svelte'

describe('the shell data states (issue #629)', () => {
  it('renders no count and no all-clear while loading', () => {
    mode = 'pending'
    const { container } = render(App)
    expect(container.textContent).toContain('Loading library…')
    expect(container.textContent).not.toContain('nothing failed')
    expect(container.textContent).not.toContain('accounted for')
    expect(container.textContent).not.toContain('TITLES')
  })

  it('renders no page content and no badge while locked', async () => {
    mode = 'locked'
    const { container } = render(App)
    await screen.findByText('ShrinkRay needs a token')
    expect(container.textContent).not.toContain('nothing failed')
    expect(container.textContent).not.toContain('accounted for')
    expect(container.textContent).not.toContain('TITLES')
    expect(container.querySelectorAll('.rail-badge')).toHaveLength(0)
    expect(container.textContent).toContain('Locked')
  })
  it('marks the current page for assistive technology, not by colour alone (issue #636)', async () => {
    mode = 'live'
    const { container } = render(App)
    const current = container.querySelectorAll('[aria-current="page"]')
    expect(current).toHaveLength(1)
    expect(current[0].textContent).toContain('Overview')
  })
  it('restores the title from its own URL, not only from sessionStorage (issue #635)', async () => {
    mode = 'live'
    window.location.hash = '#/movie/' + encodeURIComponent('/media/Legend (2015)/Legend.mkv')
    const { container } = render(App)
    // the hero only renders on the detail page: asserting the name alone passed
    // with the fix reverted, because the library listing carries it too
    await vi.waitFor(() => expect(container.querySelector('.hero h2')).not.toBeNull(), {
      timeout: 3000,
    })
    expect(container.querySelector('.hero h2')!.textContent).toContain('Legend')
    window.location.hash = ''
  })
})

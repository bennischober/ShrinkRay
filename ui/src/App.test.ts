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
          library: [],
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

vi.mock('./lib/progress.svelte', () => ({
  connectEvents: () => {},
  progressFor: () => null,
  eventsConnected: () => mode === 'live',
  resetEvents: () => {},
}))

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
})

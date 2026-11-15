import { describe, expect, it } from 'vitest'
import { render, screen } from '@testing-library/svelte'
import LibraryPage from './LibraryPage.svelte'
import type { Movie } from '../api'

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

function renderLibrary(overrides: Record<string, unknown> = {}) {
  return render(LibraryPage, {
    movies: [movie(), movie({ path: '/m/b.mkv', name: 'b.mkv' })],
    selectedPaths: new Set<string>(),
    targetVmaf: 96,
    onopenmovie: () => {},
    ontoggle: () => {},
    onselectall: () => {},
    onclear: () => {},
    ...overrides,
  })
}

describe('LibraryPage', () => {
  // issue #630, case 12: the bar said "tick checkboxes for bulk work", but
  // ticking only sums the plan estimate — it never queues anything
  it('says what ticking a title does, not a bulk action it cannot perform', () => {
    renderLibrary()
    expect(screen.getByText(/tick titles to sum their plan estimate/)).toBeTruthy()
    expect(screen.queryByText(/bulk work/)).toBeNull()
  })
})

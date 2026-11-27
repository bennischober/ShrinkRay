import { afterEach, describe, expect, it } from 'vitest'
import { cleanup, render, screen, within } from '@testing-library/svelte'
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
  afterEach(() => cleanup())

  // issue #630, case 12: the bar said "tick checkboxes for bulk work", but
  // ticking only sums the plan estimate — it never queues anything
  it('says what ticking a title does, not a bulk action it cannot perform', () => {
    renderLibrary()
    expect(screen.getByText(/tick titles to sum their plan estimate/)).toBeTruthy()
    expect(screen.queryByText(/bulk work/)).toBeNull()
  })

  // issue #698: below 768px the cards must keep the table structure the
  // desktop table had; the roles live on divs, so CSS can never strip them
  it('pins the table, rowgroup, row, columnheader and cell roles', () => {
    renderLibrary()
    const table = screen.getByRole('table', { name: /library catalog/i })
    expect(within(table).getAllByRole('rowgroup')).toHaveLength(2)
    expect(within(table).getAllByRole('row')).toHaveLength(3)
    expect(within(table).getAllByRole('columnheader')).toHaveLength(8)
    expect(within(table).getAllByRole('cell')).toHaveLength(16)
    expect(within(table).getByRole('columnheader', { name: /Title/ })).toBeTruthy()
  })
})

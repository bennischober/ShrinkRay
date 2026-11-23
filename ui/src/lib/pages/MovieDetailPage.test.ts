import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen, waitFor } from '@testing-library/svelte'
import MovieDetailPage from './MovieDetailPage.svelte'
import { api, type Job, type Movie } from '../api'

/** Issue #616: the job trail's unlabeled time column showed a done job's
 * finish time and a running job's start time under no header. The render test
 * pins that the cell now says which moment it shows. */

vi.mock('../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api')>()
  return { ...actual, api: { ...actual.api, movie: vi.fn() } }
})

function movie(overrides: Partial<Movie> = {}): Movie {
  return {
    path: '/m/a.mkv',
    name: 'a.mkv',
    category: 'C',
    size: 1024 ** 3,
    duration: '01:00:00',
    codec: 'h264',
    resolution: [1920, 1080],
    hdr: false,
    encode: null,
    ...overrides,
  } as Movie
}

function job(overrides: Partial<Job> = {}): Job {
  return {
    id: 1,
    kind: 'encode',
    payload: { path: '/m/a.mkv' },
    state: 'done',
    result: null,
    error: null,
    created_at: '2026-10-24T09:00:00Z',
    started_at: '2026-10-24T09:00:01Z',
    finished_at: '2026-10-24T09:05:00Z',
    ...overrides,
  }
}

describe('MovieDetailPage provenance badge (issue #647)', () => {
  afterEach(() => cleanup())
  const encodeRec = (outputSize: number) => ({
    id: 1,
    preset: 'VMAF>95',
    encoded_at: '2026-11-20T10:00:00Z',
    input_size: 1000,
    output_size: outputSize,
    output_file: '/m/a.mkv',
    generation: 1,
    outcome: 'applied',
  })

  it('marks the row that produced the file being viewed only when the size matches', async () => {
    // the identity rule's UI half: the path alone still matched after a
    // restore - the badge requires the size identity too
    vi.mocked(api.movie).mockResolvedValue({
      file: movie(),
      history: [encodeRec(1000)] as never,
      plan: null,
    })
    const { container } = render(MovieDetailPage, {
      movie: movie({ size: 1000 }),
      targetVmaf: 96,
      allJobs: [],
      onclose: () => {},
      onjobs: () => {},
    })
    await waitFor(() =>
      expect(container.querySelector('.gen.current')).not.toBeNull(),
    )
  })

  it('never marks a discarded row as the file\u2019s producer', async () => {
    // the outcome half of the identity rule (issue #647, round 11): a
    // discarded attempt can carry a path claim (set_encode_output's
    // contract) whose size matches - the row is history, and the badge
    // must use the same rule db.latest_ancestry uses
    vi.mocked(api.movie).mockResolvedValue({
      file: movie({ size: 400 }),
      history: [
        { ...encodeRec(400), id: 1, generation: 1, outcome: 'discarded' },
      ] as never,
      plan: null,
    })
    const { container } = render(MovieDetailPage, {
      movie: movie({ size: 400 }),
      targetVmaf: 96,
      allJobs: [],
      onclose: () => {},
      onjobs: () => {},
    })
    await waitFor(() => expect(container.querySelector('.gen')).not.toBeNull())
    expect(container.querySelector('.gen.current')).toBeNull()
  })

  it('marks the most recent RECORD when the generation order hides it', async () => {
    // a chain (gen1 1000->500, gen2 500->400), the restore of the gen1
    // retained original, then a re-encode: the new row is generation 1
    // AGAIN and the generation-ordered history sorts it BEFORE the gen2
    // row - the badge must follow the record id (the most recent), not
    // the display position (issue #647, round 9). API order: id1/gen1,
    // id3/gen1, id2/gen2; displayed: id2, id3, id1.
    const rec = (id: number, gen: number, outSize: number) => ({
      ...encodeRec(outSize),
      id,
      generation: gen,
      output_file: '/m/a.mkv',
    })
    vi.mocked(api.movie).mockResolvedValue({
      file: movie({ size: 400 }),
      history: [rec(1, 1, 500), rec(3, 1, 400), rec(2, 2, 400)] as never,
      plan: null,
    })
    const { container } = render(MovieDetailPage, {
      movie: movie({ size: 400 }),
      targetVmaf: 96,
      allJobs: [],
      onclose: () => {},
      onjobs: () => {},
    })
    await waitFor(() => expect(container.querySelectorAll('.gen').length).toBe(3))
    const gens = [...container.querySelectorAll('.gen')]
    // displayed order: id2 (gen2), id3 (gen1 - the re-encode), id1 (gen1)
    expect(gens[0].classList.contains('current')).toBe(false)
    expect(gens[1].classList.contains('current')).toBe(true)
    expect(gens[2].classList.contains('current')).toBe(false)
  })

  it('marks only the LATEST matching row when two rows carry a coinciding claim', async () => {
    // the restore + a re-encode at the same size: both rows satisfy the
    // path+size condition, but only the newest row produced the file being
    // viewed - the older row is history (issue #647, round 8)
    vi.mocked(api.movie).mockResolvedValue({
      file: movie(),
      history: [encodeRec(500), { ...encodeRec(500), id: 2, generation: 1 }] as never,
      plan: null,
    })
    const { container } = render(MovieDetailPage, {
      movie: movie({ size: 500 }),
      targetVmaf: 96,
      allJobs: [],
      onclose: () => {},
      onjobs: () => {},
    })
    await waitFor(() => expect(container.querySelectorAll('.gen').length).toBe(2))
    const gens = [...container.querySelectorAll('.gen')]
    expect(gens[0].classList.contains('current')).toBe(true)
    expect(gens[1].classList.contains('current')).toBe(false)
  })

  it('leaves the badge off when the row\u2019s output size no longer matches', async () => {
    vi.mocked(api.movie).mockResolvedValue({
      file: movie(),
      history: [encodeRec(500)] as never,
      plan: null,
    })
    const { container } = render(MovieDetailPage, {
      movie: movie({ size: 1000 }),
      targetVmaf: 96,
      allJobs: [],
      onclose: () => {},
      onjobs: () => {},
    })
    await waitFor(() => expect(container.querySelector('.gen')).not.toBeNull())
    expect(container.querySelector('.gen.current')).toBeNull()
  })
})

describe('MovieDetailPage job trail (issue #616)', () => {
  it('states whether a trail time is a start or a finish', async () => {
    const queued = job({
      id: 3,
      state: 'queued',
      started_at: null,
      finished_at: null,
    })
    const running = job({
      id: 2,
      state: 'running',
      started_at: '2026-10-24T10:00:01Z',
      finished_at: null,
    })
    const done = job({
      id: 1,
      state: 'done',
      started_at: '2026-10-24T09:00:01Z',
      finished_at: '2026-10-24T09:05:00Z',
    })
    vi.mocked(api.movie).mockResolvedValue({ file: movie(), history: [], plan: null })
    render(MovieDetailPage, {
      movie: movie(),
      targetVmaf: 96,
      allJobs: [queued, running, done],
      onclose: () => {},
      onjobs: () => {},
    })
    const trail = screen.getByText('JOB TRAIL').closest('section')!
    await waitFor(() => expect(trail.querySelectorAll('.hist-row').length).toBe(3))
    const rows = [...trail.querySelectorAll('.hist-row')]
    const when = (row: Element) => (row.querySelector('.when')?.textContent ?? '').trim()
    expect(when(rows[0])).toBe('—')
    expect(when(rows[1])).toContain('started ')
    expect(when(rows[2])).toContain('finished ')
  })
})

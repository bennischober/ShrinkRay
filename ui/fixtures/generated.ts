import type { Job, Movie } from '../src/lib/api.js'
import type { Fixture } from '../src/lib/fixtures.js'

/** Scale scenarios are generated, not committed (issue #628): 20 000 titles of
 * invented JSON is megabytes of repository for no extra coverage, and a generator
 * keeps the shape in one place. Everything is invented — no personal data. */

const GENRES = ['Filme', 'Serien', 'Dokumentationen']
const CODECS = ['h264', 'hevc', 'vp9', 'av1']

function movie(index: number, nasty = false): Movie {
  const genre = GENRES[index % GENRES.length]
  const name = nasty
    ? `Titel ${index} — 東京 · فيلم · 🎬 · ${'x'.repeat(index % 120)}`
    : `Titel ${index} (${2000 + (index % 25)})`
  return {
    path: `/media/${genre}/${name}.mkv`,
    name,
    category: genre,
    duration: `0${1 + (index % 2)}:${String(index % 60).padStart(2, '0')}:00`,
    codec: CODECS[index % CODECS.length],
    resolution: index % 7 === 0 ? [3840, 2160] : [1920, 1080],
    size: 800_000_000 + index * 1_000_000,
    hdr: index % 11 === 0,
    encode: index % 3 === 0
      ? {
          crf: 28 + (index % 6),
          vmaf: 94 + (index % 6),
          saved_percent: 10 + (index % 50),
          preset: 'p5',
          encoder_preset: 'p5',
          input_size: 800_000_000 + index * 1_000_000,
          output_size: 600_000_000 + index * 1_000_000,
          generation: 1,
        }
      : null,
  }
}

function job(id: number, state: Job['state'], kind: Job['kind'], offsetMinutes: number): Job {
  const started = new Date(Date.now() - offsetMinutes * 60_000).toISOString()
  return {
    id,
    kind,
    state,
    created_at: started,
    started_at: state === 'queued' ? null : started,
    finished_at: state === 'done' || state === 'failed' ? started : null,
    payload: { paths: [`/media/Filme/Titel ${id} (2020).mkv`] },
    result: state === 'done' ? { encoded: 1 } : null,
    error: null,
  }
}

/** `busy`: one file mid-CRF-search, the next queued, 40 queued files, 300
 * finished jobs with mixed outcomes and a 2 000-title library. */
export function busy(): Fixture {
  const jobs: Job[] = [job(1, 'running', 'crf-search', 3)]
  for (let i = 2; i <= 41; i += 1) jobs.push(job(i, 'queued', 'encode', 0))
  for (let i = 42; i <= 341; i += 1) {
    jobs.push(job(i, i % 9 === 0 ? 'failed' : 'done', i % 4 === 0 ? 'plan' : 'encode', i))
  }
  return {
    status: {
      queue: jobs.filter((j) => j.state === 'queued' || j.state === 'running'),
      watcher: 'running',
      database: '/data/metadata.db',
      rss_mib: 61.8,
      config_error: null,
      paused: null,
      encode_window: 'open',
      reveal_available: false,
    },
    library: Array.from({ length: 2000 }, (_, i) => movie(i)),
    jobs,
    retained: [{
      source_path: '/media/Filme/Titel 7 (2020).mkv',
      retained_path: '/media/Filme/Titel 7 (2020).mkv.shrinkray-original',
      size_before: 4_200_000_000,
      size_after: 1_100_000_000,
      created_at: new Date(Date.now() - 3_600_000).toISOString(),
      present: true,
    }],
    config: { quality: { target_vmaf: 96 } },
    movie: null,
    job: null,
    events: [
      { id: 1, kind: 'crf-search', movie: 'Titel 1 (2020)', detail: 'crf 30', percent: 41 },
    ],
  }
}

/** `huge`: 20 000 titles and 1 500 jobs — scale and performance. */
export function huge(): Fixture {
  const fixture = busy()
  return {
    ...fixture,
    library: Array.from({ length: 20_000 }, (_, i) => movie(i, i % 13 === 0)),
    jobs: Array.from({ length: 1_500 }, (_, i) =>
      job(1000 + i, i % 7 === 0 ? 'failed' : 'done', i % 5 === 0 ? 'plan' : 'encode', i),
    ),
  }
}

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
    // the server's per-title library delta + output-dir savings (#646):
    // these fixture encodes are legacy/output-dir rows, so their own saving
    reclaimed_bytes: index % 3 === 0 ? 200_000_000 : 0,
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

/** A fixed instant, never `Date.now()`: a scenario whose timestamps move
 * renders different text on every run (a "3 min ago" becomes "5 min ago"), and
 * the page's width then depends on the run rather than on the layout. */
const NOW = Date.parse('2026-11-14T12:00:00Z')

function job(id: number, state: Job['state'], kind: Job['kind'], offsetMinutes: number): Job {
  const started = new Date(NOW - offsetMinutes * 60_000).toISOString()
  return {
    id,
    kind,
    state,
    created_at: started,
    started_at: state === 'queued' ? null : started,
    finished_at: state === 'done' || state === 'failed' ? started : null,
    payload: { paths: [`/media/Filme/Titel ${id} (2020).mkv`] },
    result: null,
    error: null,
  }
}

/** The finished jobs' mixed outcomes (issue #628): encoded, skipped HDR,
 * skipped on no saving, a swap declined on verification, already encoded,
 * and the failed rows. The states and reasons are the server's own
 * vocabulary (`jobs.py`), so the pages summarize them the way they would
 * summarize real work. */
function outcome(id: number): Record<string, unknown> {
  const name = `Titel ${id} (2020)`
  const variant = id % 6
  switch (variant) {
    case 0:
      return {
        outcomes: [{
          movie: name,
          state: 'encoded',
          crf: 28 + (id % 6),
          vmaf: 95.5 + (id % 4) / 10,
          input_size: 4_200_000_000,
          output_size: 1_400_000_000 + (id % 7) * 10_000_000,
          saved_percent: 60 + (id % 9),
        }],
      }
    case 1:
      return { outcomes: [{ movie: name, state: 'skipped', error: 'hdr not supported' }] }
    case 2:
      return { outcomes: [{ movie: name, state: 'skipped', error: 'output not smaller' }] }
    case 3:
      return {
        outcomes: [{
          movie: name,
          state: 'skipped',
          error: 'the swap was declined: audio track lost in verification (the encode is discarded)',
        }],
      }
    case 4:
      return { outcomes: [{ movie: name, state: 'already_encoded' }] }
    default:
      return { outcomes: [{ movie: name, state: 'failed', error: 'HandBrakeCLI exited with code 1' }] }
  }
}

/** `busy`: one file mid-CRF-search, the next queued, 40 queued files, 300
 * finished jobs with mixed outcomes and a 2 000-title library. */
export function busy(): Fixture {
  const jobs: Job[] = [job(1, 'running', 'crf-search', 3)]
  for (let i = 2; i <= 41; i += 1) jobs.push(job(i, 'queued', 'encode', 0))
  for (let i = 42; i <= 341; i += 1) {
    const finished = job(i, i % 9 === 0 ? 'failed' : 'done', i % 4 === 0 ? 'plan' : 'encode', i)
    if (finished.state === 'done') {
      // a plan writes `plans`, an encode writes `outcomes` — both keyed by
      // kind the way the server's job trail is
      finished.result = finished.kind === 'plan'
        ? {
            plans: [{
              movie: `Titel ${i} (2020)`,
              state: i % 8 === 0 ? 'skip' : 'encode',
              crf: 28 + (i % 6),
              estimated_size: 1_500_000_000,
              saved_percent: 55 + (i % 10),
              reason: i % 8 === 0 ? 'hdr not supported' : null,
            }],
          }
        : outcome(i)
    } else {
      finished.error = 'HandBrakeCLI exited with code 1'
    }
    jobs.push(finished)
  }
  const first = movie(0)
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
      created_at: new Date(NOW - 3_600_000).toISOString(),
      present: true,
    }],
    config: { quality: { target_vmaf: 96 } },
    movie: {
      file: {
        path: first.path,
        name: first.name,
        category: first.category,
        duration: first.duration,
        codec: first.codec,
        resolution: first.resolution,
        size: first.size,
        hdr: first.hdr,
        generation: 1,
      },
      history: [{
        id: 1,
        preset: 'p5',
        crf: 30,
        vmaf: 96.2,
        input_size: 4_200_000_000,
        output_size: 1_350_000_000,
        saved_percent: 67.9,
        encoder_preset: 'p5',
        input_bitrate: 9_400,
        output_bitrate: 3_000,
        generation: 1,
        outcome: 'applied',
        outcome_reason: null,
        encoded_at: new Date(NOW - 7_200_000).toISOString(),
      }],
      plan: null,
    },
    events: [
      { job_id: 1, stage: 'decision', detail: 'eligible: h264 1080p', movie: 'Titel 1 (2020)', percent: 0 },
      { job_id: 1, stage: 'crf-search', detail: 'probe crf 32', movie: 'Titel 1 (2020)', percent: 20 },
      { job_id: 1, stage: 'crf-search', detail: 'probe crf 28', movie: 'Titel 1 (2020)', percent: 45 },
      { job_id: 1, stage: 'crf-search', detail: 'probe crf 30', movie: 'Titel 1 (2020)', percent: 60 },
      { job_id: 1, stage: 'encode', detail: 'encoding at crf 30', movie: 'Titel 1 (2020)', percent: 70 },
      { job_id: 1, stage: 'encode', detail: 'encoding at crf 30', movie: 'Titel 1 (2020)', percent: 88 },
      { job_id: 1, stage: 'done', detail: 'vmaf 96.4', movie: 'Titel 1 (2020)', percent: 100 },
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

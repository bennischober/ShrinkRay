import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

// The progress store (progress.svelte.ts) is module-scoped Svelte 5 state fed
// by one EventSource on /api/events. These tests stub EventSource and drive
// the documented frame protocol: `hello` seeds/replaces the map and rebuilds
// the file model, `data` frames merge into it. The model is the ONE view every
// page renders (issue #631).

type Handler = (e: { data: string }) => void

class FakeEventSource {
  static instances: FakeEventSource[] = []
  url: string
  onopen: (() => void) | null = null
  onmessage: ((e: { data: string }) => void) | null = null
  onerror: (() => void) | null = null
  listeners: Record<string, Handler[]> = {}

  constructor(url: string) {
    this.url = url
    FakeEventSource.instances.push(this)
  }

  addEventListener(kind: string, fn: Handler) {
    ;(this.listeners[kind] ??= []).push(fn)
  }

  emit(kind: string, data: unknown) {
    for (const fn of this.listeners[kind] ?? []) fn({ data: JSON.stringify(data) })
    if (kind === 'message' && this.onmessage) this.onmessage({ data: JSON.stringify(data) })
  }

  open() {
    this.onopen?.()
  }

  fail() {
    this.onerror?.()
  }
}

const encodeEvent = {
  v: 1,
  stage: 'encode' as const,
  file: {
    movie: 'a.mkv',
    path: '/media/a.mkv',
    stage: 'encode' as const,
    step: 5,
    steps: 7,
  },
  encode: { percent: 40, fps: 12.3, avg_fps: 12.1, eta_seconds: 60 },
}

const probe = (index: number, crf: number, vmaf: number, passed: boolean) => ({
  index,
  count: 4,
  crf,
  vmaf,
  predicted_bytes: null,
  passed,
})

describe('progress store', () => {
  let mod: typeof import('./progress.svelte')

  beforeEach(async () => {
    vi.stubGlobal('EventSource', FakeEventSource)
    FakeEventSource.instances = []
    localStorage.clear()
    // a fresh module instance per test: $state is module-scoped
    vi.resetModules()
    mod = await import('./progress.svelte')
  })

  afterEach(() => {
    vi.useRealTimers()
    vi.unstubAllGlobals()
  })

  it('connects once (idempotent) to /api/events', () => {
    mod.connectEvents()
    mod.connectEvents()
    mod.connectEvents()
    expect(FakeEventSource.instances).toHaveLength(1)
    expect(FakeEventSource.instances[0].url).toBe('/api/events')
    expect(mod.eventsConnected()).toBe(false)
    FakeEventSource.instances[0].open()
    expect(mod.eventsConnected()).toBe(true)
  })

  it('adds the token query parameter when one is stored (issue #36)', () => {
    localStorage.setItem('shrinkray.token', 's3cret')
    mod.connectEvents()
    expect(FakeEventSource.instances[0].url).toBe('/api/events?token=s3cret')
  })

  it('hello replaces the whole map and rebuilds the probe trail', () => {
    vi.useFakeTimers()
    vi.setSystemTime(1000)
    mod.connectEvents()
    const es = FakeEventSource.instances[0]
    es.open()
    es.emit('hello', {
      jobs: {
        '1': {
          v: 1,
          stage: 'crf-search',
          stage_job_id: 2,
          run: { done: 3, total: 12 },
          file: {
            movie: 'a.mkv',
            path: '/media/a.mkv',
            stage: 'crf-search',
            step: 4,
            steps: 7,
          },
          probe: probe(2, 24, 96.6, true),
          probes: [probe(1, 30, 94.8, false), probe(2, 24, 96.6, true)],
          target_vmaf: 96,
        },
      },
    })
    expect(mod.progressFor(1)?.run).toEqual({ done: 3, total: 12 })
    const view = mod.fileProgressFor(1)
    expect(view?.probes).toHaveLength(2)
    expect(view?.probe?.crf).toBe(24)
    // a later quiet hello clears stale entries
    es.emit('hello', { jobs: {} })
    expect(mod.progressFor(1)).toBeNull()
    expect(mod.fileProgressFor(1)).toBeNull()
  })

  it('data frames merge and a percent tick never erases the file counter', () => {
    mod.connectEvents()
    const es = FakeEventSource.instances[0]
    es.open()
    es.emit('message', {
      job_id: 7,
      v: 1,
      stage: 'crf-search',
      stage_job_id: 8,
      run: { done: 3, total: 12 },
      file: { movie: 'a.mkv', path: '/media/a.mkv', stage: 'crf-search', step: 4, steps: 7 },
      probe: probe(1, 30, 94.8, false),
    })
    es.emit('message', { ...encodeEvent, job_id: 7, stage_job_id: 8 })
    const view = mod.fileProgressFor(7)
    expect(view?.run).toEqual({ done: 3, total: 12 })
    expect(view?.probes).toHaveLength(1)
    expect(view?.stage).toBe('encode')
    expect(view?.step).toBe(5)
    expect(view?.encode?.percent).toBe(40)
    expect(mod.progressFor(null)).toBeNull()
    expect(mod.progressFor(999)).toBeNull()
  })

  it('a stage row event keeps its own job_id and does not feed the file model', () => {
    mod.connectEvents()
    const es = FakeEventSource.instances[0]
    es.open()
    es.emit('message', {
      job_id: 9,
      v: 1,
      stage: 'crf-search',
      stage_job_id: 9,
      file: { movie: 'a.mkv', path: '/a.mkv', stage: 'crf-search', step: 4, steps: 7 },
      probe: probe(1, 30, 94.8, false),
    })
    expect(mod.progressFor(9)?.probe?.vmaf).toBe(94.8)
    expect(mod.fileProgressFor(9)).toBeNull()
  })

  it('an error flips connected off (the poll keeps the UI correct)', () => {
    mod.connectEvents()
    const es = FakeEventSource.instances[0]
    es.open()
    expect(mod.eventsConnected()).toBe(true)
    es.fail()
    expect(mod.eventsConnected()).toBe(false)
  })

  it('an advancing encode gets a rate estimate, not before two ticks', () => {
    vi.useFakeTimers()
    vi.setSystemTime(0)
    mod.connectEvents()
    const es = FakeEventSource.instances[0]
    es.open()
    es.emit('message', { ...encodeEvent, job_id: 1, stage_job_id: 2, encode: { percent: 20, eta_seconds: null } })
    expect(mod.encodeEta(mod.fileProgressFor(1))).toBeNull()
    vi.setSystemTime(60_000)
    es.emit('message', { ...encodeEvent, job_id: 1, stage_job_id: 2, encode: { percent: 40, eta_seconds: null } })
    // 20% in 60 s => 3 s per percent => 60% left = 180 s
    expect(mod.encodeEta(mod.fileProgressFor(1))).toBe(180)
  })

  it('the encoder ETA is smoothed instead of jumping', () => {
    expect(mod.smoothEta(null, 1000)).toBe(1000)
    expect(mod.smoothEta(1000, 2000)).toBe(1300)
  })

  it('a CRF search time-left needs two advancing probes and a known count', () => {
    vi.useFakeTimers()
    vi.setSystemTime(0)
    mod.connectEvents()
    const es = FakeEventSource.instances[0]
    es.open()
    es.emit('message', {
      job_id: 1,
      v: 1,
      stage: 'crf-search',
      stage_job_id: 2,
      file: { movie: 'a.mkv', path: '/a.mkv', stage: 'crf-search', step: 4, steps: 7 },
      probe: probe(1, 30, 94.8, false),
    })
    expect(mod.probeEta(mod.fileProgressFor(1))).toBeNull()
    vi.setSystemTime(60_000)
    es.emit('message', {
      job_id: 1,
      v: 1,
      stage: 'crf-search',
      stage_job_id: 2,
      file: { stage: 'crf-search', step: 4 },
      probe: probe(2, 24, 96.6, true),
    })
    // 60 s per probe, 2 of 4 done => 2 left = 120 s
    expect(mod.probeEta(mod.fileProgressFor(1))).toBe(120)
  })

  it('the canonical summary is the same words every page renders', () => {
    vi.useFakeTimers()
    vi.setSystemTime(0)
    mod.connectEvents()
    const es = FakeEventSource.instances[0]
    es.open()
    es.emit('message', { ...encodeEvent, job_id: 1, stage_job_id: 2 })
    const view = mod.fileProgressFor(1)
    expect(mod.workSummary(view, Date.now(), false)).toBe(
      'Encode · 40.0% · 12.3 fps · about 1 min left',
    )
    expect(mod.workSummary(view, Date.now(), true)).toContain('stale')
  })

  it('the CRF summary names the probe position, not a fabricated percent', () => {
    const view = {
      jobId: 1,
      stage: 'crf-search' as const,
      movie: 'a.mkv',
      path: '/a.mkv',
      step: 4,
      steps: 7,
      run: { done: 3, total: 12 },
      probe: { ...probe(2, 24, 96.6, true), count: 4 },
      probes: [probe(1, 30, 94.8, false), probe(2, 24, 96.6, true)],
      targetVmaf: 96,
      chosen: null,
      encode: null,
      sample: null,
      detail: null,
      etaSmoothed: null,
      encodeTicks: [],
      probeTicks: [],
    }
    expect(mod.probePosition(view)).toBe('probe 2 of 4')
    expect(mod.workSummary(view, Date.now(), false)).toBe('CRF search · probe 2 of 4')
  })

  it('the chosen line uses the issue wording (issue #631)', () => {
    const view = {
      jobId: 1,
      stage: 'encode' as const,
      movie: 'a.mkv',
      path: '/a.mkv',
      step: 5,
      steps: 7,
      run: null,
      probe: null,
      probes: [],
      targetVmaf: 96,
      chosen: { crf: 26, vmaf: 96.2, predicted_bytes: 9_400_000_000, reason: null },
      encode: null,
      sample: null,
      detail: null,
      etaSmoothed: null,
      encodeTicks: [],
      probeTicks: [],
    }
    expect(mod.chosenLine(view, 96, 25_000_000_000)).toBe(
      'Found CRF 26 — VMAF 96.2 · predicted 8.75 GiB (−62 %)',
    )
    expect(
      mod.chosenLine(
        { ...view, chosen: { crf: 30, vmaf: 94.8, predicted_bytes: null, reason: 'no size gain' } },
        96,
        25_000_000_000,
      ),
    ).toBe('Will not encode — no setting saves space at this quality')
  })
})

describe('queue outlook', () => {
  it('counts the files waiting, not the jobs', async () => {
    const { queuedFileCount } = await import('./format')
    expect(
      queuedFileCount([
        { state: 'queued', payload: { paths: ['/a', '/b'] } },
        { state: 'queued', payload: { paths: ['/c'] } },
        { state: 'running', payload: { paths: ['/d'] } },
      ]),
    ).toBe(3)
  })

  it('shows a rough time only with measured history', async () => {
    const { queuePaceSeconds } = await import('./format')
    const movie = (seconds: number) => ({
      path: '/a',
      name: 'a',
      category: 'c',
      duration: null,
      codec: null,
      resolution: null,
      size: 1024 ** 3,
      reclaimed_bytes: 0,
      hdr: false,
      encode: {
        crf: 24,
        vmaf: 96,
        saved_percent: 50,
        preset: 'p5',
        encode_seconds: seconds,
        input_size: 1024 ** 3,
        output_size: 512 * 1024 ** 2,
      },
    })
    expect(queuePaceSeconds(3, [])).toBeNull()
    // 3 files of 1 GiB each at 3600 s/GiB => 10800 s
    expect(queuePaceSeconds(3, [movie(3600)])).toBe(3 * 3600)
  })
})

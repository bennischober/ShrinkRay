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

  it('hello recomputes the encode ETA after reload (issue #604)', () => {
    vi.useFakeTimers()
    vi.setSystemTime(0)
    mod.connectEvents()
    const es = FakeEventSource.instances[0]
    es.open()
    es.emit('hello', {
      jobs: {
        '1': {
          v: 1,
          stage: 'encode',
          stage_job_id: 2,
          run: { done: 3, total: 12 },
          file: {
            movie: 'a.mkv',
            path: '/media/a.mkv',
            stage: 'encode',
            step: 5,
            steps: 7,
          },
          encode: { percent: 50, fps: 12.3, avg_fps: 12.1, eta_seconds: 7200 },
        },
      },
    })
    const view = mod.fileProgressFor(1)
    // the estimate is recomputed from the seed, not persisted client state
    expect(mod.progressEta(view)).toBe(7200)
  })

  it('ignores an event from another schema version (PROGRESS_VERSION)', () => {
    mod.connectEvents()
    const es = FakeEventSource.instances[0]
    es.open()
    es.emit('message', {
      job_id: 1,
      v: 2,
      stage: 'encode',
      file: { movie: 'old.mkv', path: '/old.mkv', stage: 'encode', step: 5, steps: 7 },
      encode: { percent: 99 },
    })
    expect(mod.progressFor(1)).toBeNull()
    es.emit('message', {
      job_id: 1,
      v: 1,
      stage: 'encode',
      stage_job_id: 2,
      file: { movie: 'new.mkv', path: '/new.mkv', stage: 'encode', step: 5, steps: 7 },
      encode: { percent: 10 },
    })
    expect(mod.progressFor(1)?.v).toBe(1)
    expect(mod.fileProgressFor(1)?.movie).toBe('new.mkv')
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

  it('carries cache provenance and renders the reused wording (issue #631)', () => {
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
      run: { done: 0, total: 2 },
      file: {
        movie: 'A.mkv',
        path: '/media/A.mkv',
        stage: 'crf-search',
        step: 4,
        steps: 7,
      },
      target_vmaf: 96,
      probe: {
        index: 2,
        count: 2,
        crf: 24,
        vmaf: 96.6,
        predicted_bytes: null,
        passed: true,
        cached: true,
        measured_at: '2026-11-14 08:12:00',
      },
      probes: [
        {
          index: 1,
          count: 2,
          crf: 30,
          vmaf: 94.8,
          predicted_bytes: null,
          passed: false,
          cached: true,
          measured_at: '2026-11-14 08:12:00',
        },
        {
          index: 2,
          count: 2,
          crf: 24,
          vmaf: 96.6,
          predicted_bytes: null,
          passed: true,
          cached: true,
          measured_at: '2026-11-14 08:12:00',
        },
      ],
      chosen: {
        crf: 24,
        vmaf: 96.6,
        predicted_bytes: 1_400_000_000,
        reason: null,
        source: 'cache',
      },
      source: 'cache',
      measured_at: '2026-11-14 08:12:00',
    })
    const view = mod.fileProgressFor(1)
    expect(view?.source).toBe('cache')
    expect(view?.measuredAt).toBe('2026-11-14 08:12:00')
    expect(mod.reusedLabel(view)).toContain('reused from the plan cache (measured')
    expect(mod.reusedLabel(view)).toContain('Nov')
    expect(mod.workSummary(view, Date.now(), false)).toContain('reused')
    expect(mod.reusedLabel({ ...view!, source: 'plan' })).toContain(
      'reused from the plan (measured',
    )
  })

  it('a new file drops the previous file trail and chosen (issue #631)', () => {
    vi.useFakeTimers()
    vi.setSystemTime(0)
    mod.connectEvents()
    const es = FakeEventSource.instances[0]
    es.open()
    const frame = (data: Record<string, unknown>) => ({ job_id: 1, ...data })

    // file A completes: the real server-merged frames carry its trail + chosen
    es.emit(
      'message',
      frame({
        v: 1,
        stage: 'decision',
        stage_job_id: 2,
        run: { done: 0, total: 2 },
        file: {
          movie: 'A.mkv',
          path: '/media/A.mkv',
          stage: 'decision',
          step: 1,
          steps: 7,
        },
      }),
    )
    // the server's merge accumulates `probes` on every frame, so the real
    // frames carry the trail arrays, not just the latest `probe`
    const aProbes: Record<string, unknown>[] = []
    for (const [index, crf, vmaf, passed] of [
      [1, 30, 94.8, false],
      [2, 24, 96.6, true],
    ] as const) {
      const probe = {
        index,
        count: 2,
        crf,
        vmaf,
        predicted_bytes: null,
        passed,
      }
      aProbes.push(probe)
      es.emit(
        'message',
        frame({
          v: 1,
          stage: 'crf-search',
          stage_job_id: 2,
          run: { done: 0, total: 2 },
          file: {
            movie: 'A.mkv',
            path: '/media/A.mkv',
            stage: 'crf-search',
            step: 4,
            steps: 7,
          },
          target_vmaf: 96,
          probe,
          probes: [...aProbes],
        }),
      )
    }
    es.emit(
      'message',
      frame({
        v: 1,
        stage: 'crf-search',
        stage_job_id: 2,
        file: { stage: 'crf-search', step: 4 },
        target_vmaf: 96,
        probe: {
          index: 2,
          count: 2,
          crf: 24,
          vmaf: 96.6,
          predicted_bytes: null,
          passed: true,
        },
        probes: [...aProbes],
        chosen: { crf: 24, vmaf: 96.6, predicted_bytes: 1_400_000_000, reason: null },
      }),
    )
    let view = mod.fileProgressFor(1)
    expect(view?.movie).toBe('A.mkv')
    expect(view?.probes).toHaveLength(2)
    expect(view?.chosen?.crf).toBe(24)

    // B's decision is the real server merge: no probes/chosen/encode/sample/detail
    es.emit(
      'message',
      frame({
        v: 1,
        stage: 'decision',
        stage_job_id: 3,
        run: { done: 1, total: 2 },
        file: {
          movie: 'B.mkv',
          path: '/media/B.mkv',
          stage: 'decision',
          step: 1,
          steps: 7,
        },
      }),
    )
    view = mod.fileProgressFor(1)
    expect(view?.movie).toBe('B.mkv')
    expect(view?.stage).toBe('decision')
    expect(view?.probes).toEqual([])
    expect(view?.probe).toBeNull()
    expect(view?.chosen).toBeNull()
    expect(view?.encode).toBeNull()
    expect(view?.sample).toBeNull()
    expect(view?.detail).toBeNull()

    // B's probe-start must not resurrect A's trail either
    es.emit(
      'message',
      frame({
        v: 1,
        stage: 'crf-search',
        stage_job_id: 3,
        file: {
          movie: 'B.mkv',
          path: '/media/B.mkv',
          stage: 'crf-search',
          step: 4,
          steps: 7,
        },
        target_vmaf: 96,
        probe: {
          index: 1,
          count: null,
          crf: 28,
          vmaf: null,
          predicted_bytes: null,
          passed: null,
        },
        detail: 'probe encode (CRF 28)',
      }),
    )
    view = mod.fileProgressFor(1)
    expect(view?.movie).toBe('B.mkv')
    expect(view?.stage).toBe('crf-search')
    expect(view?.probes).toEqual([])
    expect(view?.chosen).toBeNull()
    expect(view?.probe?.crf).toBe(28)
    expect(view?.probe?.vmaf).toBeNull()
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
      source: null,
      measuredAt: null,
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
      source: null,
      measuredAt: null,
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
    const recent = (seconds: number) => ({
      encode_seconds: seconds,
      input_size: 1024 ** 3,
    })
    expect(queuePaceSeconds(3, [])).toBeNull()
    // 3 files of 1 GiB each at 3600 s/GiB => 10800 s
    expect(queuePaceSeconds(3, [recent(3600)])).toBe(3 * 3600)
  })

  it('uses the NEWEST five encodes, not an alphabetical window', async () => {
    const { queuePaceSeconds } = await import('./format')
    const recent = (seconds: number) => ({
      encode_seconds: seconds,
      input_size: 1024 ** 3,
    })
    // newest first: the five 60 s encodes, then the old 9999 s one
    expect(
      queuePaceSeconds(1, [
        recent(60),
        recent(60),
        recent(60),
        recent(60),
        recent(60),
        recent(9999),
      ]),
    ).toBe(60)
  })
})

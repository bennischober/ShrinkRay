<script lang="ts">
  import { onMount } from 'svelte'
  import {
    api,
    fmtGb,
    isAuthFailure,
    jobPaths,
    storeToken,
    type Movie,
    type RetainedFile,
    type Status,
    type Job,
  } from './lib/api'
  import OverviewPage from './lib/pages/OverviewPage.svelte'
  import LibraryPage from './lib/pages/LibraryPage.svelte'
  import JobsPage from './lib/pages/JobsPage.svelte'
  import ConfigPage from './lib/pages/ConfigPage.svelte'
  import RetainedPage from './lib/pages/RetainedPage.svelte'
  import MovieDetailPage from './lib/pages/MovieDetailPage.svelte'
  import { connectEvents, progressFor } from './lib/progress.svelte'

  type Page = 'overview' | 'library' | 'jobs' | 'retained' | 'config'

  let status: Status | null = $state(null)
  let movies: Movie[] = $state([])
  let retainedFiles: RetainedFile[] = $state([])
  let history: Job[] = $state([])
  let error = $state<string | null>(null)
  /** The server requires a bearer token and none is stored (issue #36):
   * the UI asks for it instead of showing a red error strip. */
  let needsToken = $state(false)
  let tokenInput = $state('')
  /** Informational feedback ("already queued") — never red; red is for
   * failure only (the console's own Red Lock Rule). */
  let notice = $state<string | null>(null)

  // theme: dark default, toggle persisted
  let theme = $state<'dark' | 'light'>(
    localStorage.getItem('ms.theme') === 'light' ? 'light' : 'dark'
  )
  $effect(() => {
    document.documentElement.dataset.theme = theme
    localStorage.setItem('ms.theme', theme)
  })

  // ── routing: hash-based, so reload/bookmark keeps the position ──
  const PAGES: Page[] = ['overview', 'library', 'jobs', 'retained', 'config']
  function pageFromHash(): Page {
    const h = location.hash.replace(/^#\/?/, '')
    return (PAGES as string[]).includes(h) ? (h as Page) : 'overview'
  }
  let page = $state<Page>(pageFromHash())
  // the single navigation gate: any page change closes the movie detail
  // (the detail page replaces the page stack, so a rail click that only
  // changed `page` behind the overlay looked like nothing happened)
  let detailMovie = $state<Movie | null>(null)
  // where the detail was opened from — the back button returns there
  let detailFrom = $state<'overview' | 'library' | 'jobs'>('overview')
  function goto(next: Page) {
    detailMovie = null
    detailPath = null
    sessionStorage.removeItem('sr.detail')
    page = next
    location.hash = `/${next}`
  }
  function openDetail(movie: Movie, from: 'overview' | 'library' | 'jobs') {
    detailMovie = movie
    detailFrom = from
    detailPath = null
    // deep link: the hash stays routable (#/movie), the path rides in
    // sessionStorage so F5 on a detail page restores the same title
    sessionStorage.setItem('sr.detail', movie.path)
    location.hash = '/movie'
  }
  function closeDetail() {
    detailMovie = null
    detailPath = null
    sessionStorage.removeItem('sr.detail')
    page = detailFrom
    location.hash = `/${detailFrom}`
    // Escape must not drop focus to BODY (round-6 P3, measured): the page
    // we return to has a rail item — keyboard users resume there, same
    // contract as the dialogs restoring their trigger
    queueMicrotask(() => {
      const items = document.querySelectorAll<HTMLElement>('.rail-item')
      items[detailFrom === 'overview' ? 0 : detailFrom === 'jobs' ? 3 : 1]?.focus()
    })
  }
  // F5 on #/movie: restore the detail from sessionStorage (the poll may
  // not have delivered the library yet — resolve lazily when it arrives)
  if (pageFromHash() === 'overview' && location.hash === '#/movie') {
    page = 'library'
  }
  let detailPath = $state<string | null>(
    location.hash === '#/movie' ? sessionStorage.getItem('sr.detail') : null
  )

  // back/forward support: hash is the single source of truth for pages,
  // and leaving #/movie via the BROWSER (not the back button) must unmount
  // the detail — otherwise the hash says library while a detail page is
  // still mounted (a zombie the rail clicks can't explain)
  window.addEventListener('hashchange', () => {
    if (location.hash === '#/movie') {
      // Forward onto a dead deep link (detail closed, session gone):
      // settle on the overview instead of a zombie hash
      if (!detailMovie && !sessionStorage.getItem('sr.detail')) {
        window.history.replaceState(null, '', '#/overview')
        page = 'overview'
        return
      }
      // F5-restore case: park on the library until it arrives and the
      // deep-link effect resolves sr.detail
      page = 'library'
      return
    }
    if (detailMovie) {
      detailMovie = null
      sessionStorage.removeItem('sr.detail')
    }
    page = pageFromHash()
  })

  // ── keyboard layer: 1-5 switch pages, / focuses search ──
  const SHORTCUT_LEGEND = '1–5: pages · /: search'
  function handleShortcuts(e: KeyboardEvent) {
    // never hijack typing in inputs
    const t = e.target as HTMLElement
    if (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.tagName === 'SELECT') return
    if (e.metaKey || e.ctrlKey || e.altKey) return
    const map: Record<string, Page> = { '1': 'overview', '2': 'library', '3': 'jobs', '4': 'config' }
    if (map[e.key]) {
      goto(map[e.key])
    } else if (e.key === '/') {
      e.preventDefault()
      goto('library')
      queueMicrotask(() => document.querySelector<HTMLInputElement>('.search')?.focus())
    }
  }

  // ── job parameters (persisted defaults; bars never persist) ──
  let scanPaths = $state<string[]>(
    JSON.parse(localStorage.getItem('ms.paths') ?? '["D:\\\\Movies"]')
  )
  let outputDir = $state(localStorage.getItem('ms.out') ?? 'D:\\Encoded')
  let targetVmaf = $state(Number(localStorage.getItem('ms.vmaf') ?? 96))
  let workers = $state<number | null>(
    localStorage.getItem('ms.workers') ? Number(localStorage.getItem('ms.workers')) : null
  )
  // per-run arming of the irreversible bar crop — never persisted, so a
  // fresh session always starts unarmed

  function persist() {
    localStorage.setItem('ms.paths', JSON.stringify(scanPaths))
    localStorage.setItem('ms.out', outputDir)
    localStorage.setItem('ms.vmaf', String(targetVmaf))
    if (workers != null) localStorage.setItem('ms.workers', String(workers))
    else localStorage.removeItem('ms.workers')
  }

  // movies inside any scan path (case-insensitive prefix) — used for the
  // bars blast-radius when the targets are the whole library
  const barsAffected = $derived(
    movies.filter((m) =>
      scanPaths.some((p) => m.path.toLowerCase().startsWith(p.toLowerCase()))
    ).length
  )

  // ── plan/encode results of the latest finished jobs ──
  interface PlanEntry {
    movie: string
    state: string
    crf?: number | null
    vmaf?: number | null
    estimated_size?: number
    saved_percent?: number
    reason?: string
    error?: string
  }
  const lastPlan = $derived(
    history.find((j) => j.kind === 'plan' && j.state === 'done' && j.result)
  )
  const planEntries = $derived(
    (lastPlan?.result as { plans?: PlanEntry[] } | null)?.plans ?? []
  )

  // ── selection (bulk ops live on the library page) ──
  let selectedPaths = $state<Set<string>>(new Set())

  // ── resident-memory samples (the overview's sparkline buffer) ──
  // Accumulated in the poll path, never in an $effect: an effect that
  // read AND wrote its buffer looped itself to death (round-6.6). Pure
  // write-on-new-sample; capped at 40 (the sparkline's window).
  let rssSamples = $state<number[]>([])
  function rememberRss(v: number | null | undefined) {
    if (v == null) return
    rssSamples = [...rssSamples, v].slice(-40)
  }

  // ── refresh loop ──
  async function refresh() {
    try {
      const snap = await api.snapshot()
      status = snap.status
      if (snap.jobs !== null) history = snap.jobs // unchanged: keep the current array
      if (snap.library !== null) movies = snap.library // unchanged: keep the current array
      rememberRss(snap.status?.rss_mib)
      error = null
      needsToken = false
    } catch (e) {
      // a 401 is not an error to report: the server requires a token, so
      // the UI asks for one instead of showing a red strip (issue #36)
      if (isAuthFailure(e)) {
        needsToken = true
        return
      }
      error = e instanceof Error ? e.message : String(e)
    }
  }

  /** Stores the pasted token and retries immediately (issue #36). */
  function submitToken() {
    storeToken(tokenInput.trim())
    tokenInput = ''
    needsToken = false
    void refresh()
  }

  /** Loads the retention ledger (issue #35). */
  async function loadRetained() {
    try {
      retainedFiles = await api.retained()
    } catch (e) {
      if (isAuthFailure(e)) needsToken = true
    }
  }

  // deep-link restore: a reload on #/movie re-opens the detail as soon as
  // the library arrives (the path rides in sessionStorage). The origin is
  // not knowable after a reload — library is the honest default, since a
  // fresh session has no page history to return to. openDetailFromJobs
  // presets the origin it wants ('jobs') before the library arrives.
  let pendingDetailFrom: 'library' | 'jobs' = 'library'
  $effect(() => {
    if (detailPath && movies.length > 0) {
      const m = movies.find((mv) => mv.path === detailPath)
      if (m) {
        detailMovie = m
        detailFrom = pendingDetailFrom
      }
      detailPath = null
      pendingDetailFrom = 'library'
    }
  })

  // adaptive polling: one snapshot request per tick, fast (1s) while work
  // is queued/running, relaxed (3s) when idle, paused entirely while the
  // tab is hidden (the browser throttles it anyway — this is explicit)
  const POLL_ACTIVE = 1000
  const POLL_IDLE = 3000
  let pollTimer: ReturnType<typeof setTimeout> | null = null
  async function poll() {
    await refresh()
    const busy = history.some((j) => j.state === 'queued' || j.state === 'running')
    const hidden = document.hidden
    schedule(busy ? POLL_ACTIVE : POLL_IDLE, hidden)
  }
  function schedule(interval: number, hidden: boolean) {
    pollTimer = setTimeout(poll, hidden ? 10000 : interval)
  }

  // pending: global click-guard + per-kind queue guard (no job spam: a kind
  // already queued/running disables its enqueue buttons)
  let pendingKind = $state<'scan' | 'plan' | 'encode' | 'cancel' | null>(null)
  const kindQueued = $derived(
    (kind: 'scan' | 'plan' | 'encode') =>
      history.some((j) => j.kind === kind && (j.state === 'queued' || j.state === 'running'))
  )

  // ── the encode-all preflight: the loudest verb gets a briefing ──
  // One click on ENCODE ALL would queue the whole library (354 titles,
  // 3.6 TB, days of work). The one-line strip states scope + measured
  // throughput before the verb fires; the safe exit returns focus.
  let pendingEncodeAll = $state(false)
  let encodeAllEl: HTMLDivElement | null = $state(null)
  let encodeAllTrigger: HTMLElement | null = null
  $effect(() => {
    if (pendingEncodeAll) encodeAllEl?.focus()
  })
  const encodeAllTitles = $derived(movies.length)
  const encodeAllSizeGb = $derived(movies.reduce((sum, m) => sum + (m.size ?? 0), 0) / 1024 ** 3)
  /** Measured throughput from schema-v5 data (bytes per encode-second,
   * averaged over real runs) — 'measured, not guessed' applied to the
   * estimate itself. The label carries its sample size: extrapolating
   * two episodes to 3.6 TB without saying so would be the false
   * precision the console refuses elsewhere. null when unmeasured. */
  const encodeAllEstLabel = $derived.by(() => {
    const done = movies.filter((m) => m.encode?.encode_seconds != null && m.encode.encode_seconds > 0)
    if (done.length === 0) return null
    const outBytes = done.reduce((sum, m) => sum + (m.encode?.output_size ?? 0), 0)
    const outSeconds = done.reduce((sum, m) => sum + (m.encode?.encode_seconds ?? 0), 0)
    if (outBytes <= 0 || outSeconds <= 0) return null
    const seconds = (encodeAllSizeGb * 1024 ** 3 * (outSeconds / outBytes))
    const h = Math.round(seconds / 3600)
    const span = h >= 48 ? `~${(h / 24).toFixed(0)} days` : h >= 1 ? `~${h} h` : '~under an hour'
    // n=1 is the weakest possible basis: with a single sample the projection
    // inherits one title's every quirk (a 43 MB episode vs a 40 GB remux),
    // so it must not wear the confident "measured throughput" voice —
    // same number, but the sentence says what it is
    if (done.length === 1) {
      const basisGb = done[0].size ?? done[0].encode?.output_size ?? 0
      // the missing wall-time figure gets its explanation — silence next
      // to a multi-TB scope read as an oversight, not an honesty rule
      return basisGb > 0
        ? `rough extrapolation from a single ${fmtGb(basisGb)} title — real runs will differ; basis too thin to project wall time`
        : 'rough extrapolation from a single title — real runs will differ; basis too thin to project wall time'
    }
    return `${span} at measured throughput (basis: ${done.length} titles)`
  })
  function requestEncodeAll() {
    encodeAllTrigger = document.activeElement as HTMLElement | null
    pendingEncodeAll = true
  }
  function closeEncodeAll() {
    pendingEncodeAll = false
    encodeAllTrigger?.focus()
    encodeAllTrigger = null
  }
  function encodeAllKeydown(e: KeyboardEvent) {
    if (e.key === 'Escape') {
      e.preventDefault()
      closeEncodeAll()
      return
    }
    if (e.key === 'Tab' && encodeAllEl) {
      const focusables = encodeAllEl.querySelectorAll<HTMLElement>('button:not(:disabled)')
      if (focusables.length < 2) return
      const first = focusables[0]
      const last = focusables[focusables.length - 1]
      if (e.shiftKey && document.activeElement === first) {
        e.preventDefault()
        last.focus()
      } else if (!e.shiftKey && document.activeElement === last) {
        e.preventDefault()
        first.focus()
      }
    }
  }

  // ── the encode-all confirmation: the single gate for a whole-library run ──
  // Only ENCODE ALL asks, because it is the one action that can touch every
  // title at once. Bar removal is not a choice any more (issue #128): every
  // encode crops the bars it finds, so there is nothing to arm.


  async function enqueue(
    kind: 'scan' | 'plan' | 'encode',
    paths?: string[],
  ) {
    if (pendingKind !== null) return
    if (kindQueued(kind)) {
      // informational, not an error — it gets the quiet notice channel,
      // not the red alert strip (red is for failure only)
      notice = `${kind} is already queued — it runs next`
      return
    }
    const targets = paths ?? scanPaths
    if (targets.length === 0) {
      notice = paths ? 'select at least one movie' : 'at least one movie folder is required'
      return
    }
    pendingKind = kind
    persist()
    try {
      await api.enqueue({
        kind,
        path: targets[0],
        paths: targets.slice(1),
        output_dir: kind === 'encode' ? outputDir || undefined : undefined,
        quality: kind !== 'scan',
        target_vmaf: Math.min(100, Math.max(70, Number(targetVmaf) || 96)),
        workers: kind === 'scan' ? workers : null,
      })
      await refresh()
    } catch (e) {
      error = e instanceof Error ? e.message : String(e)
    } finally {
      pendingKind = null
    }
  }

  async function cancelJob(id: number | null) {
    if (id === null || pendingKind !== null) return
    pendingKind = 'cancel'
    try {
      await api.cancel(id)
      await refresh()
    } catch (e) {
      error = e instanceof Error ? e.message : String(e)
    } finally {
      pendingKind = null
    }
  }

  async function toggleWatcher() {
    if (!status) return
    try {
      if (status.watcher === 'running') {
        await api.watch('stop')
      } else {
        await api.watch('start', scanPaths[0], outputDir)
      }
      await refresh()
    } catch (e) {
      error = e instanceof Error ? e.message : String(e)
    }
  }

  // retry: re-enqueue the job with ITS OWN scope and parameters (never the
  // current form — a failed single-movie encode must not become a
  // whole-library run)
  async function retryJob(job: Job) {
    if (pendingKind !== null) return
    // a row of a removed kind is history only: it has no payload to replay
    if (job.kind === 'legacy') return
    // replay the STORED payload verbatim (issue #100): re-deriving settings
    // from the current form state would run the job with TODAY's knobs
    // while the history row records the original run - the row and the
    // execution would disagree. The payload IS the enqueue body.
    void replayJob(job.payload as Record<string, unknown>)
  }

  async function replayJob(payload: Record<string, unknown>) {
    const kind = payload.kind as 'scan' | 'plan' | 'encode' | undefined
    if (kind !== 'scan' && kind !== 'plan' && kind !== 'encode') return
    if (pendingKind !== null) return
    pendingKind = kind
    persist()
    try {
      await api.enqueue({ ...payload, kind } as Parameters<typeof api.enqueue>[0])
      await refresh()
    } catch (e) {
      error = e instanceof Error ? e.message : String(e)
    } finally {
      pendingKind = null
    }
  }

  // per-movie actions (encode/plan a single title from its page)
  async function encodeOne(path: string) {
    if (pendingKind !== null) return
    void enqueue('encode', [path])
  }

  // jobs → movie: a single-movie job row links to its title's detail page.
  // The movie must be in the snapshot's library (the detail page's props
  // come from it); when the library hasn't arrived yet, the deep-link
  // restore effect resolves it as soon as it does.
  function openDetailFromJobs(path: string) {
    const m = movies.find((mv) => mv.path === path)
    if (m) {
      openDetail(m, 'jobs')
    } else {
      // library not fetched yet (or movie absent from the snapshot): the
      // deep-link restore resolves it as soon as the library arrives
      pendingDetailFrom = 'jobs'
      detailPath = path
      detailFrom = 'jobs'
      location.hash = '/movie'
    }
  }

  let dismissed = $state<Set<number>>(new Set())
  const runningJobs = $derived(
    history.filter((j) => (j.state === 'running' || j.state === 'queued') && j.id !== null && !dismissed.has(j.id))
  )
  // failed work: jobs that FAILED, and DONE jobs whose per-movie outcomes
  // failed (the engine completed but the work did not — a green DONE must
  // not hide them)
  const failedJobs = $derived(
    history.filter((j) => {
      if (j.id === null || dismissed.has(j.id)) return false
      if (j.state === 'failed') return true
      if (j.state === 'done' && j.kind === 'encode') {
        const outcomes = (j.result as { outcomes?: { state: string }[] } | null)?.outcomes ?? []
        return outcomes.some((o) => o.state === 'failed')
      }
      return false
    })
  )
  function dismissFailed(id: number | null) {
    if (id === null) return
    dismissed = new Set([...dismissed, id])
  }
  const runningJob = $derived(runningJobs.find((j) => j.state === 'running') ?? null)
  const runningProgress = $derived(runningJob !== null ? progressFor(runningJob.id) : null)

  const missingMovies = $derived(movies.filter((m) => m.missing))

  onMount(() => {
    poll()
    connectEvents()
    void loadRetained()
    return () => {
      if (pollTimer !== null) clearTimeout(pollTimer)
    }
  })

  const savedTotalGb = $derived(
    movies.reduce((sum, m) => {
      if (!m.encode || m.encode.saved_percent == null) return sum
      return sum + ((m.encode.input_size ?? 0) - (m.encode.output_size ?? 0))
    }, 0) / 1024 ** 3
  )
  const measuredCount = $derived(movies.filter((m) => m.encode?.vmaf != null).length)
</script>

<svelte:window onkeydown={handleShortcuts} />

<!-- skip link: the first focusable element — keyboard users reach page
     content in one Tab instead of walking seven rail controls -->
<a class="skip-link" href="#main-content">SKIP TO CONTENT</a>

<div class="console">
  <!-- ══ sidebar rail ══ -->
  <aside class="rail">
    <div class="brand">
      <h1>SHRINKRAY</h1>
      <p class="legend">LIBRARY CONSOLE</p>
    </div>
    <nav class="rail-nav" aria-label="Sections">
      <button class="rail-item" class:active={page === 'overview'} onclick={() => goto('overview')}>
        <span class="rail-label">Overview</span>
        {#if failedJobs.length > 0}<span class="rail-badge bad mono">{failedJobs.length}</span>{/if}
      </button>
      <button class="rail-item" class:active={page === 'library'} onclick={() => goto('library')}>
        <span class="rail-label">Library</span>
        <span class="rail-badge mono">{movies.length}</span>
      </button>
      <button class="rail-item" class:active={page === 'jobs'} onclick={() => goto('jobs')}>
        <span class="rail-label">Jobs</span>
        {#if runningJobs.length > 0}<span class="rail-badge warn mono">{runningJobs.length}</span>{/if}
      </button>
      <button class="rail-item" class:active={page === 'retained'} onclick={() => goto('retained')}>
        <span class="rail-label">Retained</span>
        {#if retainedFiles.filter((f) => f.present).length > 0}<span class="rail-badge warn mono">
          {retainedFiles.filter((f) => f.present).length}
        </span>{/if}
      </button>
      <button class="rail-item" class:active={page === 'config'} onclick={() => goto('config')}>
        <span class="rail-label">Config</span>
      </button>
    </nav>
    <div class="rail-foot">
      <button class="theme-toggle legend" onclick={() => (theme = theme === 'dark' ? 'light' : 'dark')}
        aria-label={theme === 'dark' ? 'Switch to the light working bench' : 'Switch to the dark cinema ground'}
        title={theme === 'dark' ? 'Switch to the light working bench' : 'Switch to the dark cinema ground'}>
        {theme === 'dark' ? '◐ DARK' : '◑ LIGHT'}
      </button>
      <button class="legend watcher" class:on={status?.watcher === 'running'} onclick={toggleWatcher}
        aria-label={status?.watcher === 'running' ? 'Stop the background watcher' : 'Start the background watcher'}
        aria-pressed={status?.watcher === 'running'}>
        {status?.watcher === 'running' ? '● WATCHER' : '○ WATCHER'}
      </button>
      <span class="legend shortcut-hint mono">{SHORTCUT_LEGEND}</span>
    </div>
  </aside>

  <!-- ══ main column ══ -->
  <main class="main" id="main-content" tabindex="-1">
    {#if needsToken}
      <div class="api-error mono" role="alert">
        <form
          onsubmit={(e) => {
            e.preventDefault()
            submitToken()
          }}>
          <label for="token-input">this dashboard requires a token (SHRINKRAY_TOKEN):</label>
          <input
            id="token-input"
            type="password"
            bind:value={tokenInput}
            autocomplete="current-password"
            placeholder="paste the token"
          />
          <button type="submit">unlock</button>
        </form>
      </div>
    {/if}
    {#if error}
      <div class="api-error mono" role="alert">
        {error}
        <button class="dismiss" onclick={() => (error = null)} aria-label="Dismiss error">✕</button>
      </div>
    {/if}
    {#if notice}
      <div class="api-notice mono">
        {notice}
        <button class="dismiss" onclick={() => (notice = null)} aria-label="Dismiss note">✕</button>
      </div>
    {/if}

    {#if detailMovie}
      <MovieDetailPage
        movie={detailMovie}
        {targetVmaf}
        allJobs={history}
        onclose={closeDetail}
        onencode={encodeOne}
        onplan={(p) => enqueue('plan', [p])}
        onjobs={() => goto('jobs')}
        backTo={detailFrom}
      />
    {:else}
      <div class="pages" class:hidden={page !== 'overview'}>
        <OverviewPage
          {movies}
          {history}
          {status}
          {targetVmaf}
          {savedTotalGb}
          {measuredCount}
          {missingMovies}
          {runningJobs}
          {failedJobs}
          {scanPaths}
          {outputDir}
          sparkSamples={rssSamples}
          ondismiss={dismissFailed}
          ongoto={goto}
          onopenmovie={(m) => openDetail(m, 'overview')}
          oncancel={cancelJob}
          cancelling={pendingKind === 'cancel'}
        />
      </div>
      <div class="pages" class:hidden={page !== 'library'}>
        <LibraryPage
          {movies}
          {selectedPaths}
          {targetVmaf}
          scanning={kindQueued('scan')}
          pending={pendingKind !== null}
          runningMovie={runningProgress?.movie ?? null}
          runningPercent={runningProgress?.percent ?? null}
          plannedPaths={planEntries.filter((p) => p.state === 'encode').map((p) => p.movie)}
          onenqueue={(kind, paths) => {
            // whole-library encode (no explicit paths) gets the preflight:
            // scope + measured estimate before the loudest verb fires
            if (kind === 'encode' && !paths) {
              requestEncodeAll()
              return
            }
            enqueue(kind, paths)
          }}
          onopenmovie={(m) => openDetail(m, 'library')}
          ontoggle={(path, checked) => {
            const next = new Set(selectedPaths)
            if (checked) next.add(path)
            else next.delete(path)
            selectedPaths = next
          }}
          onselectall={(checked, paths) => {
            const next = new Set(selectedPaths)
            if (checked) paths.forEach((p) => next.add(p))
            else paths.forEach((p) => next.delete(p))
            selectedPaths = next
          }}
          onclear={() => (selectedPaths = new Set())}
        />
      </div>
      <div class="pages" class:hidden={page !== 'jobs'}>
        <JobsPage {history} oncancel={cancelJob} onretry={retryJob} onopenMovie={(p) => openDetailFromJobs(p)} />
      </div>
      <div class="pages" class:hidden={page !== 'retained'}>
        <RetainedPage files={retainedFiles} onrecover={() => void loadRetained()} />
      </div>
      <div class="pages" class:hidden={page !== 'config'}>
        <ConfigPage
          {scanPaths}
          {outputDir}
          {targetVmaf}
          {workers}
          onpathsChange={(p) => { scanPaths = p; persist() }}
          onoutputChange={(o) => { outputDir = o; persist() }}
          onvmafChange={(v) => { targetVmaf = v; persist() }}
          onworkersChange={(w) => { workers = w; persist() }}
        />
      </div>
    {/if}
  </main>
</div>

<!-- ══ encode-all preflight: scope + measured estimate before the loudest verb ══ -->
{#if pendingEncodeAll}
  <!-- amber ceremony, not the red one: encoding is reversible (sources
       untouched) — red is the destruction hue and must stay frightening
       for the bars crop and the recycle bin (round-6 P1) -->
  <div class="bars-confirm encode-preflight" role="alertdialog" aria-label="Confirm whole-library encode" tabindex="-1" bind:this={encodeAllEl} onkeydown={encodeAllKeydown}>
    <div class="bars-text">
      <strong>ENCODE THE ENTIRE LIBRARY?</strong>
      {encodeAllTitles} titles · {encodeAllSizeGb.toFixed(1)} GB source
      {#if encodeAllEstLabel}· {encodeAllEstLabel}{/if}
      — sources are never touched; results land in {outputDir || 'the output folder'}.
    </div>
    <div class="bars-buttons">
      <button class="mono deny" onclick={closeEncodeAll}>NOT NOW</button>
      <button class="mono confirm primary" onclick={() => { pendingEncodeAll = false; void enqueue('encode', undefined) }}>
        ENCODE ALL
      </button>
    </div>
  </div>
{/if}

<style>
  /* skip link: invisible until keyboard-focused, then a console-styled
     jump label over the content */
  .skip-link {
    position: fixed;
    top: -48px;
    left: 190px;
    z-index: 100;
    padding: 8px 14px;
    background: var(--panel-active);
    border: 1px solid var(--teal);
    border-radius: var(--radius);
    color: var(--teal-ink);
    font-size: var(--fs-legend);
    letter-spacing: var(--track-legend);
    text-decoration: none;
    transition: top 0.1s ease;
  }
  .skip-link:focus-visible {
    top: 10px;
  }

  .console {
    display: flex;
    min-height: 100vh;
    background: var(--ground);
    color: var(--ink);
    font-size: var(--fs-body);
  }

  /* ══ sidebar rail ══ */
  .rail {
    width: 190px;
    flex-shrink: 0;
    background: var(--rail);
    border-right: 1px solid var(--seam);
    display: flex;
    flex-direction: column;
    padding: 18px 12px;
    gap: 20px;
    position: sticky;
    top: 0;
    height: 100vh;
  }
  .brand h1 {
    font-size: 16px;
    font-weight: 700;
    letter-spacing: 3px;
  }
  .brand .legend { margin-top: 2px; }
  .rail-nav {
    display: flex;
    flex-direction: column;
    gap: 2px;
  }
  .rail-item {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 8px;
    background: transparent;
    border: none;
    border-radius: var(--radius);
    color: var(--ink-dim);
    padding: 7px 12px;
    cursor: pointer;
    font-size: 13px;
    font-weight: 500;
    text-align: left;
  }
  .rail-item:hover { color: var(--ink); background: var(--panel); }
  .rail-item.active {
    color: var(--ink);
    background: var(--panel-active);
  }
  .rail-badge {
    font-size: 10.5px;
    color: var(--ink-dim);
  }
  .rail-badge.warn { color: var(--amber); }
  .rail-badge.bad { color: var(--red); }
  .rail-foot {
    margin-top: auto;
    display: flex;
    flex-direction: column;
    gap: 6px;
  }
  .theme-toggle, .watcher {
    background: transparent;
    border: 1px solid var(--seam);
    border-radius: var(--radius);
    color: var(--ink-dim);
    padding: 5px 10px;
    cursor: pointer;
    text-align: left;
    /* a11y pass: interactive controls read at data size and carry a
       24px-minimum hit target */
    font-size: var(--fs-data);
    min-height: 24px;
  }
  .theme-toggle:hover { color: var(--ink); border-color: var(--ink-dim); }
  .watcher.on { color: var(--teal-ink); border-color: var(--teal); }
  .watcher:not(.on):hover { color: var(--ink); }
  /* the keyboard legend: shortcuts exist since always — this line makes
     them discoverable without an overlay or a help page */
  .shortcut-hint {
    border: none;
    color: var(--ink-faint);
    padding: 0 2px;
    font-size: 9.5px;
    letter-spacing: 0.3px;
    white-space: nowrap;
  }

  /* ══ main column ══ */
  .main {
    flex: 1;
    min-width: 0;
    padding: 20px 26px 40px;
    display: flex;
    flex-direction: column;
    gap: 14px;
    max-width: 1500px;
  }
  /* skip-link target: focusable via the link, never Tab-stops on it */
  .main:focus {
    outline: none;
  }
  .main:focus-visible {
    outline: 1px solid var(--teal);
    outline-offset: -1px;
  }
  /* pages stay mounted (state survives tab switches); only one visible */
  .pages { display: flex; flex-direction: column; gap: 14px; min-height: 0; }
  .pages.hidden { display: none; }

  .api-error {
    border: 1px solid var(--red);
    color: var(--red);
    background: repeating-linear-gradient(45deg, rgba(242, 114, 101, 0.06) 0 4px, transparent 4px 8px), var(--panel);
    padding: 8px 14px;
    border-radius: var(--radius);
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 12px;
    word-break: break-word;
  }
  /* the quiet sibling: same geometry, no red — informational feedback
     ("already queued", "select at least one") never wears failure red */
  .api-notice {
    border: 1px solid var(--seam);
    color: var(--ink-dim);
    background: var(--panel);
    padding: 8px 14px;
    border-radius: var(--radius);
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 12px;
  }
  .api-notice .dismiss { color: var(--ink-dim); }
  .dismiss {
    background: transparent;
    border: none;
    color: var(--red);
    cursor: pointer;
    font-size: 12px;
  }

  /* ══ confirmation strip ══ */
  .bars-confirm {
    position: fixed;
    left: 190px;
    right: 0;
    bottom: 0;
    z-index: 60;
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 14px;
    flex-wrap: wrap;
    border-top: 1px solid var(--red);
    background: repeating-linear-gradient(45deg, rgba(242, 114, 101, 0.08) 0 4px, transparent 4px 8px), var(--panel);
    padding: 12px 26px;
  }
  .bars-text { font-size: var(--fs-data); }
  .bars-buttons { display: flex; gap: 8px; }
  .bars-buttons button {
    background: transparent;
    border: 1px solid var(--seam);
    color: var(--ink);
    padding: 5px 14px;
    cursor: pointer;
    border-radius: var(--radius);
    font-size: var(--fs-legend);
    letter-spacing: var(--track-legend);
  }
  .bars-buttons .confirm { border-color: var(--red); color: var(--red); }
  .bars-buttons .confirm:hover { background: var(--red); color: var(--ground); }
  .bars-buttons .deny:hover { border-color: var(--ink-dim); }

  /* the preflight variant: an amber ceremony for a REVERSIBLE verb.
     Encoding never touches a source, so it must not rent red's alarm —
     that would teach the eye to ignore the real red ceremonies
     (bars crop, recycle bin). Amber = work in progress; the confirm
     button keeps its primary teal fill. */
  .bars-confirm.encode-preflight {
    border-top-color: var(--amber);
    background: repeating-linear-gradient(45deg, rgba(224, 164, 94, 0.08) 0 4px, transparent 4px 8px), var(--panel);
  }
  .bars-confirm.encode-preflight .bars-buttons .confirm {
    border-color: var(--teal);
    color: var(--ground);
    background: var(--teal);
  }
  .bars-confirm.encode-preflight .bars-buttons .confirm:hover { filter: brightness(1.08); background: var(--teal); color: var(--ground); }

  @media (max-width: 900px) {
    .console { flex-direction: column; }
    .rail {
      width: 100%;
      height: auto;
      position: static;
      flex-direction: row;
      align-items: center;
      padding: 10px 14px;
      gap: 12px;
    }
    .brand .legend { display: none; }
    .rail-nav { flex-direction: row; flex: 1; overflow-x: auto; }
    .rail-item { padding: 6px 10px; white-space: nowrap; }
    .rail-item.active { box-shadow: inset 0 -2px 0 var(--teal); }
    .rail-foot { margin-top: 0; flex-direction: row; }
    .main { padding: 14px; }
    .bars-confirm { left: 0; }
  }
</style>

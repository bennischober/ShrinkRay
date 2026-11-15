<script lang="ts">
  import { queueLabel } from './lib/format'
  import { onMount } from 'svelte'
  import {
    api,
    isAuthFailure,
    isFailedOutcome,
    jobPaths,
    storeToken,
    storedToken,
    type Movie,
    type RetainedFile,
    type Status,
    type Job,
    type JobOutcome,
  } from './lib/api'
  import OverviewPage from './lib/pages/OverviewPage.svelte'
  import LibraryPage from './lib/pages/LibraryPage.svelte'
  import JobsPage from './lib/pages/JobsPage.svelte'
  import ConfigPage from './lib/pages/ConfigPage.svelte'
  import RetainedPage from './lib/pages/RetainedPage.svelte'
  import MovieDetailPage from './lib/pages/MovieDetailPage.svelte'
  import { connectEvents, eventsConnected, progressFor, resetEvents } from './lib/progress.svelte'
  import { deriveConnection, freshnessText, POLL_ACTIVE, POLL_HIDDEN, POLL_IDLE } from './lib/connection'

  type Page = 'overview' | 'library' | 'jobs' | 'retained' | 'config'

  let status: Status | null = $state(null)
  let movies: Movie[] = $state([])
  let retainedFiles: RetainedFile[] = $state([])
  let history: Job[] = $state([])
  let error = $state<string | null>(null)
  /** The server requires a bearer token and none is stored (issue #36):
   * the UI asks for it instead of showing a red error strip. */
  let needsToken = $state(false)
  /** when the last snapshot arrived: the freshness indicator and the stale
   * state both read it (issue #629) */
  let lastOkAt = $state<number | null>(null)
  let tokenWrong = $state(false)
  /** ticks once a second so the freshness text ages without a new snapshot */
  let clock = $state(Date.now())
  $effect(() => {
    const timer = setInterval(() => (clock = Date.now()), 1000)
    return () => clearInterval(timer)
  })
  const connectionState = $derived(
    deriveConnection({
      hasSnapshot: status !== null,
      locked: needsToken,
      lastOkAt,
      eventsUp: eventsConnected(),
      now: clock,
    }),
  )
  // a badge is a count: it is never drawn while the data is unknown or old
  const showCounts = $derived(connectionState === 'live' || connectionState === 'stale')
  // the banner names the failure it actually has: a dropped event stream is not
  // an unreachable server, and blaming the server would send the operator to the
  // wrong place (issue #629 review)
  const staleReason = $derived(
    error !== null
      ? "Can't reach ShrinkRay — retrying every 3 s. Work on the server continues."
      : 'Live progress disconnected — the numbers below are from the last update. Reconnecting…',
  )
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

  // ── job parameters (read-only facts, issue #331) ──
  // These are the RESIDENT APP's settings, not UI preferences: the VMAF
  // floor lives in config.json (quality.target_vmaf), the scan sources in
  // the running scan job's payload. The localStorage drafts (ms.paths,
  // ms.out, ms.vmaf, ms.workers) wrote values the app never used — a
  // control surface that controls nothing (issue #331; fixed mode's
  // workers were deleted with it, #329).
  let configVmaf = $state<number | null>(null)
  let targetVmaf = $derived(configVmaf ?? 96)
  let scanPaths = $state<string[]>([])
  let outputDir = $state('')

  $effect(() => {
    api
      .config()
      .then((c) => (configVmaf = c?.quality?.target_vmaf ?? null))
      .catch(() => {}) // the config strip (config_error on /api/status) reports why it is stale
  })

  // the scan sources: the latest scan/plan/encode job's payload paths (the
  // real roots the app works from), not a localStorage draft
  const latestScanJob = $derived(history.find((j) => j.kind === 'scan' && j.state === 'done'))
  $effect(() => {
    const paths = latestScanJob ? jobPaths(latestScanJob) : []
    if (paths.length > 0) scanPaths = paths
  })

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

  // issue #633: the Overview's sparkline is gone, and with it the
  // resident-memory window that fed it — no panel shows the trend, so no
  // code accumulates one.

  // ── refresh loop ──
  async function refresh() {
    try {
      const snap = await api.snapshot()
      status = snap.status
      if (snap.jobs !== null) history = snap.jobs // unchanged: keep the current array
      if (snap.library !== null) movies = snap.library // unchanged: keep the current array
      error = null
      needsToken = false
      lastOkAt = Date.now()
    } catch (e) {
      // a 401 is not an error to report: the server requires a token, so
      // the UI asks for one instead of showing a red strip (issue #36)
      if (isAuthFailure(e)) {
        needsToken = true
        tokenWrong = storedToken() !== null
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
    tokenWrong = false
    // the stream is opened once: without this the first mount's tokenless
    // connection would never carry the new token (issue #629)
    resetEvents()
    connectEvents()
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

  // adaptive polling: one snapshot request per tick, fast while work runs,
  // relaxed when idle, slow while the tab is hidden
  let pollTimer: ReturnType<typeof setTimeout> | null = null
  async function poll() {
    await refresh()
    const busy = history.some((j) => j.state === 'queued' || j.state === 'running')
    const hidden = document.hidden
    schedule(busy ? POLL_ACTIVE : POLL_IDLE, hidden)
  }
  function schedule(interval: number, hidden: boolean) {
    pollTimer = setTimeout(poll, hidden ? POLL_HIDDEN : interval)
  }

  // ── the whole-library confirmation ──
  // This block is reserved shape: the dashboard renders no encode control
  // (issue #134 - it starts nothing; the resident app starts the backlog
  // and the watcher itself, PRODUCT.md's deployment model). The pattern
  // the DESIGN.md Do's and Don'ts section reserves ("Do gate irreversible
  // work behind a per-run confirmation", "Do brief the loudest verb") is
  // what a whole-library gate would wear if the product ever offers one.
  // Bar removal is not a choice any more (issue #128): every encode crops
  // the bars it finds, so there is nothing to arm.


  // retry, like the confirmation above, is reserved shape: the dashboard
  // renders no retry control (issue #134 — it starts nothing; a failed
  // job's row is its trail, and the terminal is the recovery path). The
  // pattern a retry would wear if the product ever offers one: re-enqueue
  // the job with ITS OWN scope and parameters (never the current form — a
  // failed single-movie encode must not become a whole-library run)
  // per-movie actions (encode/plan a single title from its page)
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
        const outcomes = (j.result as { outcomes?: JobOutcome[] } | null)?.outcomes ?? []
        return outcomes.some(isFailedOutcome)
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
      // only an APPLIED encode reclaimed anything (issue #625): a discarded
      // attempt's sizes describe an artifact that was thrown away
      if (!m.encode || m.encode.outcome === 'discarded') return sum
      if (m.encode.saved_percent == null) return sum
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
      <button class="rail-item" class:active={page === 'overview'} aria-current={page === 'overview' ? 'page' : undefined} onclick={() => goto('overview')}>
        <span class="rail-label">Overview</span>
        {#if showCounts && failedJobs.length > 0}<span class="rail-badge bad mono">{failedJobs.length}</span>{/if}
      </button>
      <button class="rail-item" class:active={page === 'library'} aria-current={page === 'library' ? 'page' : undefined} onclick={() => goto('library')}>
        <span class="rail-label">Library</span>
        {#if showCounts}<span class="rail-badge mono">{movies.length}</span>{/if}
      </button>
      <button class="rail-item" class:active={page === 'jobs'} aria-current={page === 'jobs' ? 'page' : undefined} onclick={() => goto('jobs')}>
        <span class="rail-label">Jobs</span>
        {#if showCounts && runningJobs.length > 0}<span class="rail-badge warn mono" title={queueLabel(runningJobs.length)} aria-label={queueLabel(runningJobs.length)}>{runningJobs.length}</span>{/if}
      </button>
      <button class="rail-item" class:active={page === 'retained'} aria-current={page === 'retained' ? 'page' : undefined} onclick={() => goto('retained')}>
        <span class="rail-label">Retained</span>
        {#if showCounts && retainedFiles.filter((f) => f.present).length > 0}<span class="rail-badge warn mono">
          {retainedFiles.filter((f) => f.present).length}
        </span>{/if}
      </button>
      <button class="rail-item" class:active={page === 'config'} aria-current={page === 'config' ? 'page' : undefined} onclick={() => goto('config')}>
        <span class="rail-label">Config</span>
      </button>
    </nav>
    <div class="rail-foot">
      <button class="theme-toggle legend" onclick={() => (theme = theme === 'dark' ? 'light' : 'dark')}
        aria-label={theme === 'dark' ? 'Switch to the light working bench' : 'Switch to the dark cinema ground'}
        title={theme === 'dark' ? 'Switch to the light working bench' : 'Switch to the dark cinema ground'}>
        {theme === 'dark' ? '◐ DARK' : '◑ LIGHT'}
      </button>
      {#if showCounts}
        <span class="legend watcher" class:on={connectionState === 'live' && status?.watcher === 'running'}
          title={connectionState === 'live' ? (status?.watcher === 'running' ? 'The background watcher is running' : (status?.watcher_error ?? 'The background watcher is stopped')) : 'The watcher state is unknown: the data is not current'}>
          {connectionState === 'live' ? (status?.watcher === 'running' ? '● WATCHER' : '○ WATCHER') : '? WATCHER'}
        </span>
        <span class="legend worker" class:on={connectionState === 'live' && status?.worker === 'running'}
          title={connectionState === 'live' ? (status?.worker === 'running' ? 'The job worker is running' : (status?.worker_error ?? 'The job worker is stopped')) : 'The worker state is unknown: the data is not current'}>
          {connectionState === 'live' ? (status?.worker === 'running' ? '● WORKER' : '○ WORKER') : '? WORKER'}
        </span>
      {/if}
      <span class="legend freshness" class:stale={connectionState === 'stale'} class:live={connectionState === 'live'}>
        {freshnessText(connectionState, lastOkAt, clock)}
      </span>
      <span class="legend shortcut-hint mono">{SHORTCUT_LEGEND}</span>
    </div>
  </aside>

  <!-- ══ main column ══ -->
  <main class="main" id="main-content" tabindex="-1">
    {#if connectionState === 'locked'}
      <div class="unlock">
        <form
          class="mono"
          onsubmit={(e) => {
            e.preventDefault()
            submitToken()
          }}>
          <h2>ShrinkRay needs a token</h2>
          <p class="dim">
            The dashboard reads the token the server was started with — <code>/data/token</code> in the container, or
            <code>$SHRINKRAY_TOKEN</code>.
          </p>
          <label for="token-input">Token</label>
          <input
            id="token-input"
            type="password"
            bind:value={tokenInput}
            autocomplete="current-password"
            placeholder="paste the token"
            aria-describedby={tokenWrong ? 'token-wrong' : undefined}
          />
          {#if tokenWrong}
            <p class="unlock-wrong" id="token-wrong" role="status">That token was refused — check /data/token.</p>
          {/if}
          <button type="submit">Unlock</button>
        </form>
      </div>
    {:else if connectionState === 'loading'}
      <div class="loading mono dim">Loading library…</div>
    {:else}
      {#if connectionState === 'stale'}
        <div class="api-notice mono" role="status">
          {staleReason}
          <span class="dim"> {freshnessText(connectionState, lastOkAt, clock)}</span>
          <details>
            <summary>Details</summary>
            <pre class="mono">{error ?? 'no response'}</pre>
          </details>
        </div>
      {/if}
    {#if status?.worker_error}
      <!-- not dismissible: it clears on the worker's next app restart (issue
        #649): a dead worker means nothing runs - the first thing to see -->
      <div class="api-error mono" role="alert">{status.worker_error}</div>
    {/if}
    {#if status?.config_error}
      <!-- not dismissible: it clears once the file is fixed (issue #247) -->
      <div class="api-error mono" role="alert">{status.config_error}</div>
    {/if}
    {#if status?.watcher_error}
      <!-- not dismissible: it clears when the watcher starts (issue #648) -->
      <div class="api-error mono" role="alert">{status.watcher_error}</div>
    {/if}
    {#if status?.encode_window === 'closed'}
      <!-- read-only: the window is set in config.json (issue #434) -->
      <div class="api-notice mono" role="status">Outside the encode window — no new file starts</div>
    {/if}
    {#if status?.paused}
      <!-- read-only: pausing and resuming happen in the terminal (issue #250) -->
      <div class="api-notice mono" role="status">Paused — no new file starts: {status.paused}</div>
    {/if}
    {#if error && connectionState !== 'stale'}
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
        revealAvailable={status?.reveal_available ?? false}
        onclose={closeDetail}
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
          {retainedFiles}
          stale={connectionState === 'stale'}
          ondismiss={dismissFailed}
          ongoto={goto}
          onopenmovie={(m) => openDetail(m, 'overview')}
        />
      </div>
      <div class="pages" class:hidden={page !== 'library'}>
        <LibraryPage
          {movies}
          {selectedPaths}
          {targetVmaf}
          runningMovie={runningProgress?.movie ?? null}
          runningPercent={runningProgress?.percent ?? null}
          plannedPaths={planEntries.filter((p) => p.state === 'encode').map((p) => p.movie)}
          planSavings={planEntries.flatMap((p) =>
            p.state === 'encode' && p.saved_percent != null && p.saved_percent > 0
              ? [[p.movie, p.saved_percent] as const]
              : [],
          )}
          onopenmovie={(m) => openDetail(m, 'library')}
          stale={connectionState === 'stale'}
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
        <JobsPage {history} stale={connectionState === 'stale'} onopenMovie={(p) => openDetailFromJobs(p)} />
      </div>
      <div class="pages" class:hidden={page !== 'retained'}>
        <RetainedPage files={retainedFiles} onrecover={() => void loadRetained()} />
      </div>
      <div class="pages" class:hidden={page !== 'config'}>
        <ConfigPage {scanPaths} {outputDir} {targetVmaf} />
      </div>
    {/if}
    {/if}
  </main>
</div>

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
  .theme-toggle, .watcher, .worker {
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
  .watcher.on, .worker.on { color: var(--teal-ink); border-color: var(--teal); }
  .watcher:not(.on):hover, .worker:not(.on):hover { color: var(--ink); }
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

  @media (max-width: 900px) {
    /* the rail becomes a bottom tab bar: thumb reach and one-tap switching fit a
       status app, and every item is reachable at 320 px instead of scrolling
       sideways inside the rail (issue #636) */
    .console { flex-direction: column-reverse; }
    .rail {
      width: 100%;
      height: auto;
      position: sticky;
      bottom: 0;
      z-index: 20;
      flex-direction: column;
      gap: 6px;
      padding: 6px 8px calc(6px + env(safe-area-inset-bottom));
      border-right: none;
      border-top: 1px solid var(--seam);
    }
    .brand { display: none; }
    .rail-nav {
      display: grid;
      grid-template-columns: repeat(5, 1fr);
      gap: 2px;
      overflow: visible;
    }
    .rail-item {
      justify-content: center;
      padding: 8px 2px;
      white-space: normal;
      text-align: center;
      font-size: 10px;
      letter-spacing: 0.04em;
    }
    .rail-item.active {
      background: var(--panel-active);
      box-shadow: inset 0 2px 0 var(--teal);
    }
    .rail-foot {
      margin-top: 0;
      flex-direction: row;
      justify-content: space-between;
      padding: 0;
      border-top: none;
      gap: 8px;
    }
    .rail-foot .shortcut-hint { display: none; }
    .main { padding: 14px 12px 20px; flex: 1; }
    /* the margin belongs to the element that scrolls into view, not to the
       container: on .main it changed nothing and a tabbed-to button still landed
       fully behind the sticky bar (issue #636 review) */
    :global(.main :is(button, a, input, select, textarea, summary)) {
      scroll-margin-bottom: 84px;
    }
  }

  /* a keyboard legend is meaningless without a keyboard (issue #636) */
  @media (hover: none) {
    .shortcut-hint { display: none; }
  }

  /* prose keeps a readable measure on a very wide screen (issue #636) */
  @media (min-width: 1800px) {
    .main { padding: 24px 32px; }
    :global(.page-header .desc) { max-width: 75ch; }
  }
  .unlock {
    max-width: 460px;
    margin: 14vh auto 0;
    padding: 26px;
    border: 1px solid var(--row-line);
    border-radius: 4px;
    background: var(--panel);
  }
  .unlock h2 { margin: 0 0 10px; font-size: 15px; font-weight: 600; }
  .unlock p { margin: 0 0 14px; font-size: 12px; line-height: 1.55; }
  .unlock label {
    display: block;
    margin-bottom: 6px;
    font-size: 10.5px;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    color: var(--ink-dim);
  }
  .unlock input { width: 100%; margin-bottom: 12px; }
  .unlock button { padding: 6px 16px; }
  .unlock-wrong { color: var(--red); margin: 0 0 12px; font-size: 11px; }
  .loading { padding: 14vh 0; text-align: center; font-size: 12px; }
  .freshness { margin-left: auto; }
  .freshness.live { color: var(--green); }
  .freshness.stale { color: var(--amber); }
</style>

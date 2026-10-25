<script lang="ts">
  import { fmtGb, fmtSavedHonest, fmtSeconds, fmtSpan, fmtWhen, humanizeError, jobPaths, type Movie, type Status, type Job, type RetainedFile } from '../api'
  import { progressFor } from '../progress.svelte'

  /** Path equality that survives separator and case differences. */
  function pathEq(a: string, b: string): boolean {
    return a.replace(/[\\/]+/g, '/').replace(/\/$/, '').toLowerCase() === b.replace(/[\\/]+/g, '/').replace(/\/$/, '').toLowerCase()
  }

  let {
    movies,
    history,
    status,
    targetVmaf,
    savedTotalGb,
    measuredCount,
    missingMovies,
    runningJobs,
    failedJobs,
    scanPaths,
    outputDir,
    sparkSamples = [],
    retainedFiles = [],
    ondismiss,
    ongoto,
    onopenmovie,
  }: {
    movies: Movie[]
    history: Job[]
    status: Status | null
    targetVmaf: number
    savedTotalGb: number
    measuredCount: number
    missingMovies: Movie[]
    runningJobs: Job[]
    failedJobs: Job[]
    scanPaths: string[]
    outputDir: string
    /** resident-memory samples accumulated by the poll (App.svelte) —
     * the sparkline's buffer lives at app level so it survives page
     * switches and never writes what it reads */
    sparkSamples?: number[]
    /** the retention ledger's rows (App.svelte) — the panel totals the
     * present ones; the rows themselves are the Retained page's job */
    retainedFiles?: RetainedFile[]
    ondismiss: (id: number | null) => void
    ongoto: (page: 'overview' | 'library' | 'jobs' | 'config' | 'retained') => void
    onopenmovie: (movie: Movie) => void
  } = $props()

  const encodedCount = $derived(movies.filter((m) => m.encode && !m.encode.preset?.startsWith('skipped')).length)
  const untouchedCount = $derived(movies.filter((m) => !m.encode).length)
  const totalSizeGb = $derived(movies.reduce((sum, m) => sum + (m.size ?? 0), 0) / 1024 ** 3)

  // mean measured VMAF
  const measured = $derived(movies.filter((m) => m.encode?.vmaf != null))
  const meanVmaf = $derived(
    measured.length
      ? measured.reduce((sum, m) => sum + (m.encode?.vmaf ?? 0), 0) / measured.length
      : null
  )
  const minVmaf = $derived(measured.length ? Math.min(...measured.map((m) => m.encode?.vmaf ?? 100)) : null)
  const worstMeasured = $derived.by(() => {
    if (!measured.length) return null
    return measured.reduce((worst, m) =>
      (m.encode?.vmaf ?? 100) < (worst.encode?.vmaf ?? 100) ? m : worst
    )
  })

  // encode cost statistics (schema v5)
  const withDuration = $derived(movies.filter((m) => m.encode?.encode_seconds != null))
  const totalEncodeHours = $derived(
    withDuration.reduce((sum, m) => sum + (m.encode?.encode_seconds ?? 0), 0) / 3600
  )

  const runningJob = $derived(runningJobs.find((j) => j.state === 'running') ?? null)
  const runningProgress = $derived(runningJob !== null ? progressFor(runningJob.id) : null)

  // ETA: measured throughput (schema v5: encode_seconds per GB across all
  // recorded encodes) where available, proportional fallback otherwise
  const throughputSecPerGb = $derived.by(() => {
    const measuredEncodes = movies.filter(
      (m) => m.encode?.encode_seconds != null && (m.encode.input_size ?? 0) > 0
    )
    if (measuredEncodes.length === 0) return null
    const totalSeconds = measuredEncodes.reduce((sum, m) => sum + (m.encode?.encode_seconds ?? 0), 0)
    const totalGb = measuredEncodes.reduce((sum, m) => sum + (m.encode?.input_size ?? 0) / 1024 ** 3, 0)
    return totalGb > 0 ? totalSeconds / totalGb : null
  })
  const eta = $derived.by(() => {
    if (!runningJob || !runningJob.started_at) return null
    const elapsedSec = Math.max(0, (Date.now() - Date.parse(runningJob.started_at.endsWith('Z') || runningJob.started_at.includes('+') ? runningJob.started_at : runningJob.started_at + 'Z')) / 1000)
    if (runningProgress?.percent != null && runningProgress.percent > 1) {
      // proportional: percent implies total wall time
      const totalSec = elapsedSec / (runningProgress.percent / 100)
      return Math.max(0, totalSec - elapsedSec)
    }
    if (throughputSecPerGb != null && runningProgress?.movie) {
      const movie = movies.find((m) => m.name === runningProgress.movie)
      const gb = (movie?.size ?? 0) / 1024 ** 3
      if (gb > 0) return Math.max(0, gb * throughputSecPerGb - elapsedSec)
    }
    return null
  })
  function fmtEta(seconds: number | null): string {
    if (seconds == null) return '—'
    const m = Math.round(seconds / 60)
    if (m < 60) return `~${m}m remaining`
    return `~${Math.floor(m / 60)}h ${m % 60}m remaining`
  }

  // completion moment: the last finished encode run, stated (peak-end)
  interface OutcomeEntry {
    movie: string
    state: string
    input_size?: number
    output_size?: number
    saved_percent?: number
    vmaf?: number | null
  }
  const lastEncodeRun = $derived(
    history.find((j) => j.kind === 'encode' && (j.state === 'done' || j.state === 'cancelled') && j.result)
  )
  const encodeOutcomes = $derived(
    (lastEncodeRun?.result as { outcomes?: OutcomeEntry[] } | null)?.outcomes ?? []
  )
  const runSummary = $derived.by(() => {
    if (!lastEncodeRun || encodeOutcomes.length === 0) return null
    const encoded = encodeOutcomes.filter((o) => o.state === 'encoded')
    const savedBytes = encoded.reduce(
      (sum, o) => sum + Math.max(0, (o.input_size ?? 0) - (o.output_size ?? 0)),
      0
    )
    // adaptive units: a 40 MB win must not read as "−0.0 GB"
    const savedLabel =
      savedBytes >= 0.1 * 1024 ** 3
        ? `−${(savedBytes / 1024 ** 3).toFixed(1)} GB`
        : `−${(savedBytes / 1024 ** 2).toFixed(0)} MB`
    const vmafs = encoded.map((o) => o.vmaf).filter((v) => v != null) as number[]
    const wall = fmtSpan(lastEncodeRun.started_at, lastEncodeRun.finished_at)
    return {
      id: lastEncodeRun.id,
      state: lastEncodeRun.state,
      count: encoded.length,
      failed: encodeOutcomes.filter((o) => o.state === 'failed').length,
      savedLabel,
      minVmaf: vmafs.length ? Math.min(...vmafs) : null,
      wall,
    }
  })

  // per-category bars: size + reclaimed per category (pure CSS, honest data)
  interface CatBar {
    name: string
    titles: number
    sizeGb: number
    reclaimedGb: number
  }
  /** Honest reclaimed label: adaptive like the LAST RUN band — a real
   * 43 MB win must not print as "−0.0 GB (0%)". Sub-1 GB reads in MB,
   * and a percentage below 1 prints as "<1%" instead of a false zero. */
  function fmtReclaimed(reclaimedGb: number, sizeGb: number): string {
    const size = reclaimedGb < 1 ? `−${(reclaimedGb * 1024).toFixed(0)} MB` : `−${reclaimedGb >= 10 ? reclaimedGb.toFixed(0) : reclaimedGb.toFixed(1)} GB`
    const pct = (reclaimedGb / sizeGb) * 100
    return `${size} (${pct >= 1 ? pct.toFixed(0) : '<1'}%)`
  }
  const catBars = $derived.by(() => {
    const map = new Map<string, CatBar>()
    for (const m of movies) {
      const bar = map.get(m.category) ?? { name: m.category, titles: 0, sizeGb: 0, reclaimedGb: 0 }
      bar.titles += 1
      bar.sizeGb += (m.size ?? 0) / 1024 ** 3
      if (m.encode?.saved_percent != null && m.encode.saved_percent > 0) {
        bar.reclaimedGb += ((m.encode.input_size ?? 0) - (m.encode.output_size ?? 0)) / 1024 ** 3
      }
      map.set(m.category, bar)
    }
    return [...map.values()].sort((a, b) => b.sizeGb - a.sizeGb)
  })
  const maxCatSize = $derived(Math.max(...catBars.map((b) => b.sizeGb), 0.001))

  // the chart is never amputated: the top-8 cap orders, the expander
  // reveals the rest inline — a "more →" that jumps to the library left
  // the chart literally cut off (round-6, user report)
  let showAllCats = $state(false)
  const visibleCats = $derived(showAllCats ? catBars : catBars.slice(0, 8))

  // plan results of the latest finished plan job
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
  /** A plan entry whose movie has an encode row is history, not a queue:
   * the prediction was overtaken by a measurement (the console must not
   * present "1 to encode" for titles that already encoded). */
  const alreadyEncoded = (movie: string) =>
    movies.some(
      (m) =>
        m.path.replace(/[\\/]/g, '').toLowerCase().endsWith(movie.replace(/[\\/]/g, '').toLowerCase()) &&
        m.encode != null
    )
  /** The library record for a plan entry (path-suffix join, plan entries
   * store bare filenames) — the measured truth an ENCODED row shows. */
  const movieFor = (movie: string) =>
    movies.find(
      (m) =>
        m.path.replace(/[\\/]/g, '').toLowerCase().endsWith(movie.replace(/[\\/]/g, '').toLowerCase())
    )
  /** Plan age in honest units — a stale plan must say so. */
  const planAge = $derived.by(() => {
    if (!lastPlan?.finished_at) return ''
    const span = fmtSpan(lastPlan.finished_at, new Date().toISOString())
    if (!span) return ''
    return `finished ${span} ago`
  })
  /** Head summary in real terms: what happened SINCE the plan ran. */
  const planSummary = $derived.by(() => {
    const encoded = planEntries.filter((p) => alreadyEncoded(p.movie)).length
    const open = planEntries.filter((p) => p.state === 'encode' && !alreadyEncoded(p.movie)).length
    const skipped = planEntries.filter((p) => p.state === 'skip' && !alreadyEncoded(p.movie)).length
    const parts: string[] = []
    if (encoded > 0) parts.push(`${encoded} encoded since`)
    if (open > 0) parts.push(`${open} to encode`)
    if (skipped > 0) parts.push(`${skipped} skipped`)
    return parts.join(' · ')
  })

  // ── the "what's left" figure: plan-predicted savings not yet realized ──
  // The RECLAIMED stat answers "what did we win"; the operator's next
  // question is "what remains" — the last plan predicted an outcome for
  // every open (not-yet-encoded) title, and that predicted total is a
  // plan figure, not a guess (it names its own age + basis in the panel).
  const reclaimable = $derived.by(() => {
    if (planEntries.length === 0) return null
    // open titles = plan said "encode" and reality has not overtaken it
    const open = planEntries.filter((p) => p.state === 'encode' && !alreadyEncoded(p.movie))
    if (open.length === 0) return null
    // predicted savings per open title: saved_percent × its library size.
    // When the library join fails (suffix collisions) or size is unknown,
    // the entry's own estimated_size back-computes the source
    // (estimated = source × (1 − saved%)) — an understatement is also a
    // lie; the sub-line says "plan-predicted" throughout.
    const gb = open.reduce((sum, p) => {
      const m = movieFor(p.movie)
      const pct = p.saved_percent ?? 45
      const sizeGb = m?.size != null && m.size > 0 ? m.size / 1024 ** 3 : p.estimated_size != null && pct < 100 ? p.estimated_size / (1 - pct / 100) / 1024 ** 3 : 0
      return sum + (sizeGb * pct) / 100
    }, 0)
    return { count: open.length, gb }
  })

  // ── resident-memory sparkline: the app watching itself ──
  // rss_mib arrives live on every snapshot; the client keeps a small
  // buffer so the SETUP panel shows the trend, not just the instant.
  // A slow leak becomes visible before it becomes a problem.
  // Samples accumulate in App.svelte's poll path (an $effect that read
  // AND wrote rssSamples looped itself to death — Svelte 5 forbids
  // self-reading effects, round-6.6 lesson).
  const rssSpark = $derived.by(() => {
    const samples = sparkSamples
    if (samples.length < 2) return null
    const min = Math.min(...samples)
    const max = Math.max(...samples)
    const span = Math.max(max - min, 2) // ≥2 MiB span: a flat line stays readable
    const w = 180
    const h = 30
    const pts = samples.map(
      (v, i) =>
        `${((i / (samples.length - 1)) * w).toFixed(1)},${(h - 2 - ((v - min) / span) * (h - 4)).toFixed(1)}`
    )
    return {
      points: pts.join(' '),
      min,
      max,
      last: samples[samples.length - 1],
      n: samples.length,
      w,
      h,
    }
  })

  /** The fail-row error currently expanded (one at a time). */
  let expandedErr = $state<number | null>(null)</script>

<div class="page page-in">
  <header class="page-head">
    <h2>Overview</h2>
    <p class="legend">THE LIBRARY AT A GLANCE · VMAF TARGET {targetVmaf}</p>
  </header>

  <!-- system line: the console's own telemetry (only what's not already
       on the rail badges and the stat band — those carry the counts) -->
  <div class="sysline mono dim">
    <span title="adaptive: 1s while work is queued/running, 3s idle, 10s in hidden tabs">POLL ADAPTIVE</span>
    <span>DB {status?.database?.split(/[\\/]/).pop() ?? '—'}</span>
    <span>{movies.length} TITLES · {measuredCount} MEASURED</span>
    <span class="spacer"></span>
    <span>{scanPaths.length} SOURCE{scanPaths.length === 1 ? '' : 'S'}</span>
  </div>

  <!-- ══ stat band ══ -->
  <div class="stats">
    <button class="stat" onclick={() => ongoto('library')}>
      <span class="legend">TITLES</span>
      <span class="stat-v mono">{movies.length}</span>
      <span class="stat-sub mono dim">{encodedCount} encoded · {untouchedCount} untouched</span>
    </button>
    <button class="stat" onclick={() => ongoto('library')}>
      <span class="legend">RECLAIMED</span>
      <span class="stat-v mono">{savedTotalGb.toFixed(1)} GB</span>
      <span class="stat-sub mono dim">of {totalSizeGb.toFixed(0)} GB library</span>
    </button>
    {#if reclaimable}
      <button class="stat" onclick={() => ongoto('library')} title="Plan-predicted savings on the {reclaimable.count} titles the last plan queued for encode — realized when they encode">
        <span class="legend">RECLAIMABLE</span>
        <span class="stat-v mono">{reclaimable.gb.toFixed(1)} GB</span>
        <span class="stat-sub mono dim">{reclaimable.count} planned · plan #{lastPlan?.id}</span>
      </button>
    {/if}
    {#if missingMovies.length > 0}
      <button class="stat" onclick={() => ongoto('library')}>
        <span class="legend">MISSING</span>
        <span class="stat-v mono bad">{missingMovies.length}</span>
        <span class="stat-sub mono dim">flagged by the last scan</span>
      </button>
    {/if}
  </div>

  <!-- ══ the figure in progress ══ -->
  {#if runningJob}
    <div class="running">
      <div class="running-head">
        <span class="legend amber">IN WORK · {runningJob.kind.toUpperCase()}</span>
        <span class="mono dim">{runningProgress?.done != null && runningProgress?.total != null ? `${runningProgress.done}/${runningProgress.total} · ` : ''}{runningProgress?.percent != null ? `${runningProgress.percent}%` : ''}</span>
      </div>
      <div class="running-title">{runningProgress?.movie || (runningJob.payload?.path as string)?.split(/[\\/]/).pop() || runningJob.kind}</div>
      <div class="trace" role="progressbar" aria-valuenow={runningProgress?.percent ?? undefined} aria-valuemin="0" aria-valuemax="100" aria-label="encode progress">
        <div class="fill" style="width: {runningProgress?.percent ?? 2}%"></div>
      </div>
      <div class="mono dim detail">
        {runningProgress?.detail ?? 'working…'}
        · started {fmtWhen(runningJob.started_at)}
        · {fmtEta(eta)}
      </div>
    </div>
  {:else}
    {#if runSummary}
      <div class="quiet-strip done-strip">
        <span class="mono">
          <span class="legend">LAST RUN #{runSummary.id}</span>
          {runSummary.count} encoded · {runSummary.savedLabel}{runSummary.minVmaf != null ? ` · min VMAF ${runSummary.minVmaf.toFixed(1)}` : ''}{runSummary.minVmaf != null && runSummary.minVmaf < targetVmaf ? ` (target ${targetVmaf})` : ''}{runSummary.wall ? ` · ${runSummary.wall} wall` : ''}
          {#if runSummary.failed > 0}<span class="bad"> · {runSummary.failed} failed</span>{/if}
        </span>
        <button class="nav-link" onclick={() => ongoto('jobs')}>JOBS →</button>
      </div>
    {:else}
      <div class="quiet-strip">
        <span class="mono dim">no active work</span>
        <button class="nav-link" onclick={() => ongoto('library')}>LIBRARY →</button>
      </div>
    {/if}
  {/if}

  <div class="cols">
    <!-- ══ failures ══ -->
    <section class="panel">
      <div class="panel-head">
        <span class="legend">ATTENTION — FAILURES</span>
        <span class="head-tools">
          {#if failedJobs.length > 1}<button class="link legend" onclick={() => failedJobs.forEach((j) => ondismiss(j.id))} aria-label="Dismiss all {failedJobs.length} failed jobs from the overview">DISMISS ALL</button>{/if}
          {#if failedJobs.length > 0}<button class="nav-link" onclick={() => ongoto('jobs')}>JOBS →</button>{/if}
        </span>
      </div>
      {#if failedJobs.length === 0}
        <div class="dim pad">nothing failed — the last runs are clean</div>
      {:else}
        {#each failedJobs.slice(0, 4) as job, i (job.id)}
          {@const failedOutcome = (job.result as { outcomes?: { state: string; error?: string }[] } | null)?.outcomes?.find((o) => o.state === 'failed')}
          {@const rawErr = job.error ?? failedOutcome?.error ?? ''}
          {@const errText = rawErr ? humanizeError(null, rawErr) : ''}
          {@const target = movies.find((m) => jobPaths(job).some((jp) => pathEq(m.path, jp)))}
          <div class="fail-row">
            <span class="mono bad">#{job.id} {job.kind}</span>
            <!-- the error is the diagnosis: a long one becomes a button that
                 expands inline, so keyboard users can actually read it -->
            {#if errText.length > 60}
              <button
                class="mono dim err err-expandable"
                onclick={() => (expandedErr = expandedErr === job.id ? null : job.id)}
                aria-expanded={expandedErr === job.id}
                title={errText}
              >
                {errText}
              </button>
            {:else}
              <span class="mono dim err">{errText}</span>
            {/if}
            {#if target}
              <button class="link mono" onclick={() => onopenmovie(target)} aria-label="Open details for {target.name}">DETAILS</button>
            {/if}
            <button class="link mono" onclick={() => ondismiss(job.id)} aria-label="Dismiss failed job #{job.id} from the overview">DISMISS</button>
          </div>
        {/each}
        {#if failedJobs.length > 4}
          <button class="link legend more" onclick={() => ongoto('jobs')}>{failedJobs.length - 4} more →</button>
        {/if}
      {/if}
    </section>
    <!-- ══ missing files ══ -->
    <section class="panel">
      <div class="panel-head">
        <span class="legend">MISSING FILES</span>
      </div>
      {#if missingMovies.length === 0}
        <div class="dim pad">every file accounted for at the last scan</div>
      {:else}
        {#each missingMovies.slice(0, 4) as movie (movie.path)}
          <div class="fail-row">
            <span class="mono bad">{movie.name}</span>
            <span class="mono dim err">absent since {movie.missing_since?.slice(0, 10) ?? 'last scan'}</span>
            <button class="link mono" onclick={() => onopenmovie(movie)} aria-label="Open details for missing file {movie.name}">DETAILS</button>
          </div>
        {/each}
        {#if missingMovies.length > 4}
          <button class="link legend more" onclick={() => ongoto('library')}>{missingMovies.length - 4} more →</button>
        {/if}
      {/if}
    </section>

    <!-- ══ setup ══ -->
    <section class="panel">
      <div class="panel-head">
        <span class="legend">SETUP</span>
        <button class="nav-link" onclick={() => ongoto('config')}>CONFIG →</button>
      </div>
      <div class="setup mono">
        <div class="fact"><span class="legend">SCAN FROM</span><span class="src">{scanPaths.join(' · ') || '—'}</span></div>
        <div class="fact"><span class="legend">WRITE TO</span><span class="src">{outputDir || '—'}</span></div>
        <div class="fact"><span class="legend">DATABASE</span><span class="src">{status?.database ?? '—'}</span></div>
        <div class="fact"><span class="legend">WATCHER</span><span class="src">{status?.watcher ?? '—'}</span></div>
        {#if status?.rss_mib != null}
          <div class="fact rss-fact">
            <span class="legend">RESIDENT MEMORY</span>
            {#if rssSpark}
              <!-- the trend, not just the instant: a leak announces
                   itself here before the log does -->
              <span class="rss-wrap" title="resident {rssSpark.last.toFixed(1)} MiB · min {rssSpark.min.toFixed(1)} · max {rssSpark.max.toFixed(1)} MiB · {rssSpark.n} samples this session">
                <svg viewBox="0 0 {rssSpark.w} {rssSpark.h}" preserveAspectRatio="none" aria-hidden="true">
                  <polyline points={rssSpark.points} class="rss-line" fill="none"></polyline>
                </svg>
                <span class="mono">{rssSpark.last.toFixed(1)} MiB</span>
              </span>
            {:else}
              <span class="src">{status.rss_mib.toFixed(1)} MiB</span>
            {/if}
          </div>
        {/if}
      </div>
    </section>

    <!-- ══ measured quality: the score that justifies the whole product ══ -->
    <!-- lives in the attention row (4th panel): the user asked for the avg
         VMAF "as a small card like the attention cards" -->
    <section class="panel">
      <div class="panel-head">
        <span class="legend">MEASURED QUALITY — VMAF {targetVmaf} TARGET</span>
      </div>
      {#if measured.length === 0}
        <div class="dim pad">nothing measured yet — plan predicts, encode measures</div>
      {:else}
        <div class="quality mono">
          <div class="q-head">
            <span class="q-big" class:warn={(minVmaf ?? 100) < targetVmaf}>{meanVmaf?.toFixed(1)}</span>
            <span class="q-label legend">MEAN</span>
            {#if measured.length > 1}
              <!-- n=1 duplicates its own row ("min 90.3 · 1 measured" over
                   the single row carrying exactly that) — the sub-line only
                   earns its place with a spread to summarize -->
              <span class="q-sub mono dim">min {minVmaf?.toFixed(1)} · {measured.length} measured</span>
            {/if}
          </div>
          {#each measured.slice(0, 6) as m (m.path)}
            <div class="q-row" title="{m.name}: VMAF {m.encode?.vmaf?.toFixed(1)} (target {targetVmaf})">
              <span class="q-name">{m.name}</span>
              <span class="q-bar"><span class="q-fill" style="width: {Math.min(100, ((m.encode?.vmaf ?? 0) / 100) * 100)}%"></span></span>
              <span class="q-v mono" class:warn={(m.encode?.vmaf ?? 100) < targetVmaf}>{m.encode?.vmaf?.toFixed(1)}</span>
            </div>
          {/each}
          {#if measured.length > 6}
            <button class="link legend more" onclick={() => ongoto('library')}>{measured.length - 6} more →</button>
          {/if}
        </div>
      {/if}
    </section>

    <!-- ══ retained originals: the space the operator may reclaim ══ -->
    <!-- the ledger's rows are the Retained page's job; the overview totals
         them so the pile is visible without a click (issue #182). Show
         always — the pile IS actionable (delete in the file manager) — but
         a zero pile reads as noise, so it renders only when present rows
         exist -->
    {#if retainedFiles.filter((f) => f.present).length > 0}
      {@const present = retainedFiles.filter((f) => f.present)}
      {@const totalBytes = present.reduce((sum, f) => sum + f.size_before, 0)}
      <section class="panel">
        <div class="panel-head">
          <span class="legend">RETAINED ORIGINALS — RECLAIMABLE</span>
          <button class="nav-link" onclick={() => ongoto('retained')}>RETAINED →</button>
        </div>
        <div class="dim pad mono">
          {present.length} original{present.length === 1 ? '' : 's'} · {fmtGb(totalBytes)} held aside —
          each has a verified encode in the library; delete them in your file manager when satisfied
        </div>
      </section>
    {/if}
  </div>

  <!-- ══ per-category bars: where the bytes live ══ -->
  <section class="panel wide">
    <div class="panel-head">
      <span class="legend">LIBRARY BY CATEGORY — SIZE AND RECLAIMED</span>
      <!-- the two visual encodings, stated once (round-6: the bars were
           never explained — grey vs teal hatch was tribal knowledge) -->
      <span class="legend bar-legend">BAR = SIZE · TEAL HATCH = RECLAIMED</span>
      <button class="nav-link" onclick={() => ongoto('library')}>LIBRARY →</button>
    </div>
    <div class="bars">
      <!-- top-8 cap orders the chart; the expander reveals the rest IN
           PANEL — a "more →" jump left the graph cut off (round-6) -->
      {#each visibleCats as bar (bar.name)}
        <div class="bar-row" title="{bar.name}: {bar.titles} titles · {bar.sizeGb.toFixed(1)} GB · {bar.reclaimedGb.toFixed(1)} GB reclaimed">
          <span class="bar-name">{bar.name}</span>
          <div class="bar-track">
            <div class="bar-size" style="width: {bar.sizeGb > 0 ? Math.max((bar.sizeGb / maxCatSize) * 100, 2) : 0}%"></div>
            <!-- hatch overlays the BAR's extent (same global-max scale):
                 share-of-category scaling let the hatch outrun the grey
                 bar it belongs to — claiming track space the category
                 does not have. The 1.5% floor keeps sub-1% wins findable
                 (exact figures live in the nums + row title). -->
            <div class="bar-reclaimed" style="width: {bar.reclaimedGb > 0 ? Math.max((bar.reclaimedGb / maxCatSize) * 100, 1.5) : 0}%"></div>
          </div>
          <span class="bar-nums mono dim">{bar.titles} · {bar.sizeGb.toFixed(0)} GB{bar.reclaimedGb > 0 ? ` · ${fmtReclaimed(bar.reclaimedGb, bar.sizeGb)}` : ''}</span>
        </div>
      {:else}
        <div class="dim pad">no titles scanned yet</div>
      {/each}
      {#if catBars.length > 8}
        <button class="link legend more" onclick={() => (showAllCats = !showAllCats)}>
          {showAllCats ? 'SHOW LESS' : `SHOW ${catBars.length - 8} MORE`}
        </button>
      {/if}
    </div>
  </section>

  <!-- ══ plan results: the prediction, visible ══ -->
  {#if planEntries.length > 0}
    <section class="panel wide">
      <div class="panel-head">
        <span class="legend">LAST PLAN — RUN #{lastPlan?.id}{planAge ? ` · ${planAge}` : ''}</span>
        <span class="mono dim">{planSummary}</span>
      </div>
      <div class="plan-list">
        <!-- SR context: the visual head row is aria-hidden (the round-4
             pattern), so screen readers get the column contract here —
             same pattern as the library table's sr-only caption -->
        <span class="sr-only">The plan table: per title the planned state, CRF, VMAF, size and saving, and the measured values once encoded. Measured cells name their predicted value in the tooltip.</span>
        <div class="plan-row plan-head mono legend" aria-hidden="true">
          <span class="p-name">TITLE</span>
          <span>STATE</span>
          <span class="p-num">CRF</span>
          <span class="p-num">VMAF</span>
          <span class="p-num">SIZE</span>
          <span class="p-num">SAVED</span>
          <span class="p-reason"></span>
        </div>
        {#each planEntries.slice(0, 8) as entry (entry.movie)}
          {@const done = alreadyEncoded(entry.movie)}
          {@const rec = done ? movieFor(entry.movie)?.encode : null}
          {@const plannedCrf = entry.crf ?? null}
          {@const crfDelta = rec?.crf != null && plannedCrf != null ? rec.crf - plannedCrf : null}
          {@const plannedSaved = entry.saved_percent ?? null}
          {@const savedDelta = rec?.saved_percent != null && plannedSaved != null ? rec.saved_percent - plannedSaved : null}
          {@const verdict =
            rec == null
              ? entry.reason ?? entry.error ?? ''
              : entry.state === 'skip'
                ? 'planned skip — encoded anyway'
                : rec.vmaf == null
                  ? plannedSaved != null && rec.saved_percent != null
                    ? // fixed-mode rows kept their measured truth dormant —
                      // the juxtaposition must fire for them too (round-6 P3)
                      `plan ${plannedSaved.toFixed(0)}% → fixed-mode ${rec.saved_percent.toFixed(1)}% (unmeasured)`
                    : 'encoded (fixed mode — no VMAF measurement)'
                  : savedDelta != null && savedDelta < -2
                    ? `plan ${plannedSaved?.toFixed(0)}% → measured ${rec.saved_percent?.toFixed(1)}%`
                    : plannedSaved != null
                      ? `plan ${plannedSaved.toFixed(0)}% · measured ${rec.saved_percent?.toFixed(1)}%`
                      : 'measured'
          }
          <div class="plan-row mono" class:plan-done={done} title={done ? 'the prediction has been overtaken by a measurement — the row shows the measured values' : ''}>
            <span class="p-name">{entry.movie}</span>
            <span class="chip {done ? 'done' : entry.state === 'encode' ? 'planned' : entry.state === 'skip' ? 'skip' : 'failed'}">
              <span class="swatch"></span>{done ? 'ENCODED' : entry.state.toUpperCase()}
            </span>
            <span class="p-num" class:measured={done && rec?.crf != null} title={done && rec?.crf != null ? 'measured value — the plan predicted ' + (entry.crf ?? '—') : 'planned value'}>
              {#if done}{rec?.crf ?? '—'}{#if crfDelta != null && crfDelta !== 0}<span class="delta-mark"> ({crfDelta > 0 ? '+' : '−'}{Math.abs(crfDelta)})</span>{/if}{:else}{entry.crf ?? '—'}{/if}
            </span>
            <span class="p-num" class:measured={done && rec?.vmaf != null} title={done && rec?.vmaf != null ? 'measured value — the plan predicted ' + (entry.vmaf?.toFixed(1) ?? '—') : 'planned value'}>
              {#if done}{rec?.vmaf?.toFixed(1) ?? '—'}{:else}{entry.vmaf?.toFixed(1) ?? '—'}{/if}
            </span>
            <span class="p-num" class:measured={done && rec?.output_size != null} title={done && rec?.output_size != null ? 'measured value — the plan predicted ' + (entry.estimated_size != null ? fmtGb(entry.estimated_size) : '—') : 'planned value'}>
              {#if done}{fmtGb(rec?.output_size ?? null)}{:else}{entry.estimated_size != null ? fmtGb(entry.estimated_size) : '—'}{/if}
            </span>
            <span class="p-num" class:measured={done && rec?.vmaf != null} title={done && rec?.saved_percent != null ? 'measured value — the plan predicted ' + (plannedSaved != null ? plannedSaved.toFixed(0) + '%' : '—') : 'planned value'}>
              {#if done}
                {@const saved = fmtSavedHonest(rec?.saved_percent)}
                {saved.text}
              {:else}
                {plannedSaved != null ? `${plannedSaved.toFixed(0)}%` : '—'}
              {/if}
            </span>
            <span class="dim p-reason">{verdict}</span>
          </div>
        {/each}
      </div>
    </section>
  {/if}
</div>

<style>
  .page { display: flex; flex-direction: column; gap: 14px; }
  .page-head h2 { font-size: var(--fs-page); font-weight: 700; }
  .page-head .legend { margin-top: 2px; }
  .dim { color: var(--ink-dim); }
  .warn { color: var(--amber); }
  .bad { color: var(--red); }
  .amber { color: var(--amber); }

  .sysline {
    display: flex;
    gap: 16px;
    align-items: baseline;
    font-size: 10.5px;
    letter-spacing: 0.4px;
    padding: 0 2px;
    flex-wrap: wrap;
  }
  .sysline .spacer { flex: 1; }

  /* stat band — dense, tile-sized, data-first; the band hugs its cards
     (a capped auto-fit left the unused tracks as dead seam ground —
     the "whitespace in the bar", round-6 user report) */
  .stats {
    display: flex;
    flex-wrap: wrap;
    gap: 1px;
    background: var(--seam);
    border: 1px solid var(--seam);
    border-radius: var(--radius);
    overflow: hidden;
    width: fit-content;
    max-width: 100%;
  }
  .stat {
    flex: 0 0 auto;
    min-width: 128px;
    max-width: 220px;
    display: flex;
    flex-direction: column;
    gap: 2px;
    background: var(--panel);
    padding: 9px 12px;
    text-align: left;
    color: inherit;
    font: inherit;
    cursor: pointer;
    border: none;
  }
  .stat:hover { background: var(--panel-raised); }
  .stat-v { font-size: 22px; font-weight: 600; }
  .stat-sub { font-size: 10px; }

  /* running band */
  .running {
    border: 1px solid var(--seam);
    border-radius: var(--radius);
    background: repeating-linear-gradient(45deg, var(--teal-glow) 0 4px, transparent 4px 8px), var(--panel);
    padding: 12px 16px;
    display: flex;
    flex-direction: column;
    gap: 6px;
  }
  .running-head { display: flex; justify-content: space-between; align-items: center; }
  .running-title { font-weight: 600; font-size: 14px; }
  .trace {
    height: 10px;
    border-radius: 5px;
    background: var(--hatch-work);
    overflow: hidden;
  }
  .fill { height: 100%; background: var(--teal); }
  .detail { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .quiet-strip {
    display: flex;
    justify-content: space-between;
    align-items: center;
    border: 1px dashed var(--seam);
    border-radius: var(--radius);
    padding: 10px 16px;
  }
  .done-strip { border-style: solid; border-color: var(--teal); background: var(--panel-active); }
  .done-strip .legend { margin-right: 8px; }

  /* columns */
  .cols {
    display: grid;
    /* the four attention panels form a 2×2: auto-fit wrapped them 3+1 and
       orphaned the fourth with two-thirds dead ground (round-5 P2) */
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 10px;
  }
  @media (max-width: 640px) {
    /* single column below 640px — with the 2-col clamps repeated: a bare
       `1fr` track does NOT clamp min-content, so the nowrap error text
       blew the page out to a 90,000px horizontal scroll (round-6 P1) */
    .cols {
      grid-template-columns: minmax(0, 1fr);
    }
  }
  .panel.wide { grid-column: 1 / -1; }

  /* category bars */
  .bars { display: flex; flex-direction: column; gap: 4px; padding: 10px 14px 14px; }
  .bar-row {
    display: grid;
    grid-template-columns: 150px 1fr 170px;
    gap: 10px;
    align-items: center;
  }
  .bar-name {
    font-size: 11.5px;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .bar-track {
    position: relative;
    height: 12px;
    background: var(--ground);
    border: 1px solid var(--row-line);
    border-radius: 2px;
    overflow: hidden;
  }
  .bar-size {
    position: absolute;
    inset: 0 auto 0 0;
    background: var(--seam);
    border-right: 1px solid var(--ink-dim);
  }
  .bar-reclaimed {
    position: absolute;
    inset: 0 auto 0 0;
    background: repeating-linear-gradient(45deg, var(--teal-glow) 0 3px, transparent 3px 6px);
    border-right: 1px solid var(--teal);
    box-shadow: inset -1px 0 0 var(--teal);
  }
  .bar-nums { font-size: 10px; text-align: right; white-space: nowrap; }

  /* plan list */
  .plan-list { display: flex; flex-direction: column; padding: 4px 14px 14px; }
  .plan-row {
    display: grid;
    /* SAVED at 88px: '+26.3% larger' measures ~86px in 11px mono — 62px
       wrapped it onto two lines despite the round-5 comment claiming one
       (and 74px still wrapped the longest honest-growth label) */
    grid-template-columns: minmax(140px, 1.4fr) 110px 40px 50px 70px 88px 1fr;
    gap: 10px;
    align-items: center;
    padding: 4px 0;
    border-bottom: 1px solid var(--row-line);
    font-size: 11px;
  }
  .plan-row:last-child { border-bottom: none; }
  /* a plan row overtaken by a measurement: shows the MEASURED values */
  .plan-row.plan-done { opacity: 0.8; }
  .p-num.measured { color: var(--teal-ink); }
  .delta-mark { color: var(--ink-faint); font-size: 10px; }
  .plan-head {
    border-bottom: 1px solid var(--seam);
    color: var(--ink-faint);
    letter-spacing: 1px;
    font-size: 10px;
  }
  .plan-head .p-name { font-weight: 400; }
  .p-name { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-weight: 600; }
  .p-num { text-align: right; }
  /* sr-only column context (plan/enc heads are aria-hidden by design) */
  .sr-only {
    position: absolute;
    width: 1px;
    height: 1px;
    padding: 0;
    margin: -1px;
    overflow: hidden;
    clip: rect(0 0 0 0);
    white-space: nowrap;
    border: 0;
  }
  .p-reason {
    font-size: 10px;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .panel {
    border: 1px solid var(--seam);
    border-radius: var(--radius);
    background: var(--panel);
    display: flex;
    flex-direction: column;
  }
  .panel-head {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 9px 14px;
    border-bottom: 1px solid var(--row-line);
    gap: 12px;
  }
  .bar-legend { color: var(--ink-faint); white-space: nowrap; }
  .pad { padding: 16px 14px; }
  .fail-row {
    display: flex;
    align-items: baseline;
    gap: 10px;
    padding: 7px 14px;
    border-bottom: 1px solid var(--row-line);
    font-size: var(--fs-data);
  }
  .fail-row:last-of-type { border-bottom: none; }
  .err { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; min-width: 0; }
  /* long diagnoses must wrap when the grid collapses to one column — the
     nowrap ellipsis only works above the 640px breakpoint (round-6 P1) */
  @media (max-width: 640px) {
    .err {
      white-space: normal;
      word-break: break-word;
    }
  }
  /* keyboard-expandable error: the button resets to the span look, expanded
     state wraps the full diagnosis */
  button.err-expandable {
    background: transparent;
    border: none;
    padding: 0;
    font: inherit;
    color: var(--ink-dim);
    cursor: pointer;
    text-align: left;
  }
  button.err-expandable[aria-expanded='true'] { white-space: normal; word-break: break-word; }
  .link {
    background: transparent;
    border: none;
    color: var(--teal-ink);
    cursor: pointer;
    padding: 0;
    font-size: inherit;
    letter-spacing: inherit;
    text-transform: inherit;
  }
  .link:hover { text-decoration: underline; }

  /* the ONE cross-page navigation style: page name + arrow, quiet link,
     teal ink, underline on hover — band buttons and panel heads alike */
  .nav-link {
    background: transparent;
    border: none;
    color: var(--teal-ink);
    cursor: pointer;
    padding: 0;
    font: inherit;
    font-size: var(--fs-legend);
    letter-spacing: var(--track-legend);
    text-transform: uppercase;
    white-space: nowrap;
  }
  .nav-link:hover { text-decoration: underline; }
  .head-tools { display: flex; gap: 12px; align-items: center; }
  .more { padding: 8px 14px; text-align: left; }
  .setup { display: flex; flex-direction: column; gap: 6px; padding: 10px 14px 14px; }
  .fact { display: flex; justify-content: space-between; gap: 12px; }
  .src { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; max-width: 320px; }
  /* the memory sparkline: trend beside the instant figure */
  .rss-fact { align-items: center; }
  .rss-wrap { display: inline-flex; align-items: center; gap: 8px; }
  .rss-wrap svg { width: 90px; height: 16px; }
  .rss-line { stroke: var(--teal); stroke-width: 1.5; }

  /* measured quality panel */
  .quality { padding: 8px 14px 12px; display: flex; flex-direction: column; gap: 4px; }
  .q-head { display: flex; align-items: baseline; gap: 8px; margin-bottom: 4px; }
  .q-big { font-size: 26px; font-weight: 600; color: var(--teal-ink); }
  .q-big.warn { color: var(--amber); }
  .q-label { color: var(--ink-faint); }
  .q-sub { margin-left: auto; font-size: 10px; }
  .q-row { display: grid; grid-template-columns: minmax(0, 1fr) 90px 38px; gap: 8px; align-items: center; }
  .q-name { font-size: 10.5px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .q-bar { height: 6px; background: var(--ground); border: 1px solid var(--row-line); border-radius: 3px; overflow: hidden; }
  .q-fill { display: block; height: 100%; background: var(--teal); }
  .q-v { font-size: 10.5px; text-align: right; }

</style>

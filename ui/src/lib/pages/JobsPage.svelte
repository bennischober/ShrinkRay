<script lang="ts">
  import { humanizeError, failedOutcomeNote, type Job, type JobOutcome, type Movie } from '../api'
  import { bytes, crf, eta, groupJobs, isStageRow, kindWord, relative, resultSummary, span, stateWord, timeColumns, vmaf } from '../format'
  import { fileProgressFor, progressEta } from '../progress.svelte'
  import EmptyState from '../components/EmptyState.svelte'
  import FileName from '../components/FileName.svelte'
  import PageHeader from '../components/PageHeader.svelte'
  import QueueOutlook from '../components/QueueOutlook.svelte'
  import StatusBadge from '../components/StatusBadge.svelte'
  import WorkProgress from '../components/WorkProgress.svelte'
  import type { Tone } from '../components/tone'

  /** The run log as an event log: every job with its timestamps, wall
   * duration, and expandable payload/result — the tool's audit trail. */

  let {
    history,
    movies = [],
    recent = [],
    targetVmaf = 96,
    onopenMovie,
    stale = false,
  }: {
    history: Job[]
    /** the library, for the running file's source size */
    movies?: Movie[]
    /** the last real encodes by recency, for the queue outlook */
    recent?: { encode_seconds: number; input_size: number }[]
    targetVmaf?: number
    /** Open a movie's detail page (a row's movie link, single-movie or the
     * running file, names its title). */
    onopenMovie: (path: string) => void
    /** The shell's data is no longer current: the clock stops so a frozen
     * elapsed time and ETA cannot keep looking live (issue #629). */
    stale?: boolean
  } = $props()

  const progress = (job: Job) => fileProgressFor(job.id)

  /** The library's movie paths, the directory guard below: a payload path is a
   * title only when the snapshot's library knows it (issue #703). */
  const moviePaths = $derived(new Set(movies.map((m) => m.path)))

  /** The movie a row may link to. A running job names the file it is
   * working on through the shared progress model (#631); a single-movie job
   * names its one target in the payload. A folder job's single payload path
   * is a directory, so it links only when the library knows the path as a
   * movie — a directory is never a title page (issue #703). */
  function singleMoviePath(job: Job): string | null {
    const running = job.state === 'running' ? progress(job)?.path ?? null : null
    if (running) return running
    const paths = job.payload.paths as string[] | undefined
    if (paths?.length !== 1) return null
    return moviePaths.has(paths[0]) ? paths[0] : null
  }

  /** The path the FILE cell shows: the job's own target, so a folder job
   * keeps showing its folder while the running file gets its own line. */
  function rowPath(job: Job): string | null {
    const paths = job.payload.paths as string[] | undefined
    return paths?.length === 1 ? paths[0] : null
  }

  let now = $state(Date.now())
  $effect(() => {
    if (stale) return
    if (!history.some((j) => j.state === 'running')) return
    const timer = setInterval(() => (now = Date.now()), 1000)
    return () => clearInterval(timer)
  })

  function liveElapsed(job: Job): string | null {
    if (job.state !== 'running' || !job.started_at) return null
    const iso = job.started_at.endsWith('Z') || job.started_at.includes('+')
      ? job.started_at
      : job.started_at + 'Z'
    const seconds = Math.max(0, Math.floor((now - Date.parse(iso)) / 1000))
    const h = Math.floor(seconds / 3600)
    const m = Math.floor((seconds % 3600) / 60)
    const s = seconds % 60
    return h > 0 ? `${h}h${String(m).padStart(2, '0')}m` : `${m}m${String(s).padStart(2, '0')}s`
  }

  const queuedNote = (job: Job) => {
    if (job.state !== 'queued') return ''
    const p = job.payload as Record<string, unknown>
    const parts: string[] = []
    if (p.quality) parts.push(p.target_vmaf != null ? `quality · VMAF>${p.target_vmaf}` : 'quality')
    else if (job.kind === 'encode') parts.push('fixed preset')
    if (p.remove_bars) parts.push('bars removed')
    return parts.join(' · ')
  }

  /** A DONE job whose per-movie outcomes failed is not a clean done: the
   * lifecycle completed, but the work did not. The row says so in red — a
   * green DONE must never hide failed work (the resident app owns job
   * control, so the dashboard renders no RETRY). The helper
   * lives in api.ts (shared with the detail trail). */
  function stateClass(job: Job): string {
    if (job.state === 'done') {
      // a DONE with failed outcomes is a warning, not a confirmation —
      // teal would say "clean" while the row says PARTIAL
      return failedOutcomeNote(job) !== '' ? 'partial' : 'ok'
    }
    if (job.state === 'interrupted') return 'warn'
    return job.state === 'failed' ? 'bad' : job.state === 'running' ? 'run' : 'dim'
  }

  // expandable detail (payload + result JSON)
  let expanded = $state<Set<number>>(new Set())
  function toggleExpand(id: number | null) {
    if (id === null) return
    const next = new Set(expanded)
    if (next.has(id)) next.delete(id)
    else next.add(id)
    expanded = next
  }
  const expandedJobs = $derived(history.filter((j) => j.id !== null && expanded.has(j.id)))

  // kind/state filter: 19 entries and 6 failures made the scan manual
  // (round-5 H7) — two selects, the same pattern as the library toolbar.
  // Filter state persists per session (library sort persists; the jobs
  // filters resetting on F5 was the round-6 granularity inconsistency)
  type KindFilter = 'all' | 'scan' | 'plan' | 'decision' | 'crf-search' | 'encode' | 'legacy'
  type StateFilter = 'all' | 'running' | 'queued' | 'done' | 'partial' | 'failed' | 'cancelled' | 'interrupted'
  const storedKind = sessionStorage.getItem('ms.jobsKind')
  const storedState = sessionStorage.getItem('ms.jobsState')
  let kindFilter = $state<KindFilter>(
    ['all', 'scan', 'plan', 'decision', 'crf-search', 'encode', 'legacy'].includes(storedKind ?? '') ? (storedKind as KindFilter) : 'all'
  )
  let stateFilter = $state<StateFilter>(
    ['all', 'running', 'queued', 'done', 'partial', 'failed', 'cancelled', 'interrupted'].includes(storedState ?? '')
      ? (storedState as StateFilter)
      : 'all'
  )
  $effect(() => {
    sessionStorage.setItem('ms.jobsKind', kindFilter)
    sessionStorage.setItem('ms.jobsState', stateFilter)
  })
  const isPartial = (j: Job) => j.state === 'done' && failedOutcomeNote(j) !== ''
  /** The per-movie outcomes a finished row summarises; a plan writes `plans`
   * where an encode writes `outcomes` (`jobs.py`), and both are the same shape.
   * Scans and legacy rows carry neither, so they show no summary. */
  const outcomesOf = (j: Job) => {
    const r = j.result as { outcomes?: JobOutcome[]; plans?: JobOutcome[] } | null
    return r?.outcomes ?? r?.plans ?? []
  }

  /** A scan reports the paths it could not read; without this the only trace
   * of them was the raw JSON (issue #632 review). */
  const scanErrorsOf = (j: Job) =>
    ((j.result as { errors?: string[] } | null)?.errors ?? [])

  const filteredHistory = $derived(
    history.filter((j) => {
      if (kindFilter !== 'all' && j.kind !== kindFilter) return false
      if (stateFilter === 'partial') return isPartial(j)
      // "done" means clean-done: a DONE with failed outcomes is its own
      // partial state (round-6: the done filter hid the failures it held)
      if (stateFilter === 'done') return j.state === 'done' && !isPartial(j)
      if (stateFilter !== 'all' && j.state !== stateFilter) return false
      return true
    })
  )

  // issue #632: two lists, not one dump — what is running above, what
  // happened below. A per-file stage row is the running folder job's own work
  // (#602): the folder job shows its progress, so the stage row is not a job of
  // its own and must not appear as a second one doing the same thing.
  // The order inside ACTIVE stays the operator's: running first, then the queue
  // oldest-first, because the snapshot is newest-first.
  const groups = $derived(groupJobs(filteredHistory.filter((j) => !isStageRow(j))))
  const activeRows = $derived([...groups.running, ...groups.queued])
  const pastRows = $derived(groups.history)
</script>

<div class="page page-in">
  <PageHeader
    title="Jobs"
    description={`${filteredHistory.length} of ${history.length} entries · the full audit trail${history.length !== filteredHistory.length ? ` · stage rows are the running job's own work, not jobs` : ''}`}
  >
    {#if history.length > 0}
      <div class="filters">
        <select class="mono" bind:value={kindFilter} aria-label="Filter by kind">
          <option value="all">all kinds</option>
          <option value="scan">{kindWord('scan')}</option>
          <option value="plan">{kindWord('plan')}</option>
          <option value="decision">{kindWord('decision')}</option>
          <option value="crf-search">{kindWord('crf-search')}</option>
          <option value="encode">{kindWord('encode')}</option>
          <option value="legacy">{kindWord('legacy')}</option>
        </select>
        <select class="mono" bind:value={stateFilter} aria-label="Filter by state">
          <option value="all">all states</option>
          <option value="running">{stateWord('running')}</option>
          <option value="queued">{stateWord('queued')}</option>
          <option value="done">{stateWord('done')}</option>
          <option value="partial">{stateWord('partial')}</option>
          <option value="failed">{stateWord('failed')}</option>
          <option value="cancelled">{stateWord('cancelled')}</option>
          <option value="interrupted">{stateWord('interrupted')}</option>
        </select>
      </div>
    {/if}
  </PageHeader>

  {#if history.length === 0}
    <EmptyState title="No jobs yet" hint="the resident app fills this page as it works" />
  {:else if filteredHistory.length === 0}
    <EmptyState title="No entries match this filter" hint="clear a filter to see the rest of the trail" />
  {:else}
    {#snippet jobRow(job: Job)}
      {@const cols = timeColumns(job)}
        <div class="entry" class:running={job.state === 'running'} class:failed={job.state === 'failed'} class:partial={job.state === 'done' && failedOutcomeNote(job) !== ''} class:open={job.id !== null && expanded.has(job.id)}>
          <div class="row mono">
            <span class="id">{job.id}</span>
            <span class="kind">{kindWord(job.kind)}</span>
            <span class="file">
              {#if rowPath(job)}
                {#if singleMoviePath(job) === rowPath(job)}
                  <!-- single-movie jobs link to the title: the two audit
                       trails reference each other -->
                  <button class="movie-link" onclick={() => onopenMovie(rowPath(job)!)} title="Open the movie's detail page">
                    <FileName path={rowPath(job)!} />
                  </button>
                {:else}
                  <FileName path={rowPath(job)!} />
                {/if}
              {/if}
            </span>
            <span class="state"><StatusBadge
              label={`${stateWord(job.state)}${job.state === 'done' && failedOutcomeNote(job) !== '' ? ' · Partial' : ''}${liveElapsed(job) ? ` · ${liveElapsed(job)}` : ''}`}
              tone={stateClass(job) === 'ok' ? 'ok' : stateClass(job) === 'partial' ? 'work' : stateClass(job) === 'bad' ? 'fail' : stateClass(job) === 'warn' ? 'work' : stateClass(job) === 'run' ? 'work' : 'dim'}
            /></span>
            <span class="when" title={job.started_at ?? 'not started yet'}>{cols.started ? relative(job.started_at) : ''}</span>
            <span class="dur" data-work-duration title={job.state === 'running' ? 'the shared running-work ETA, same words as the summary' : undefined}>{cols.duration ? span(job.started_at, job.finished_at) : job.state === 'running' ? (eta(progressEta(progress(job))) || '…') : ''}</span>
            <span class="ops">
              <button class="mini" onclick={() => toggleExpand(job.id)} aria-expanded={job.id !== null && expanded.has(job.id)}>
                {job.id !== null && expanded.has(job.id) ? 'HIDE' : 'DETAIL'}
              </button>
            </span>
          </div>

          {#if job.state === 'running'}
            {@const p = progress(job)}
            {#if singleMoviePath(job) && singleMoviePath(job) !== rowPath(job)}
              <!-- the running file is a per-movie stage of the folder job
                   (#602/#631): name and link the title it is working on,
                   while the row's FILE cell keeps showing the folder -->
              <div class="running-file mono" data-running-movie>
                <a
                  class="movie-link"
                  href={`#/movie/${encodeURIComponent(singleMoviePath(job)!)}`}
                  onclick={() => onopenMovie(singleMoviePath(job)!)}
                  title="Open the movie's detail page"
                >
                  <FileName path={singleMoviePath(job)!} />
                </a>
              </div>
            {/if}
            <WorkProgress progress={p} {stale} detail {targetVmaf} sourceBytes={movies.find((m) => m.path === p?.path)?.size ?? null} />
          {:else if job.state === 'queued' && queuedNote(job)}
            <div class="mono dim note">{queuedNote(job)}</div>
          {:else if job.state === 'done' && failedOutcomeNote(job)}
            {@const note = failedOutcomeNote(job)}
            <div class="mono error">
              {note.replace('__FAILED_OUTCOMES__', '')}
            </div>
          {:else if job.state === 'done' && resultSummary(outcomesOf(job))}
            <div class="mono dim note">{resultSummary(outcomesOf(job))}</div>
          {:else if job.error}
            <!-- translated for the row (the operator's language); the raw
                 exception stays in the DETAIL expansion for diagnosis -->
            <div class="mono error">{humanizeError(null, job.error)}</div>
          {/if}

          {#if job.id !== null && expanded.has(job.id)}
            <div class="detail">
              {#if outcomesOf(job).length > 0}
                <div class="d-block">
                  <span class="legend">WHAT IT DID</span>
                  <div class="d-table">
                    {#each outcomesOf(job) as o (o.movie)}
                      <div class="d-row mono">
                        <span class="d-file" title={o.movie}>{o.movie}</span>
                        <span class="d-state">{stateWord(o.state)}</span>
                        {#if o.crf != null}<span class="d-num">CRF {crf(o.crf)}</span>{/if}
                        {#if o.vmaf != null}<span class="d-num">VMAF {vmaf(o.vmaf)}</span>{/if}
                        {#if typeof o.input_size === 'number' && typeof o.output_size === 'number'}
                          <span class="d-num">{bytes(o.input_size)} → {bytes(o.output_size)}</span>
                        {:else if typeof o.estimated_size === 'number'}
                          <span class="d-num">≈ {bytes(o.estimated_size)}</span>
                        {/if}
                        {#if o.reason}<span class="d-why">{o.reason}</span>{/if}
                        {#if o.error}<span class="d-why">{humanizeError(null, o.error)}</span>{/if}
                      </div>
                    {/each}
                  </div>
                </div>
              {/if}
              {#if ((job.payload.paths as string[] | undefined) ?? []).length > 0}
                <div class="d-block">
                  <span class="legend">TARGET</span>
                  {#each (job.payload.paths as string[] | undefined) ?? [] as path (path)}
                    <div class="mono d-path">{path}</div>
                  {/each}
                </div>
              {/if}
              {#if scanErrorsOf(job).length > 0}
                <div class="d-block">
                  <span class="legend">COULD NOT READ</span>
                  {#each scanErrorsOf(job) as e}
                    <div class="mono d-why">{e}</div>
                  {/each}
                </div>
              {/if}
              {#if job.error}
                <div class="d-block">
                  <span class="legend">ERROR</span>
                  <pre class="mono error">{job.error}</pre>
                </div>
              {/if}
              <div class="d-block">
                <span class="legend">TIMESTAMPS</span>
                <pre class="mono">created {relative(job.created_at)} · started {relative(job.started_at)} · finished {relative(job.finished_at)}</pre>
              </div>
              <!-- the issue's own wording: the raw payload/result JSON lives behind
                   a disclosure, for bug reports, not in the operator's way -->
              <details class="d-block">
                <summary class="legend">TECHNICAL DETAILS</summary>
                <pre class="mono">{JSON.stringify({ payload: job.payload, result: job.result }, null, 2)}</pre>
              </details>
            </div>
          {/if}
        </div>
    {/snippet}

    <!-- ACTIVE: what is running now and what comes next (issue #632). -->
    <section class="section">
      <div class="section-head legend">
        <span>ACTIVE</span>
        <span class="section-count mono">{activeRows.length} running or waiting</span>
      </div>
      <QueueOutlook jobs={groups.queued} {recent} />
      {#if activeRows.length === 0}
        <div class="dim pad">nothing running — the watcher is idle</div>
      {:else}
        <div class="log">
          <div class="log-head legend">
            <span>#</span>
            <span>KIND</span>
            <span>FILE</span>
            <span>STATE</span>
            <span>STARTED</span>
            <span>DURATION</span>
            <span></span>
          </div>
          {#each activeRows as job (job.id)}
            {@render jobRow(job)}
          {/each}
        </div>
      {/if}
    </section>

    <!-- HISTORY: what happened, newest first, on its own. -->
    <section class="section">
      <div class="section-head legend">
        <span>HISTORY</span>
        <span class="section-count mono">{pastRows.length} finished</span>
      </div>
      {#if pastRows.length === 0}
        <div class="dim pad">nothing finished yet</div>
      {:else}
        <div class="log">
          <div class="log-head legend">
            <span>#</span>
            <span>KIND</span>
            <span>FILE</span>
            <span>STATE</span>
            <span>STARTED</span>
            <span>DURATION</span>
            <span></span>
          </div>
          {#each pastRows as job (job.id)}
            {@render jobRow(job)}
          {/each}
        </div>
      {/if}
    </section>
  {/if}
</div>

<style>
  .page { display: flex; flex-direction: column; gap: 12px; }
      .filters { display: flex; gap: 8px; margin-top: 8px; }
  .filters select {
    background: var(--panel);
    color: var(--ink);
    border: 1px solid var(--seam);
    border-radius: var(--radius);
    padding: 3px 8px;
    font-size: 11px;
  }
  .section { display: flex; flex-direction: column; gap: 6px; margin-bottom: 18px; }
  .section-head {
    display: flex;
    align-items: baseline;
    gap: 10px;
    padding: 0 14px;
    color: var(--ink-dim);
  }
  .section-count { margin-left: auto; color: var(--ink-faint); }
  .dim { color: var(--ink-dim); }
    .movie-link {
    background: transparent;
    border: none;
    padding: 0;
    color: var(--teal-ink);
    cursor: pointer;
    font: inherit;
    text-decoration: none;
  }
  .movie-link:hover { text-decoration: underline; }
  .running-file { padding: 0 0 6px; min-width: 0; }
  .running-file .movie-link { display: inline-block; max-width: 100%; }
        .log {
    border: 1px solid var(--seam);
    border-radius: var(--radius);
    background: var(--panel);
    overflow-x: auto;
  }
  .log-head {
    display: grid;
    grid-template-columns: 44px 104px minmax(120px, 1fr) 150px 90px 112px 92px;
    gap: 10px;
    padding: 8px 14px;
    border-bottom: 1px solid var(--seam);
  }
  .entry { padding: 0 14px; border-bottom: 1px solid var(--row-line); }
  .entry:last-child { border-bottom: none; }
  .entry.running { background: repeating-linear-gradient(45deg, rgba(224, 164, 94, 0.08) 0 4px, transparent 4px 8px), var(--panel); }
  .entry.failed {
    /* the hatch follows the row's dominant truth: red hatch = failed run,
       amber hatch = PARTIAL (a done with failed outcomes) — the state text
       and the ground must agree (round-5: amber text on red hatch blurred
       two hue semantics on one row) */
    background: repeating-linear-gradient(45deg, rgba(242, 114, 101, 0.07) 0 4px, transparent 4px 8px), var(--panel);
  }
  .entry.partial {
    background: repeating-linear-gradient(45deg, rgba(224, 164, 94, 0.08) 0 4px, transparent 4px 8px), var(--panel);
  }
  .entry.open { background: var(--panel-raised); }
  .row {
    display: grid;
    grid-template-columns: 44px 104px minmax(120px, 1fr) 150px 90px 112px 92px;
    gap: 10px;
    align-items: baseline;
    padding: 7px 0;
  }
  .id { color: var(--ink-faint); }
  .kind { font-weight: 600; }
  /* issue #632: the filename has its own cell, so it can neither wrap
     character by character inside KIND nor draw over the start time;
     min-width 0 lets it shrink instead of widening the row */
  .file { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .when { color: var(--ink-dim); }
  .dur { color: var(--ink-dim); white-space: nowrap; }
  .ops { display: flex; gap: 6px; justify-content: flex-end; }
  .mini {
    background: transparent;
    border: 1px solid var(--seam);
    border-radius: 3px;
    color: var(--ink-dim);
    padding: 3px 10px;
    cursor: pointer;
    font-size: var(--fs-data); /* interactive text reads at data size (a11y pass) */
    letter-spacing: 1px;
    min-height: 24px;
    min-width: 24px;
  }
  .mini:hover { color: var(--ink); border-color: var(--ink-dim); }
  .note { padding: 2px 0 8px; }
  .error { padding: 2px 0 8px; color: var(--red); white-space: normal; word-break: break-word; }
  .detail {
    padding: 8px 0 14px;
    display: flex;
    flex-direction: column;
    gap: 10px;
    border-top: 1px solid var(--row-line);
  }
  .d-block .legend { display: block; margin: 8px 0 4px; }
  .d-table { display: flex; flex-direction: column; gap: 6px; }
  .d-row { display: flex; flex-wrap: wrap; gap: 12px; align-items: baseline; font-size: 10.5px; }
  .d-file { color: var(--ink); }
  .d-state { color: var(--ink-dim); }
  .d-num { color: var(--ink); }
  .d-why { color: var(--ink-dim); }
  .d-path { font-size: 10.5px; color: var(--ink-dim); padding: 1px 0; }
  .d-block pre {
    margin: 0;
    padding: 8px 10px;
    background: var(--ground);
    border: 1px solid var(--row-line);
    border-radius: 3px;
    font-size: 10.5px;
    line-height: 1.5;
    overflow: auto;
    max-height: 200px;
    white-space: pre-wrap;
    word-break: break-word;
  }
  /* below 768px a job becomes a card: the desktop columns clip on a phone and
     the filename wrapped into a 3-4 line column (issue #636) */
  @media (max-width: 768px) {
    .log-head { display: none; }
    .log { overflow-x: visible; }
    .entry { padding: 0 10px; }
    .row {
      grid-template-columns: auto 1fr auto;
      grid-template-areas:
        'id kind ops'
        'file file file'
        'state when dur';
      gap: 6px 10px;
      align-items: center;
      padding: 10px 0;
    }
    .id { grid-area: id; }
    .kind { grid-area: kind; }
    .file {
      grid-area: file;
      white-space: normal;
      overflow: visible;
      text-overflow: clip;
      word-break: break-word;
    }
    .state { grid-area: state; }
    .when { grid-area: when; }
    /* the shared ETA wording is longer than the old `~1 min`: let it wrap
       inside its auto track instead of widening the row past the phone */
    .dur { grid-area: dur; white-space: normal; overflow-wrap: anywhere; }
    .ops { grid-area: ops; justify-self: end; }
  }
</style>

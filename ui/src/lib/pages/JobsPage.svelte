<script lang="ts">
  import { fmtGb, fmtWhen, fmtSpan, humanizeError, failedOutcomeNote, type Job } from '../api'
  import { progressFor } from '../progress.svelte'

  /** The run log as an event log: every job with its timestamps, wall
   * duration, and expandable payload/result — the tool's audit trail. */

  let {
    history,
    onopenMovie,
  }: {
    history: Job[]
    /** Open a movie's detail page (single-movie jobs link to their title). */
    onopenMovie: (path: string) => void
  } = $props()

  /** True when the job targeted exactly one movie FILE (the UI's
   * per-movie actions) — such rows link to the title's detail page.
   * Folder-scoped jobs (scan-all, plan-all) link nothing: their payload
   * names a directory, not a title. */
  function singleMoviePath(job: Job): string | null {
    const paths = job.payload.paths as string[] | undefined
    return paths?.length === 1 ? paths[0] : null
  }

  const progress = (job: Job) => progressFor(job.id)

  let now = $state(Date.now())
  $effect(() => {
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
    if (p.quality) parts.push(`quality · VMAF>${p.target_vmaf ?? 95}`)
    else parts.push('fixed preset (removed mode)')
    if (p.remove_bars) parts.push('bars removed')
    return parts.join(' · ')
  }

  /** A DONE job whose per-movie outcomes failed is not a clean done: the
   * lifecycle completed, but the work did not. The row says so in red and
   * offers RETRY — a green DONE must never hide failed work. The helper
   * lives in api.ts (shared with the detail trail). */
  function stateClass(job: Job): string {
    if (job.state === 'done') {
      // a DONE with failed outcomes is a warning, not a confirmation —
      // teal would say "clean" while the row says PARTIAL
      return failedOutcomeNote(job) !== '' ? 'partial' : 'ok'
    }
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
  type KindFilter = 'all' | 'scan' | 'plan' | 'encode' | 'legacy'
  type StateFilter = 'all' | 'running' | 'queued' | 'done' | 'partial' | 'failed' | 'cancelled'
  const storedKind = sessionStorage.getItem('ms.jobsKind')
  const storedState = sessionStorage.getItem('ms.jobsState')
  let kindFilter = $state<KindFilter>(
    ['all', 'scan', 'plan', 'encode', 'legacy'].includes(storedKind ?? '') ? (storedKind as KindFilter) : 'all'
  )
  let stateFilter = $state<StateFilter>(
    ['all', 'running', 'queued', 'done', 'partial', 'failed', 'cancelled'].includes(storedState ?? '')
      ? (storedState as StateFilter)
      : 'all'
  )
  $effect(() => {
    sessionStorage.setItem('ms.jobsKind', kindFilter)
    sessionStorage.setItem('ms.jobsState', stateFilter)
  })
  const isPartial = (j: Job) => j.state === 'done' && failedOutcomeNote(j) !== ''
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
</script>

<div class="page page-in">
  <header class="page-head">
    <h2>Jobs</h2>
    <p class="legend">{filteredHistory.length} OF {history.length} ENTRIES · THE FULL AUDIT TRAIL</p>
    {#if history.length > 0}
      <div class="filters">
        <select class="mono" bind:value={kindFilter} aria-label="Filter by kind">
          <option value="all">all kinds</option>
          <option value="scan">scan</option>
          <option value="plan">plan</option>
          <option value="encode">encode</option>
          <option value="legacy">legacy</option>
        </select>
        <select class="mono" bind:value={stateFilter} aria-label="Filter by state">
          <option value="all">all states</option>
          <option value="running">running</option>
          <option value="queued">queued</option>
          <option value="done">done</option>
          <option value="partial">partial</option>
          <option value="failed">failed</option>
          <option value="cancelled">cancelled</option>
        </select>
      </div>
    {/if}
  </header>

  {#if history.length === 0}
    <div class="empty dim">no jobs yet — enqueue a scan to open the log</div>
  {:else if filteredHistory.length === 0}
    <div class="empty dim">no entries match this filter</div>
  {:else}
    <div class="log">
      <div class="log-head legend">
        <span>#</span>
        <span>KIND</span>
        <span>STATE</span>
        <span>STARTED</span>
        <span>DURATION</span>
        <span></span>
      </div>
      {#each filteredHistory as job (job.id)}
        <div class="entry" class:running={job.state === 'running'} class:failed={job.state === 'failed'} class:partial={job.state === 'done' && failedOutcomeNote(job) !== ''} class:open={job.id !== null && expanded.has(job.id)}>
          <div class="row mono">
            <span class="id">{job.id}</span>
            <span class="kind">
              {job.kind}
              {#if singleMoviePath(job)}
                <!-- single-movie jobs link to the title: the two audit
                     trails reference each other -->
                <button class="movie-link" onclick={() => onopenMovie(singleMoviePath(job)!)} title="Open the movie's detail page">
                  {(job.payload.paths as string[])[0].split(/[\\/]/).pop()}
                </button>
              {/if}
            </span>
            <span class="state {stateClass(job)}">
              {job.state.toUpperCase()}{#if job.state === 'done' && failedOutcomeNote(job) !== ''} · PARTIAL{/if}{#if liveElapsed(job)} · {liveElapsed(job)}{/if}
            </span>
            <span class="when" title={job.started_at ?? ''}>{fmtWhen(job.started_at ?? job.created_at)}</span>
            <span class="dur">{job.state === 'done' ? fmtSpan(job.started_at, job.finished_at) ?? '—' : job.state === 'running' ? '…' : '—'}</span>
            <span class="ops">
              <button class="mini" onclick={() => toggleExpand(job.id)} aria-expanded={job.id !== null && expanded.has(job.id)}>
                {job.id !== null && expanded.has(job.id) ? 'HIDE' : 'DETAIL'}
              </button>
            </span>
          </div>

          {#if job.state === 'running'}
            {@const p = progress(job)}
            <div class="trace" role="progressbar" aria-valuenow={p?.percent ?? undefined} aria-valuemin="0" aria-valuemax="100" aria-label="{job.kind} progress">
              <div class="fill" style="width: {p?.percent ?? 2}%"></div>
              <span class="mono reading">
                {p?.movie ? `${p.movie} · ` : ''}{p?.detail ?? 'starting…'}{p?.percent != null ? ` · ${p.percent}%` : ''}
              </span>
            </div>
          {:else if job.state === 'queued' && queuedNote(job)}
            <div class="mono dim note">{queuedNote(job)}</div>
          {:else if job.state === 'done' && failedOutcomeNote(job)}
            {@const note = failedOutcomeNote(job)}
            <div class="mono error">
              {note.replace('__FAILED_OUTCOMES__', '')}
            </div>
          {:else if job.error}
            <!-- translated for the row (the operator's language); the raw
                 exception stays in the DETAIL expansion for diagnosis -->
            <div class="mono error">{humanizeError(null, job.error)}</div>
          {/if}

          {#if job.id !== null && expanded.has(job.id)}
            <div class="detail">
              <div class="d-block">
                <span class="legend">PAYLOAD</span>
                <pre class="mono">{JSON.stringify(job.payload, null, 2)}</pre>
              </div>
              {#if job.result}
                <div class="d-block">
                  <span class="legend">RESULT</span>
                  <pre class="mono">{JSON.stringify(job.result, null, 2)}</pre>
                </div>
              {/if}
              <div class="d-block">
                <span class="legend">TIMESTAMPS</span>
                <pre class="mono">created {fmtWhen(job.created_at)} · started {fmtWhen(job.started_at)} · finished {fmtWhen(job.finished_at)}</pre>
              </div>
            </div>
          {/if}
        </div>
      {/each}
    </div>
  {/if}
</div>

<style>
  .page { display: flex; flex-direction: column; gap: 12px; }
  .page-head h2 { font-size: var(--fs-page); font-weight: 700; }
  .page-head .legend { margin-top: 2px; }
  .filters { display: flex; gap: 8px; margin-top: 8px; }
  .filters select {
    background: var(--panel);
    color: var(--ink);
    border: 1px solid var(--seam);
    border-radius: var(--radius);
    padding: 3px 8px;
    font-size: 11px;
  }
  .dim { color: var(--ink-dim); }
  .ok { color: var(--teal-ink); }
  .movie-link {
    background: transparent;
    border: none;
    padding: 0;
    color: var(--teal-ink);
    cursor: pointer;
    font: inherit;
  }
  .movie-link:hover { text-decoration: underline; }
  .partial { color: var(--amber); }
  .bad { color: var(--red); }
  .run { color: var(--amber); }
  .empty {
    padding: 60px 20px;
    text-align: center;
    border: 1px dashed var(--seam);
    border-radius: var(--radius);
  }
  .log {
    border: 1px solid var(--seam);
    border-radius: var(--radius);
    background: var(--panel);
    overflow-x: auto;
  }
  .log-head {
    display: grid;
    grid-template-columns: 44px 130px 150px 90px 80px 1fr;
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
    grid-template-columns: 44px 130px 150px 90px 80px 1fr;
    gap: 10px;
    align-items: baseline;
    padding: 7px 0;
  }
  .id { color: var(--ink-faint); }
  .kind { font-weight: 600; }
  .when { color: var(--ink-dim); }
  .dur { color: var(--ink-dim); }
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
  .trace {
    position: relative;
    height: 18px;
    margin: 2px 0 8px;
    background: var(--hatch-work);
    border: 1px solid var(--seam);
    border-radius: 3px;
    overflow: hidden;
  }
  .fill {
    position: absolute;
    left: 0; top: 0; bottom: 0;
    background: var(--hatch-progress);
    border-right: 2px solid var(--amber);
  }
  .reading {
    position: absolute;
    right: 6px; top: 2px;
    color: var(--ink);
    max-width: 85%;
    overflow: hidden;
    text-overflow: ellipsis;
  }
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
</style>

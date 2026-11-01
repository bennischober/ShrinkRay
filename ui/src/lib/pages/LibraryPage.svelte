<script lang="ts">
  import { fmtGb, fmtSavedHonest, type Movie } from '../api'

  let {
    movies,
    selectedPaths,
    targetVmaf,
    scanning = false,
    pending = false,
    runningMovie = null,
    runningPercent = null,
    plannedPaths = [],
    planSavings = [],
    onopenmovie,
    ontoggle,
    onselectall,
    onclear,
  }: {
    movies: Movie[]
    selectedPaths: Set<string>
    targetVmaf: number
    scanning?: boolean
    pending?: boolean
    runningMovie?: string | null
    runningPercent?: number | null
    plannedPaths?: string[]
    planSavings?: readonly (readonly [string, number])[]
    onopenmovie: (movie: Movie) => void
    ontoggle: (path: string, checked: boolean) => void
    onselectall: (checked: boolean, paths: string[]) => void
    onclear: () => void
  } = $props()

  // ── search + category (a select, not an exploding chip row) ──
  // Filter state persists per session: opening a detail page DESTROYS
  // this component ({#if detailMovie} unmounts every page), so in-memory
  // state died on the round-trip — the user lost their filtered view by
  // looking at a title and coming back (round-6 user report). Same
  // sessionStorage contract as sort.
  let query = $state(sessionStorage.getItem('ms.query') ?? '')
  let category = $state<string | null>(sessionStorage.getItem('ms.category'))
  let decision = $state<'all' | 'untouched' | 'planned' | 'encoded' | 'skipped' | 'missing'>(
    (sessionStorage.getItem('ms.decision') as DecisionState | null) ?? 'all'
  )
  $effect(() => {
    sessionStorage.setItem('ms.query', query)
    sessionStorage.setItem('ms.category', category ?? '')
    sessionStorage.setItem('ms.decision', decision)
  })
  // sorting: persisted per session via sessionStorage
  type SortKey = 'name' | 'category' | 'size' | 'vmaf' | 'saved'
  type DecisionState = 'all' | 'untouched' | 'planned' | 'encoded' | 'skipped' | 'missing'
  const storedKey = sessionStorage.getItem('ms.sortKey')
  let sortKey = $state<SortKey>(
    storedKey === 'name' || storedKey === 'category' || storedKey === 'size' || storedKey === 'vmaf' || storedKey === 'saved'
      ? storedKey
      : 'name'
  )
  let sortAsc = $state(sessionStorage.getItem('ms.sortAsc') !== 'false')

  $effect(() => {
    sessionStorage.setItem('ms.sortKey', sortKey)
    sessionStorage.setItem('ms.sortAsc', String(sortAsc))
  })

  const categories = $derived([...new Set(movies.map((m) => m.category))].sort())

  const filtered = $derived.by(() => {
    const q = query.trim().toLowerCase()
    const list = movies.filter((m) => {
      if (category && m.category !== category) return false
      if (decision !== 'all' && decisionOf(m).label.toLowerCase() !== decision) return false
      // search answers "which title is this?" across the identifying
      // fields — a category or folder fragment must reach its titles too
      // (round-6: name-only search left category/path unreachable)
      if (
        q &&
        !m.name.toLowerCase().includes(q) &&
        !m.category.toLowerCase().includes(q) &&
        !m.path.toLowerCase().includes(q)
      )
        return false
      return true
    })
    const dir = sortAsc ? 1 : -1
    list.sort((a, b): number => {
      switch (sortKey) {
        case 'name':
          return a.name.localeCompare(b.name) * dir
        case 'category':
          return a.category.localeCompare(b.category) * dir
        case 'size':
          return ((a.size ?? 0) - (b.size ?? 0)) * dir
        case 'vmaf':
          return ((a.encode?.vmaf ?? -1) - (b.encode?.vmaf ?? -1)) * dir
        case 'saved':
          return ((a.encode?.saved_percent ?? -1) - (b.encode?.saved_percent ?? -1)) * dir
      }
    })
    return list
  })

  function toggleSort(key: typeof sortKey) {
    if (sortKey === key) sortAsc = !sortAsc
    else {
      sortKey = key
      sortAsc = true
    }
  }
  function sortMark(key: string): string {
    if (sortKey !== key) return ''
    return sortAsc ? ' ▲' : ' ▼'
  }
  function ariaSort(key: string): 'ascending' | 'descending' | 'none' {
    if (sortKey !== key) return 'none'
    return sortAsc ? 'ascending' : 'descending'
  }

  const selectedCount = $derived(filtered.filter((m) => selectedPaths.has(m.path)).length)
  const selectedMovies = $derived(filtered.filter((m) => selectedPaths.has(m.path)))
  const selectedBase = $derived(
    selectedMovies.reduce((sum, m) => sum + (m.encode?.output_size ?? m.size ?? 0), 0)
  )
  // Measured, not guessed (issue #324): the plan's own predictions are the
  // only source of an estimate. A selection with no plan row, or one whose
  // prediction did not materialize, shows no number at all - a made-up 45%
  // would fabricate a promise on the trust surface.
  const savingsByPath = $derived(new Map(planSavings))
  const selectedEstimate = $derived.by(() => {
    let sum = 0
    for (const m of selectedMovies) {
      if (m.encode) continue
      const pct = savingsByPath.get(m.path) ?? savingsByPath.get(m.path.replace(/[\\/]+/g, '/'))
      if (pct == null) continue
      sum += ((m.size ?? 0) * pct) / 100
    }
    return sum
  })

  function decisionOf(movie: Movie): { label: string; cls: string; bare?: boolean } {
    const preset = movie.encode?.preset
    if (movie.missing) return { label: 'MISSING', cls: 'missing' }
    if (preset?.startsWith('skipped')) return { label: 'SKIPPED', cls: 'skip' }
    if (preset) return { label: 'ENCODED', cls: 'done' }
    if (plannedPaths.includes(movie.path)) return { label: 'PLANNED', cls: 'planned' }
    // the default state renders as a bare swatch: 354 identical bordered
    // chips diluted the states that actually need action
    return { label: 'UNTOUCHED', cls: 'untouched', bare: true }
  }
</script>

<div class="page page-in">
  <header class="page-head">
    <h2>Library</h2>
    <p class="legend">{filtered.length} OF {movies.length} TITLES · TARGET VMAF {targetVmaf}</p>
  </header>

  <!-- ══ toolbar: search + category + view toggle + scan ══ -->
  <div class="toolbar">
    <input
      class="search mono"
      type="search"
      placeholder="search titles · categories · paths…"
      bind:value={query}
      aria-label="Search titles, categories and paths"
    />
    <select class="cat mono" bind:value={category} aria-label="Filter by category">
      <option value={null}>All categories ({movies.length})</option>
      {#each categories as cat (cat)}
        <option value={cat}>{cat} ({movies.filter((m) => m.category === cat).length})</option>
      {/each}
    </select>
    <select class="cat mono" bind:value={decision} aria-label="Filter by decision state">
      <option value="all">All states</option>
      <option value="untouched">Untouched</option>
      <option value="planned">Planned</option>
      <option value="encoded">Encoded</option>
      <option value="skipped">Skipped</option>
      <option value="missing">Missing</option>
    </select>
    <span class="spacer"></span>
  </div>

  <!-- ══ bulk bar (only when a selection exists) ══ -->
  {#if selectedCount > 0}
    <div class="bulk-bar leader-in">
      <span class="mono sel-count">
        <input
          type="checkbox"
          checked={selectedCount === filtered.length}
          onchange={(e) => onselectall(e.currentTarget.checked, filtered.map((m) => m.path))}
          aria-label="Select all filtered"
        />
        {selectedCount} SELECTED
      </span>
      <span class="leader-note mono">
        {#if selectedEstimate > 0}
          → PLAN-PREDICTED −{(selectedEstimate / 1024 ** 3).toFixed(1)} GB · {fmtGb(selectedBase)} BASE
        {:else}
          → no estimate (selection already encoded)
        {/if}
      </span>
      <span class="spacer"></span>
      <button class="act quiet" onclick={onclear}>CLEAR</button>
    </div>
  {:else}
    <div class="bulk-bar idle">
      <span class="mono dim">{filtered.length} titles — tick checkboxes for bulk work</span>
      <span class="spacer"></span>
      <span class="mono dim">the app works through the library on its own — this page only shows where it stands</span>
    </div>
  {/if}

  <!-- ══ table view (the catalog is a table; the grid was demoted away) ══ -->
  <div class="table-wrap">
      <table>
        <caption class="sr-only">The library catalog: one row per title with size, decision state and encode metrics. Column heads are sort buttons.</caption>
        <thead>
          <tr>
            <th class="chk">
              <input
                type="checkbox"
                checked={filtered.length > 0 && filtered.every((m) => selectedPaths.has(m.path))}
                onchange={(e) => onselectall(e.currentTarget.checked, filtered.map((m) => m.path))}
                aria-label="Select all filtered"
              />
            </th>
            <th aria-sort={ariaSort('name')}>
              <button class="sort legend" onclick={() => toggleSort('name')} aria-pressed={sortKey === 'name'}>Title{sortMark('name')}</button>
            </th>
            <th aria-sort={ariaSort('category')}>
              <button class="sort legend" onclick={() => toggleSort('category')} aria-pressed={sortKey === 'category'}>Category{sortMark('category')}</button>
            </th>
            <th class="num" aria-sort={ariaSort('size')}>
              <button class="sort legend" onclick={() => toggleSort('size')} aria-pressed={sortKey === 'size'}>Size{sortMark('size')}</button>
            </th>
            <th class="legend">Decision</th>
            <th class="legend num">CRF</th>
            <th class="num" aria-sort={ariaSort('vmaf')}>
              <button class="sort legend" onclick={() => toggleSort('vmaf')} aria-pressed={sortKey === 'vmaf'}>VMAF{sortMark('vmaf')}</button>
            </th>
            <th class="num" aria-sort={ariaSort('saved')}>
              <button class="sort legend" onclick={() => toggleSort('saved')} aria-pressed={sortKey === 'saved'}>Saved{sortMark('saved')}</button>
            </th>
          </tr>
        </thead>
        <tbody>
          {#each filtered as movie (movie.path)}
            {@const decision = decisionOf(movie)}
            <tr
              class:row-selected={selectedPaths.has(movie.path)}
              class:in-work={runningMovie != null && movie.path === runningMovie}
            >
              <td class="chk">
                <input
                  type="checkbox"
                  checked={selectedPaths.has(movie.path)}
                  onchange={(e) => ontoggle(movie.path, e.currentTarget.checked)}
                  aria-label="Select {movie.name}"
                />
              </td>
              <td>
                <button class="name-btn" onclick={() => onopenmovie(movie)}>
                  {movie.name}
                  {#if movie.hdr}<span class="mono hdr">HDR</span>{/if}
                  {#if (movie.encode?.generation ?? 1) >= 2}<span class="mono gen-badge" title="Generation {movie.encode?.generation}: this file is itself the output of a previous encode">GEN {movie.encode?.generation}</span>{/if}
                </button>
              </td>
              <td class="dim">{movie.category}</td>
              <td class="mono num">{fmtGb(movie.size)}</td>
              <td>
                {#if runningMovie != null && movie.path === runningMovie}
                  <span class="chip progress"><span class="swatch"></span>IN WORK {runningPercent != null ? `${runningPercent}%` : ''}</span>
                {:else}
                  {#if decision.bare}
                    <!-- bare swatch: the state stays visible to sighted users via
                         the legend, but the cell must not be silent for SR users -->
                    <span
                      class="chip untouched bare-swatch"
                      role="img"
                      aria-label="Decision: untouched"
                      title="Untouched"
                    ></span>
                  {:else}
                    <span class="chip {decision.cls}"><span class="swatch"></span>{decision.label}</span>
                  {/if}
                {/if}
              </td>
              <td class="mono num">{movie.encode?.crf ?? '—'}</td>
              <td class="mono num measured">{movie.encode?.vmaf?.toFixed(1) ?? '—'}</td>
              <td class="mono num" class:ok={movie.encode?.saved_percent != null && movie.encode.saved_percent > 0} class:bad={movie.encode?.saved_percent != null && movie.encode.saved_percent <= 0}>
                {#if movie.encode}
                  {@const saved = fmtSavedHonest(movie.encode.saved_percent)}
                  <span title={saved.grew ? 'the re-encode is larger than the source' : ''}>{saved.text}</span>
                {:else}
                  —
                {/if}
              </td>
            </tr>
          {:else}
            <tr><td colspan="8" class="empty dim">no titles match — adjust the search or run a scan</td></tr>
          {/each}
        </tbody>
      </table>
    </div>
</div>

<style>
  .page { display: flex; flex-direction: column; gap: 12px; min-height: 0; }
  .page-head h2 { font-size: var(--fs-page); font-weight: 700; }
  .page-head .legend { margin-top: 2px; }
  .dim { color: var(--ink-dim); }
  .warn { color: var(--amber); }
  .bad { color: var(--red); }
  .ok { color: var(--teal-ink); }

  .toolbar {
    display: flex;
    gap: 8px;
    align-items: center;
    flex-wrap: wrap;
  }
  .search {
    flex: 0 1 260px;
    background: var(--panel);
    border: 1px solid var(--seam);
    border-radius: var(--radius);
    color: var(--ink);
    padding: 6px 12px;
  }
  .search:focus { border-color: var(--teal); outline: none; }
  .cat {
    background: var(--panel);
    border: 1px solid var(--seam);
    border-radius: var(--radius);
    color: var(--ink);
    padding: 6px 10px;
    max-width: 260px;
  }
  /* visually hidden, SR-audible (table caption) */
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
  .act.scan {
    background: var(--panel-raised);
    border: 1px solid var(--ink);
    border-radius: var(--radius);
    color: var(--ink);
    padding: 6px 14px;
    cursor: pointer;
    font-size: var(--fs-legend);
    letter-spacing: var(--track-legend);
    text-transform: uppercase;
  }
  .act.scan:hover:not(:disabled) { border-color: var(--teal); color: var(--teal-ink); }
  .act.scan:disabled { opacity: 0.5; cursor: default; }
  .spacer { flex: 1; }

  .bulk-bar {
    display: flex;
    align-items: center;
    gap: 12px;
    flex-wrap: wrap;
    border: 1px solid var(--teal);
    border-radius: var(--radius);
    background: var(--panel-active);
    padding: 8px 14px;
  }
  .bulk-bar.idle { border-color: var(--seam); background: var(--panel); }
  .sel-count { display: inline-flex; align-items: center; gap: 8px; font-weight: 600; }
  .sel-count input { accent-color: var(--teal); }
  .leader-note { color: var(--teal-ink); }
  .spacer { flex: 1; }

  .act {
    background: var(--panel-raised);
    border: 1px solid var(--seam);
    border-radius: var(--radius);
    color: var(--ink);
    padding: 5px 14px;
    cursor: pointer;
    font-size: var(--fs-legend);
    letter-spacing: var(--track-legend);
    text-transform: uppercase;
  }
  .act:hover:not(:disabled) { border-color: var(--teal); color: var(--teal-ink); }
  .act.primary { background: var(--teal); border-color: var(--teal); color: var(--ground); }
  .act.primary:hover:not(:disabled) { filter: brightness(1.08); color: var(--ground); }
  .act.quiet { border-color: var(--seam); color: var(--ink-dim); }
  .act:disabled { opacity: 0.4; cursor: default; }

  .empty { padding: 40px; text-align: center; }

  /* table */
  .table-wrap {
    flex: 1;
    min-height: 0;
    border: 1px solid var(--seam);
    border-radius: var(--radius);
    background: var(--panel);
    overflow: auto;
  }
  table { width: 100%; border-collapse: collapse; }
  thead tr { border-bottom: 1px solid var(--seam); }
  th {
    padding: 8px 12px;
    text-align: left;
    position: sticky;
    top: 0;
    background: var(--panel);
    z-index: 1;
  }
  th.num { text-align: right; }
  .sort {
    background: transparent;
    border: none;
    color: inherit;
    font: inherit;
    letter-spacing: inherit;
    text-transform: inherit;
    cursor: pointer;
    /* a11y pass: the button is the hit target - 24px minimum and readable
       interactive type even though the th label is legend-sized */
    font-size: var(--fs-data);
    min-height: 24px;
    padding: 2px 0;
    width: 100%;
    text-align: inherit;
  }
  .sort:hover { color: var(--ink); }
  td {
    padding: 7px 12px;
    border-bottom: 1px solid var(--row-line);
    white-space: nowrap;
  }
  td.num { text-align: right; }
  td.chk { text-align: center; width: 34px; }
  td.chk input { accent-color: var(--teal); }
  .name-btn {
    background: transparent;
    border: none;
    color: inherit;
    font: inherit;
    font-weight: 600;
    cursor: pointer;
    padding: 0;
    max-width: 340px;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .name-btn:hover { color: var(--teal-ink); text-decoration: underline; }
  tr.row-selected td { background: var(--hatch-select); }
  tr.in-work td { background: var(--hatch-progress); }
  .measured { color: var(--teal-ink); font-weight: 600; }
  .hdr {
    margin-left: 8px;
    color: var(--amber);
    font-size: 10px;
    border: 1px solid var(--seam);
    padding: 0 4px;
    border-radius: 2px;
  }
  /* generation badge: only on gen >= 2 files (a "GEN 1" on every encoded
     row would be noise - generation only means something when the file is
     an encode of an encode, same rule as the detail hero chip) */
  .gen-badge {
    margin-left: 8px;
    color: var(--teal-ink);
    font-size: 10px;
    border: 1px solid var(--teal);
    padding: 0 4px;
    border-radius: 2px;
  }
</style>

<script lang="ts">
  import { api, type Config } from '../api'
  import HintMark from './HintMark.svelte'

  /** SHEET: configuration — sources, output, quality target and the tool
   * settings in one first-class page (setup is configuration, not a
   * footer strip). */

  let {
    scanPaths,
    outputDir,
    targetVmaf = 96,
    workers = null,
    onpathsChange,
    onoutputChange,
    onvmafChange,
    onworkersChange,
  }: {
    scanPaths: string[]
    outputDir: string
    targetVmaf?: number
    workers?: number | null
    onpathsChange: (paths: string[]) => void
    onoutputChange: (dir: string) => void
    onvmafChange?: (vmaf: number) => void
    onworkersChange?: (workers: number | null) => void
  } = $props()

  let newPath = $state('')
  // local editing copy of the VMAF target (committed on change); the
  // effect syncs it when the app-level value changes
  let vmafDraft = $state(96)
  $effect(() => {
    vmafDraft = targetVmaf
  })
  let config = $state<Config | null>(null)
  // the config panels are READ-ONLY (issue #236 part 4, PRODUCT.md: the
  // dashboard is a status window; config.json is a file the operator edits
  // on disk and restarts) - only the load error is state here
  let loadError = $state<string | null>(null)

  $effect(() => {
    api
      .config()
      .then((c) => (config = c))
      .catch((e) => (loadError = e instanceof Error ? e.message : String(e)))
  })

  $effect(() => {
    vmafDraft = targetVmaf
  })

  function addPath() {
    const value = newPath.trim()
    if (!value) return
    if (!scanPaths.some((p) => p.toLowerCase() === value.toLowerCase())) {
      onpathsChange([...scanPaths, value])
    }
    newPath = ''
  }

  function removePath(path: string) {
    onpathsChange(scanPaths.filter((p) => p !== path))
  }

  // instant-commit fields acknowledge their own persistence — the tool
  // settings panel has 'saved ✓'; silence here read as identical silence
  // with the opposite truth (round-5 P1: the two persistence models were
  // indistinguishable in the UI)
  let flashed = $state<Record<string, boolean>>({})
  function flash(key: string) {
    flashed = { ...flashed, [key]: true }
    setTimeout(() => {
      flashed = { ...flashed, [key]: false }
    }, 1400)
  }
  function commitPaths(next: string[]) {
    onpathsChange(next)
    flash('paths')
  }


  const dedupedExtensions = $derived([...new Set(config?.file?.extensions ?? [])])
</script>

<div class="page page-in">
  <header class="page-head">
    <h2>Config</h2>
    <p class="legend">SOURCES, OUTPUT AND TOOL SETTINGS</p>
  </header>

  <!-- ══ sources ══ -->
  <section class="panel">
    <div class="panel-head">
      <span class="legend">SCAN SOURCES — WHERE THE LIBRARY LIVES</span>
      <span class="mono dim saved-note" role="status">{flashed.paths ? 'saved ✓' : 'applies immediately'}</span>
    </div>
    <div class="pad">
      <div class="path-list">
        {#each scanPaths as path (path)}
          <span class="path-chip mono">
            {path}
            <button class="x" onclick={() => commitPaths(scanPaths.filter((p) => p !== path))} aria-label="Remove {path}">✕</button>
          </span>
        {/each}
        <input
          class="mono new-path"
          bind:value={newPath}
          placeholder="add folder + Enter"
          aria-label="Add movie folder"
          onkeydown={(e) => e.key === 'Enter' && addPath()}
          onblur={addPath}
        />
      </div>
      <p class="mono dim note">sources are never modified in place — encodes go to the output directory</p>
    </div>
  </section>

  <!-- ══ output ══ -->
  <section class="panel">
    <div class="panel-head">
      <span class="legend">OUTPUT — WHERE ENCODES ARE WRITTEN</span>
      <span class="mono dim saved-note" role="status">{flashed.output ? 'saved ✓' : 'applies immediately'}</span>
    </div>
    <div class="pad row">
      <input
        class="mono out"
        bind:value={outputDir}
        onchange={() => {
          onoutputChange(outputDir)
          flash('output')
        }}
        placeholder="output folder"
        aria-label="Output folder path (encode)"
      />
    </div>
  </section>

  <!-- ══ quality target ══ -->
  <section class="panel">
    <div class="panel-head">
      <span class="legend">QUALITY TARGET — THE ENCODER'S FLOOR</span>
      <span class="mono dim saved-note" role="status">{flashed.vmaf ? 'saved ✓' : 'applies immediately'}</span>
    </div>
    <div class="pad row vmaf-row">
      <label class="vmaf-label">
        <span class="dim">VMAF target</span>
        <input
          class="mono vmaf-input"
          type="number"
          min="70"
          max="100"
          step="0.5"
          bind:value={vmafDraft}
          onchange={() => {
            onvmafChange?.(Math.min(100, Math.max(70, Number(vmafDraft) || 96)))
            flash('vmaf')
          }}
          aria-label="VMAF quality target"
        />
      </label>
      <HintMark text="VMAF is the measured visual similarity to the source (0-100). 96 is the project's decided 'visually transparent' floor: encodes below it are visibly worse. Higher target = larger files. The engine searches the largest CRF that still reaches this target on real samples." />
    </div>
  </section>

  <!-- ══ encode parallelism ══ -->
  <section class="panel">
    <div class="panel-head">
      <span class="legend">ENCODE PARALLELISM — FIXED MODE ONLY</span>
      <span class="mono dim saved-note" role="status">{flashed.workers ? 'saved ✓' : 'applies immediately'}</span>
    </div>
    <div class="pad row vmaf-row">
      <label class="vmaf-label">
        <span class="dim">workers</span>
        <input
          class="mono vmaf-input"
          type="number"
          min="1"
          max="16"
          step="1"
          value={workers ?? ''}
          placeholder="auto"
          onchange={(e) => {
            const raw = (e.currentTarget as HTMLInputElement).value
            onworkersChange?.(raw === '' ? null : Math.min(16, Math.max(1, Number(raw))))
            flash('workers')
          }}
          aria-label="Concurrent encodes (fixed mode)"
        />
      </label>
      <HintMark text="How many movies encode at once in fixed mode (quality mode always encodes one movie at a time — a single x265 encode already uses all cores). Empty = automatic." />
    </div>
  </section>

  <!-- ══ resource politeness (config.json, read-only) ══ -->
  <section class="panel">
    <div class="panel-head">
      <span class="legend">RESOURCES — BEHAVIOR ON A SHARED MACHINE</span>
      <span class="mono dim">read-only</span>
    </div>
    {#if !config}
      <div class="dim pad">loading…</div>
    {:else}
      <div class="pad groups">
        <div class="option-row">
          <span>
            <strong>IDLE-PRIORITY ENCODES</strong>
            <span class="mono dim block">encodes and quality probes run below normal priority — they yield instantly to everything else on the machine</span>
          </span>
          <span class="mono">{config.resources?.idle_priority ?? true ? 'on' : 'off'}</span>
        </div>
        <div class="field">
          <span class="dim">max threads per encode (0 = all cores)</span>
          <span class="mono">{config.handbrake?.max_threads ?? 0}</span>
        </div>
        <p class="mono dim note">e.g. 8 on a 16-core mini PC leaves half the machine for a media server</p>
      </div>
    {/if}
  </section>

  <!-- ══ tool settings ══ -->
  <section class="panel">
    <div class="panel-head">
      <span class="legend">TOOL SETTINGS — FROM CONFIG.JSON</span>
      <span class="mono dim">read-only</span>
    </div>
    {#if !config}
      <div class="dim pad">{loadError ? loadError : 'loading…'}</div>
    {:else}
      <div class="pad groups">
        <div class="group">
          <div class="legend">MOVIE EXTENSIONS</div>
          <div class="ext-list">
            {#each dedupedExtensions as ext (ext)}
              <span class="ext mono">{ext}</span>
            {/each}
          </div>
        </div>

        <div class="group">
          <div class="legend">HANDBRAKE</div>
          <div class="field">
            <span class="dim">encoder</span>
            <span class="mono">{config.handbrake?.encoder}</span>
          </div>
          <div class="field">
            <span class="dim">encoder preset</span>
            <span class="mono">{config.handbrake?.encoder_preset}</span>
          </div>
        </div>

        <div class="group">
          <div class="legend">ELIGIBILITY — WHICH SOURCES ARE EVER RE-ENCODED</div>
          <div class="field">
            <span class="dim">min resolution</span>
            <span class="mono">{config.eligibility?.min_source_height ?? 1080}p floor</span>
          </div>
          <div class="field">
            <span class="dim">candidate codecs</span>
            <span class="mono">{(config.eligibility?.candidate_codecs ?? []).join(', ')}</span>
          </div>
        </div>

        <div class="group">
          <div class="legend">QUALITY — THE PROBE SETTINGS</div>
          <div class="field">
            <span class="dim">sample every (s)</span>
            <span class="mono">{config.quality?.sample_every}</span>
          </div>
          <div class="field">
            <span class="dim">sample count</span>
            <span class="mono">{config.quality?.sample_count}</span>
          </div>
          <div class="field">
            <span class="dim">sample seconds</span>
            <span class="mono">{config.quality?.sample_seconds}</span>
          </div>
        </div>
      </div>
      <div class="foot">
        <span class="mono dim">config.json is a file on disk — edit it there and restart; the dashboard is read-only by design</span>
      </div>
    {/if}
  </section>
</div>

<style>
  .page {
    display: flex;
    flex-direction: column;
    gap: 14px;
    max-width: 760px;
    /* composed, not hugged to the rail: ~640px dead ground at 1440 read
       as unfinished (round-5 minor) */
    margin-inline: auto;
  }
  .page-head h2 { font-size: var(--fs-page); font-weight: 700; }
  .page-head .legend { margin-top: 2px; }
  .dim { color: var(--ink-dim); }

  .panel {
    border: 1px solid var(--seam);
    border-radius: var(--radius);
    background: var(--panel);
  }
  .panel-head {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 9px 14px;
    border-bottom: 1px solid var(--row-line);
  }
  .saved-note { font-size: 10px; letter-spacing: 0.5px; white-space: nowrap; }
  .pad { padding: 14px; }
  .pad.row { padding: 14px; }
  .groups { display: flex; flex-direction: column; gap: 14px; }

  .option-row {
    display: flex;
    gap: 10px;
    align-items: flex-start;
    cursor: pointer;
  }
  .option-row .block { display: block; margin-top: 2px; font-size: 11px; }

  .path-list { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; }
  .path-chip {
    background: var(--panel-raised);
    border: 1px solid var(--seam);
    border-radius: var(--radius);
    padding: 4px 10px;
    display: inline-flex;
    align-items: center;
    gap: 8px;
    max-width: 340px;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .x {
    background: transparent;
    border: none;
    color: var(--ink-faint);
    cursor: pointer;
    font-size: 11px;
    padding: 0;
  }
  .x:hover { color: var(--red); }
  .new-path {
    background: transparent;
    border: 1px dashed var(--seam);
    border-radius: var(--radius);
    color: var(--ink);
    padding: 4px 10px;
    min-width: 220px;
  }
  .note { margin-top: 10px; font-size: 11px; }

  .out {
    width: 100%;
    background: var(--panel-raised);
    border: 1px solid var(--seam);
    border-radius: var(--radius);
    color: var(--ink);
    padding: 6px 12px;
  }

  .vmaf-row { display: flex; align-items: center; gap: 10px; }
  .vmaf-label { display: flex; align-items: center; gap: 10px; }
  .vmaf-input {
    background: var(--panel-raised);
    border: 1px solid var(--seam);
    border-radius: var(--radius);
    color: var(--ink);
    width: 80px;
    padding: 6px 10px;
  }

  .group { padding: 12px 14px; border-top: 1px solid var(--row-line); }
  .group:first-child { border-top: none; padding-top: 4px; }
  .group .legend { display: block; margin-bottom: 10px; }
  .ext-list { display: flex; flex-wrap: wrap; gap: 6px; align-items: center; }
  .ext {
    background: var(--panel-raised);
    border: 1px solid var(--seam);
    border-radius: var(--radius);
    padding: 2px 8px;
    display: inline-flex;
    align-items: center;
    gap: 6px;
  }
  .field { display: flex; align-items: center; gap: 10px; margin-bottom: 8px; }
  .field:last-child { margin-bottom: 0; }
  /* 150px slot: at 120px "max threads per encode (0 = all cores)" wrapped
     to three cramped lines (round-6) */
  .field span { width: 150px; flex-shrink: 0; }
  .foot {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 0 14px 14px;
  }

</style>

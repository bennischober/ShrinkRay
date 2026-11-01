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
  }: {
    scanPaths: string[]
    outputDir: string
    targetVmaf?: number
  } = $props()

  // the config panels are READ-ONLY (issue #236 part 4, PRODUCT.md: the
  // dashboard is a status window; config.json is a file the operator edits
  // on disk or via shrinkray setup, never through this page — issue #331:
  // the editable panels that survived #236 wrote localStorage and changed
  // nothing the app does) - only the load error is state here
  let config = $state<Config | null>(null)
  let loadError = $state<string | null>(null)

  $effect(() => {
    api
      .config()
      .then((c) => (config = c))
      .catch((e) => (loadError = e instanceof Error ? e.message : String(e)))
  })

  // the REAL VMAF floor (quality.target_vmaf) beats the app-level value:
  // the app-level one was a localStorage draft that never reached the engine
  const configTargetVmaf = $derived(config?.quality?.target_vmaf ?? null)

  const dedupedExtensions = $derived([...new Set(config?.file?.extensions ?? [])])
</script>

<div class="page page-in">
  <header class="page-head">
    <h2>Config</h2>
    <p class="legend">SOURCES, OUTPUT AND TOOL SETTINGS</p>
  </header>

  <!-- ══ sources (read-only, issue #331): the watched roots are the
       resident app's argv/mount — editing them here wrote localStorage and
       changed nothing the app does; the terminal and the container's
       mounts configure this -->
  <section class="panel">
    <div class="panel-head">
      <span class="legend">SCAN SOURCES — WHERE THE LIBRARY LIVES</span>
      <span class="mono dim">read-only</span>
    </div>
    <div class="pad">
      <div class="path-list">
        {#each scanPaths as path (path)}
          <span class="path-chip mono">{path}</span>
        {:else}
          <span class="mono dim">no scan source recorded yet</span>
        {/each}
      </div>
      <p class="mono dim note">the resident mode encodes IN PLACE: a verified output takes the source's path and the original is kept beside it (renamed .shrinkray-original) — nothing is deleted</p>
    </div>
  </section>

  <!-- ══ output (read-only, issue #331): the resident mode writes into the
       library (in place); a separate output dir is a Mode A flag, not a
       setting this page can change -->
  <section class="panel">
    <div class="panel-head">
      <span class="legend">OUTPUT — WHERE ENCODES ARE WRITTEN</span>
      <span class="mono dim">read-only</span>
    </div>
    <div class="pad row">
      <span class="mono src">{outputDir || 'the library itself (in place)'}</span>
    </div>
  </section>

  <!-- ══ quality target (read-only, issue #331): the VMAF floor is
       config.json's quality.target_vmaf — shown from the live config, not
       an editable draft that changed nothing -->
  <section class="panel">
    <div class="panel-head">
      <span class="legend">QUALITY TARGET — THE ENCODER'S FLOOR</span>
      <span class="mono dim">read-only</span>
    </div>
    <div class="pad row vmaf-row">
      <div class="vmaf-label">
        <span class="dim">VMAF target</span>
        <span class="mono vmaf-value">{configTargetVmaf ?? targetVmaf ?? 96}</span>
      </div>
      <HintMark text="VMAF is the measured visual similarity to the source (0-100). 96 is the project's decided 'visually transparent' floor: encodes below it are visibly worse. Higher target = larger files. The engine searches the largest CRF that still reaches this target on real samples. Change it in config.json (quality.target_vmaf) or via shrinkray setup." />
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
  .pad { padding: 14px; }
  .pad.row { padding: 14px; }
  .groups { display: flex; flex-direction: column; gap: 14px; }

  .option-row {
    display: flex;
    gap: 10px;
    align-items: flex-start;
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
  .note { margin-top: 10px; font-size: 11px; }


  .vmaf-row { display: flex; align-items: center; gap: 10px; }
  .vmaf-label { display: flex; align-items: center; gap: 10px; }

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

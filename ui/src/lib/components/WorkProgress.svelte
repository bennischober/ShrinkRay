<script lang="ts">
  import { crf, fps, percent, vmaf } from '../format'
  import {
    STAGE_COUNT,
    STAGE_LABELS,
    STAGE_STEPS,
    chosenLine,
    progressEta,
    reusedLabel,
    workSummary,
    type FileProgressView,
  } from '../progress.svelte'
  import ProgressBar from './ProgressBar.svelte'

  /** The ONE running-work surface every page renders (issue #631): the
   * canonical summary line is identical everywhere, and the expanded view adds
   * the stepper, the CRF-search probe table and the encode bar. */
  let {
    progress,
    stale = false,
    detail = false,
    targetVmaf = 96,
    sourceBytes = null,
  }: {
    progress: FileProgressView | null
    stale?: boolean
    /** expanded view: stepper + the current step's own section */
    detail?: boolean
    targetVmaf?: number
    /** the running movie's source size, for the chosen line's saving */
    sourceBytes?: number | null
  } = $props()

  let frozenNow = $state<number | null>(null)
  $effect(() => {
    if (!stale) frozenNow = Date.now()
  })
  const now = $derived(stale ? frozenNow ?? Date.now() : Date.now())
  const summary = $derived(workSummary(progress, now, stale))
  const eta = $derived(progressEta(progress))
  const stage = $derived(progress?.stage ?? null)
  const step = $derived(progress?.step ?? null)
  const steps = $derived(progress?.steps ?? STAGE_COUNT)
  const probeRows = $derived(
    (progress?.probes ?? []).filter((p) => p.crf != null && p.vmaf != null),
  )
  const chosen = $derived(progress?.chosen ?? null)
  const reused = $derived(reusedLabel(progress))
</script>

<div class="work" data-work-progress data-stale={stale || undefined}>
  <div class="summary mono" data-work-summary>{summary}</div>

  {#if detail && progress && stage && stage !== 'scan' && stage !== 'paused'}
    <ol class="stepper" aria-label="Pipeline steps">
      {#each Object.entries(STAGE_STEPS) as [name, position] (name)}
        {@const state = step == null ? 'todo' : position < step ? 'done' : position === step ? 'current' : 'todo'}
        <li class="step {state}" aria-current={state === 'current' ? 'step' : undefined}>
          <span class="mark" aria-hidden="true">{state === 'done' ? '✓' : state === 'current' ? '●' : '○'}</span>
          <span>{STAGE_LABELS[name] ?? name}</span>
        </li>
      {/each}
    </ol>
  {/if}

  {#if detail && (stage === 'crf-search' || (stage === 'encode' && probeRows.length > 0))}
    <section class="section">
      <p class="explain">
        Testing quality at different compression levels to find the smallest file
        that still looks like the original (target VMAF {targetVmaf}).
      </p>
      {#if probeRows.length > 0}
        <table class="probes">
          <thead>
            <tr><th>CRF</th><th>VMAF</th><th>Verdict</th></tr>
          </thead>
          <tbody>
            {#each probeRows as probe (probe.index)}
              <tr>
                <td class="mono">CRF {crf(probe.crf)}</td>
                <td class="mono">VMAF {vmaf(probe.vmaf, targetVmaf)}</td>
                <td class="mono" class:below={!probe.passed} class:pass={probe.passed}>
                  {probe.passed ? '✓' : '✗ below target'}
                </td>
              </tr>
            {/each}
          </tbody>
        </table>
      {/if}
      {#if reused}
        <p class="reused mono" data-reused>{reused}</p>
      {/if}
      {#if chosen}
        <p class="chosen mono" data-chosen>{chosenLine(progress, targetVmaf, sourceBytes)}</p>
      {/if}
    </section>
  {/if}

  {#if detail && stage === 'encode'}
    <section class="section">
      <ProgressBar
        percent={progress?.encode?.percent ?? null}
        stage="Encode"
        eta={eta}
        {stale}
      />
      <p class="facts mono">
        {#if progress?.encode?.fps != null}{fps(progress.encode.fps)}{/if}
        {#if progress?.encode?.avg_fps != null}
          <span class="dim"> · avg {fps(progress.encode.avg_fps)}</span>
        {/if}
      </p>
    </section>
  {/if}

  {#if detail && stage === 'samples' && progress?.sample}
    <p class="facts mono">
      extracting sample {progress.sample.index} of {progress.sample.count}
    </p>
  {/if}

  {#if detail && (stage === 'decision' || stage === 'bars' || stage === 'verify' || stage === 'swap') && progress?.detail}
    <p class="facts mono">{progress.detail}</p>
  {/if}

  {#if detail && stage === 'paused' && progress?.detail}
    <p class="facts mono">{progress.detail}</p>
  {/if}

  {#if stale}
    <p class="stale mono" role="status">live progress is stale — the numbers are frozen</p>
  {/if}
</div>

<style>
  .work { display: flex; flex-direction: column; gap: 8px; min-width: 0; }
  .summary {
    font-size: var(--fs-data);
    color: var(--ink);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .dim { color: var(--ink-dim); }
  .stepper {
    display: flex;
    flex-wrap: wrap;
    gap: 4px 10px;
    list-style: none;
    padding: 0;
    margin: 0;
    font-size: var(--fs-data);
  }
  .step { display: flex; gap: 4px; align-items: baseline; color: var(--ink-faint); }
  .step.done { color: var(--ink-dim); }
  .step.current { color: var(--amber); }
  .mark { font-family: var(--font-mono); }
  .section { display: flex; flex-direction: column; gap: 6px; }
  .explain { margin: 0; color: var(--ink-dim); font-size: var(--fs-data); }
  .probes {
    border-collapse: collapse;
    font-size: var(--fs-data);
    align-self: flex-start;
  }
  .probes th, .probes td {
    text-align: left;
    padding: 2px 10px 2px 0;
    border-bottom: 1px solid var(--row-line);
  }
  .probes th { color: var(--ink-faint); font-weight: 400; }
  .below { color: var(--red); }
  .pass { color: var(--teal-ink); }
  .reused { margin: 0; color: var(--amber); }
  .chosen { margin: 0; color: var(--ink); }
  .facts { margin: 0; color: var(--ink-dim); font-size: var(--fs-data); }
  .stale { margin: 0; color: var(--ink-faint); font-size: var(--fs-data); }
</style>

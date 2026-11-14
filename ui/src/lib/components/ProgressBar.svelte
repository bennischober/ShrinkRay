<script lang="ts">
  import { eta as fmtEta, percent as fmtPercent } from '../format'

  let {
    percent,
    stage,
    eta,
    stale = false,
  }: { percent: number | null; stage?: string; eta?: number | null; stale?: boolean } = $props()
</script>

<div class="progress" class:stale>
  <div class="row">
    <span class="stage">{stage ?? ''}</span>
    <span class="value">{fmtPercent(percent)}</span>
  </div>
  <div
    class="track"
    role="progressbar"
    aria-valuemin="0"
    aria-valuemax="100"
    aria-valuenow={percent ?? undefined}
    aria-label={stage ?? 'progress'}
  >
    <div class="fill" style:width="{percent ?? 0}%"></div>
  </div>
  {#if eta != null && fmtEta(eta)}<span class="eta">{fmtEta(eta)}</span>{/if}
</div>

<style>
  .progress { display: flex; flex-direction: column; gap: 4px; min-width: 0; }
  .row { display: flex; justify-content: space-between; gap: 8px; font-size: var(--fs-data); }
  .stage { color: var(--ink-dim); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .value { font-family: var(--font-mono); color: var(--ink); }
  .track { height: 3px; background: var(--seam); border-radius: 2px; overflow: hidden; }
  .fill { height: 100%; background: var(--amber); transition: width 400ms ease; }
  .eta { font-size: var(--fs-data); color: var(--ink-dim); }
  .stale .fill { background: var(--ink-faint); }
  .stale .stage::after { content: ' (stale)'; color: var(--ink-faint); }
</style>

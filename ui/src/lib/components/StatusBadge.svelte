<script lang="ts">
  import type { Tone } from './tone'

  let {
    label,
    tone = 'idle',
    shape = 'dot',
  }: { label: string; tone?: Tone; shape?: 'dot' | 'bar' | 'hatch' } = $props()
</script>

<span class="badge {tone}" data-shape={shape}>
  <span class="mark" aria-hidden="true"></span>
  <span class="label">{label}</span>
</span>

<style>
  .badge {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    font-family: var(--font-ui);
    font-size: var(--fs-data);
    color: var(--ink);
    white-space: nowrap;
  }
  .mark {
    width: 8px;
    height: 8px;
    border-radius: 50%;
    border: 1px solid currentColor;
    flex: none;
  }
  [data-shape='bar'] .mark { border-radius: 1px; width: 10px; height: 3px; }
  [data-shape='hatch'] .mark { border-radius: 1px; background: var(--hatch-work); }
  .ok { color: var(--ok); }
  .work { color: var(--amber); }
  .fail { color: var(--red); }
  .idle { color: var(--ink-dim); }
  .dim { color: var(--ink-faint); }
</style>

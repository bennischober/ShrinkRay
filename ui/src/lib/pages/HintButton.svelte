<script lang="ts">
  /** The hint pattern: a caps label whose meaning lives in a tooltip on
   * hover AND in a visible one-line explanation on focus (keyboard users
   * get the same text via focus + aria-describedby). Used on action
   * buttons where the verb alone under-explains. */

  let {
    label,
    hint,
    onclick,
    disabled = false,
    variant = 'default',
  }: {
    label: string
    hint: string
    onclick?: () => void
    disabled?: boolean
    variant?: 'default' | 'primary'
  } = $props()

  // unique id for aria-describedby (one hint per button instance)
  const hintId = `hint-${Math.random().toString(36).slice(2, 9)}`

  // placement awareness: a hint anchored left on a toolbar-end button
  // overflows the viewport — measure at hover/focus time and anchor right
  // instead
  let wrapEl: HTMLElement | null = $state(null)
  let alignEnd = $state(false)
  function measure() {
    if (!wrapEl) return
    const rect = wrapEl.getBoundingClientRect()
    alignEnd = rect.right + 330 > window.innerWidth
  }
</script>

<span
  class="wrap"
  bind:this={wrapEl}
  onmouseenter={measure}
  onfocusin={measure}
  role="presentation"
>
  <button
    type="button"
    {onclick}
    {disabled}
    class:primary={variant === 'primary'}
    aria-describedby={hintId}
    title={hint}
  >
    {label}
  </button>
  <span class="hint" role="note" id={hintId} class:end={alignEnd}>{hint}</span>
</span>

<style>
  .wrap {
    position: relative;
    display: inline-flex;
  }
  button {
    background: var(--panel-raised);
    border: 1px solid var(--ink);
    border-radius: var(--radius);
    color: var(--ink);
    padding: 5px 14px;
    cursor: pointer;
    font-size: var(--fs-legend);
    letter-spacing: var(--track-legend);
    text-transform: uppercase;
  }
  button:hover:not(:disabled) { border-color: var(--teal); color: var(--teal-ink); }
  button.primary {
    background: var(--teal);
    border-color: var(--teal);
    color: var(--ground);
  }
  button.primary:hover:not(:disabled) { filter: brightness(1.08); color: var(--ground); }
  button:disabled { opacity: 0.4; cursor: default; }
  /* the visible hint: shows on hover and on keyboard focus-within */
  .hint {
    position: absolute;
    top: calc(100% + 6px);
    left: 0;
    min-width: 240px;
    max-width: 320px;
    background: var(--panel-raised);
    border: 1px solid var(--seam);
    border-radius: var(--radius);
    color: var(--ink-dim);
    padding: 6px 10px;
    font-size: 10.5px;
    letter-spacing: 0.2px;
    z-index: 70;
    opacity: 0;
    pointer-events: none;
    transition: opacity 120ms ease;
    white-space: normal;
    text-transform: none;
  }
  /* toolbar-end buttons anchor the box to their right edge instead */
  .hint.end {
    left: auto;
    right: 0;
  }
  .wrap:hover .hint,
  .wrap:focus-within .hint {
    opacity: 1;
  }
</style>

<script lang="ts">
  let {
    title,
    children,
  }: { title?: string; children?: import('svelte').Snippet } = $props()
</script>

<section class="panel">
  {#if title}<h2>{title}</h2>{/if}
  {@render children?.()}
</section>

<style>
  .panel {
    background: var(--panel);
    border: 1px solid var(--seam);
    border-radius: var(--radius);
    padding: 14px 16px;
  }
  h2 {
    margin: 0 0 10px;
    font-family: var(--font-ui);
    font-size: var(--fs-title);
    font-weight: 600;
    color: var(--ink);
  }
</style>

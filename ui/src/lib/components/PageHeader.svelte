<script lang="ts">
  let {
    title,
    description,
    children,
  }: {
    title: string
    description?: string
    children?: import('svelte').Snippet
  } = $props()
</script>

<header class="page-header">
  <div class="text">
    <h1>{title}</h1>
    {#if description}<p class="desc">{description}</p>{/if}
  </div>
  {#if children}<div class="aside">{@render children()}</div>{/if}
</header>

<style>
  .page-header {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: 16px;
    margin: 0 0 18px;
  }
  .text { min-width: 0; }
  h1 {
    margin: 0;
    font-family: var(--font-ui);
    font-size: var(--fs-page);
    font-weight: 600;
    letter-spacing: 0;
    color: var(--ink);
  }
  .desc {
    margin: 4px 0 0;
    font-family: var(--font-ui);
    font-size: var(--fs-body);
    color: var(--ink-dim);
  }
  .aside { flex: none; display: flex; gap: 8px; align-items: center; }
</style>

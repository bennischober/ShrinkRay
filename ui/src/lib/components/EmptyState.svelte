<script lang="ts">
  let { title, hint }: { title: string; hint?: string } = $props()
</script>

<div class="empty">
  <p class="title">{title}</p>
  {#if hint}<p class="hint">{hint}</p>{/if}
</div>

<style>
  .empty {
    border: 1px dashed var(--seam);
    border-radius: var(--radius);
    padding: 18px;
    text-align: center;
  }
  .title { margin: 0; font-family: var(--font-ui); font-size: var(--fs-body); color: var(--ink-dim); }
  .hint { margin: 4px 0 0; font-family: var(--font-ui); font-size: var(--fs-data); color: var(--ink-faint); }
</style>

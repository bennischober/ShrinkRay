<script lang="ts">
  let { path, clamp = 1 }: { path: string; clamp?: 1 | 2 } = $props()
  const name = $derived(path.split('/').pop() ?? path)
</script>

<!-- The keyboard reveal (issue #655): the full path lives in title=, which
     is mouse-only unless the host is focusable — a deliberately
     non-interactive focusable text host, outlined by the global
     :focus-visible rule. --><!-- svelte-ignore a11y_no_noninteractive_tabindex -->
<span class="file-name" title={path} data-clamp={clamp} tabindex="0">{name}</span>

<style>
  .file-name {
    display: block;
    max-width: 100%;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    font-family: var(--font-mono);
    font-size: var(--fs-data);
    color: var(--ink);
    overflow-wrap: normal;
  }
  .file-name[data-clamp='2'] {
    display: -webkit-box;
    line-clamp: 2;
    -webkit-line-clamp: 2;
    -webkit-box-orient: vertical;
    white-space: normal;
    overflow-wrap: anywhere;
  }
  /* a phone card is ~344px wide: one nowrap line cannot fit a release-style name
     and the cell's own rules cannot beat this component's scoped rule, so the wrap
     has to live here (issue #636 review) */
  @media (max-width: 768px) {
    .file-name {
      display: -webkit-box;
      line-clamp: 2;
      -webkit-line-clamp: 2;
      -webkit-box-orient: vertical;
      white-space: normal;
      overflow-wrap: anywhere;
    }
  }
</style>

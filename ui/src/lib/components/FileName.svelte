<script lang="ts">
  let { path, clamp = 1 }: { path: string; clamp?: 1 | 2 } = $props()
  const name = $derived(path.split('/').pop() ?? path)
</script>

<span class="file-name" title={path} data-clamp={clamp}>{name}</span>

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
</style>

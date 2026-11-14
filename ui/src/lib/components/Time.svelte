<script lang="ts">
  import { absolute, relative } from '../format'

  let { iso, now }: { iso: string | null | undefined; now?: Date } = $props()
  const rel = $derived(relative(iso, now))
  const abs = $derived(absolute(iso, now))
</script>

<time datetime={iso ?? undefined} title={abs}>{rel}</time>

<style>
  time { font-family: var(--font-mono); font-size: var(--fs-data); color: var(--ink-dim); }
</style>

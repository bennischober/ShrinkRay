<script lang="ts">
  import { absolute, relative } from '../format'

  let { iso, now }: { iso: string | null | undefined; now?: Date } = $props()
  const rel = $derived(relative(iso, now))
  const abs = $derived(absolute(iso, now))
</script>

<!-- The keyboard reveal (issue #655): the exact timestamp lives in title=,
     which is mouse-only unless the host is focusable — a deliberately
     non-interactive focusable text host, outlined by the global
     :focus-visible rule. --><!-- svelte-ignore a11y_no_noninteractive_tabindex -->
<time datetime={iso ?? undefined} title={abs} tabindex="0">{rel}</time>

<style>
  time { font-family: var(--font-mono); font-size: var(--fs-data); color: var(--ink-dim); }
</style>

<script lang="ts">
  import type { Job, Movie } from '../api'
  import { duration, queuePaceSeconds, queuedFileCount } from '../format'

  /** The Overview's and Jobs page's queue outlook (issue #631): how many
   * files wait, and a ROUGH time only from measured seconds-per-GB of the
   * last real encodes. No history means no time, never a fabricated one. */
  let { jobs, movies }: { jobs: Job[]; movies: Movie[] } = $props()
  const files = $derived(queuedFileCount(jobs))
  const seconds = $derived(queuePaceSeconds(files, movies))
</script>

<div class="outlook mono" data-queue-outlook>
  {#if files > 0}
    <span>{files} file{files === 1 ? '' : 's'} waiting</span>
    {#if seconds != null}
      <span class="dim"> · roughly {duration(seconds)} at the recent pace</span>
    {/if}
  {:else}
    <span class="dim">nothing waiting</span>
  {/if}
</div>

<style>
  .outlook { font-size: var(--fs-data); color: var(--ink); }
  .dim { color: var(--ink-dim); }
</style>

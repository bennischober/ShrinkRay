<script lang="ts">
  import { fmtGb, type RetainedFile } from '../api'
  import HintButton from './HintButton.svelte'

  let { files, onrecover }: { files: RetainedFile[]; onrecover?: () => void } = $props()

  let total = $derived(files.reduce((sum, f) => sum + f.size_before, 0))
  let reclaimable = $derived(
    files.filter((f) => f.present).reduce((sum, f) => sum + f.size_before, 0),
  )
</script>

<section class="retained" aria-labelledby="retained-heading">
  <header class="retained-head">
    <h1 id="retained-heading">Retained originals</h1>
    <HintButton
      label="WHY KEEP THEM?"
      hint="ShrinkRay never deletes a source file. When an encode replaces one in place, the original is renamed aside and kept — it is listed here so you can reclaim the space yourself, in your own file manager, once you are satisfied with the result."
    />
    {#if onrecover}
      <button class="ghost mono" onclick={onrecover}>refresh</button>
    {/if}
  </header>

  {#if files.length === 0}
    <p class="empty">
      No retained originals. Nothing has been swapped in place yet — encodes written to an
      output directory never touch the source.
    </p>
  {:else}
    <p class="summary mono">
      {files.length} retained original{files.length === 1 ? '' : 's'} · {fmtGb(total)} swapped out ·
      {fmtGb(reclaimable)} reclaimable
    </p>
    <table class="retained-table mono">
      <thead>
        <tr>
          <th scope="col">retained original</th>
          <th scope="col">replaced</th>
          <th scope="col">size before</th>
          <th scope="col">size after</th>
          <th scope="col">when</th>
        </tr>
      </thead>
      <tbody>
        {#each files as file (file.retained_path)}
          <tr class:gone={!file.present}>
            <td class="path" title={file.retained_path}>
              {file.retained_path}
              {#if !file.present}<span class="tag">already removed</span>{/if}
            </td>
            <td class="path" title={file.source_path}>{file.source_path}</td>
            <td>{fmtGb(file.size_before)}</td>
            <td>{fmtGb(file.size_after)}</td>
            <td>{file.created_at}</td>
          </tr>
        {/each}
      </tbody>
    </table>
    <p class="footnote">
      Delete these in your file manager when you are satisfied. ShrinkRay has no delete button: the
      library is yours.
    </p>
  {/if}
</section>

<style>
  .retained {
    padding: 1.5rem;
  }
  .retained-head {
    display: flex;
    align-items: baseline;
    gap: 0.75rem;
    margin-bottom: 1rem;
  }
  .retained-head h1 {
    font-size: 1.1rem;
    margin: 0;
  }
  .ghost {
    margin-left: auto;
    background: transparent;
    border: 1px solid currentColor;
    border-radius: 3px;
    padding: 0.2rem 0.6rem;
    font-size: 0.75rem;
    cursor: pointer;
    opacity: 0.75;
  }
  .ghost:hover {
    opacity: 1;
  }
  .empty,
  .summary,
  .footnote {
    opacity: 0.75;
    font-size: 0.8rem;
  }
  .retained-table {
    width: 100%;
    border-collapse: collapse;
    font-size: 0.78rem;
  }
  .retained-table th {
    text-align: left;
    opacity: 0.6;
    font-weight: 500;
    padding: 0.3rem 0.5rem;
    border-bottom: 1px solid currentColor;
  }
  .retained-table td {
    padding: 0.3rem 0.5rem;
    border-bottom: 1px solid rgba(128, 128, 128, 0.2);
  }
  .path {
    max-width: 28rem;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .gone td {
    opacity: 0.45;
  }
  .tag {
    margin-left: 0.4rem;
    font-size: 0.7rem;
    opacity: 0.8;
  }
</style>

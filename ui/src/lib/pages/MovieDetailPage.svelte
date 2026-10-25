<script lang="ts">
  import {
    api,
    fmtGb,
    fmtSeconds,
    fmtWhen,
    humanizeError,
    jobPaths,
    failedOutcomeNote,
    movieOutcome,
    type Movie,
    type Job,
    type EncodeRecord,
    type MoviePlan,
  } from '../api'

  /** The provenance page: everything recorded about one title — source
   * facts, the latest encode with every schema-v5 metric, the COMPLETE
   * encode history (deltas between runs) with a VMAF-vs-target gauge,
   * the job audit trail, and the actions. Deep-linkable via #/movie. */

  let {
    movie,
    targetVmaf,
    allJobs,
    revealAvailable = false,
    onclose,
    onjobs,
    backTo = 'library',
  }: {
    movie: Movie
    targetVmaf: number
    /** The snapshot's job list — filtering it locally beats refetching
     * /api/jobs per detail-open (zero extra round-trips). */
    allJobs: Job[]
    /** the server reports whether this host can open a file manager (issue #239) */
    revealAvailable?: boolean
    onclose: () => void
    /** Navigate to the jobs page (the trail's "all jobs" link). */
    onjobs: () => void
    backTo?: 'overview' | 'library' | 'jobs'
  } = $props()

  let encodes = $state<EncodeRecord[] | null>(null)
  /** The stored plan (the prediction display): null when none recorded. */
  let plan = $state<MoviePlan | null>(null)
  /** The movie's own generation (schema v7): 1 = original source, N+1 =
   * this file is the output of a previous encode row. */
  let fileGeneration = $state<number>(1)
  /** The file row's duration (the SOURCE panel's movie prop and the
   * /api/movie row are the same title; history rows have no duration). */
  const fileDuration = $derived(movie.duration)
  let encodeBusy = $state(false)
  let revealBusy = $state(false)
  let revealError = $state<string | null>(null)
  let pageEl: HTMLElement | null = $state(null)

  /** This movie's slice of the audit trail, newest first. Jobs may target
   * the containing FOLDER (plan-all/scan-all) or exact file paths (encode),
   * so the match is by path containment against both directions: the job
   * path is the movie's ancestor, or the movie sits under the job's scope.
   * Derived from the snapshot the app already holds — no extra request. */
  const jobs = $derived.by(() =>
    allJobs
      .filter((j) => {
        const scope = jobPaths(j)
        if (scope.length === 0) return false
        const m = normPath(movie.path)
        return scope.some((jp) => {
          const n = normPath(jp)
          return n === m || m.startsWith(n + '/') || n.startsWith(m + '/')
        })
      })
      .sort((a, b) => (b.id ?? 0) - (a.id ?? 0))
  )

  /** Path comparison that survives Windows/Unix separators and case. */
  function normPath(v: string): string {
    return v.replace(/[\\/]+/g, '/').replace(/\/$/, '').toLowerCase()
  }

  $effect(() => {
    // the one fetch the detail page truly needs: the COMPLETE encode
    // history (the library list carries only the latest encode per title)
    // plus the stored plan (the prediction display)
    api
      .movie(movie.path)
      .then((d) => {
        encodes = d.history
        plan = d.plan ?? null
        if (d.file.generation != null) fileGeneration = d.file.generation
      })
      .catch(() => (encodes = []))
    pageEl?.focus()
  })

  function handleKeydown(e: KeyboardEvent) {
    if (e.key === 'Escape') onclose()
  }


  function revealInExplorer() {
    if (revealBusy) return
    revealBusy = true
    api
      .reveal(movie.path)
      .catch((e) => (revealError = e instanceof Error ? e.message : String(e)))
      .finally(() => (revealBusy = false))
  }

  const encode = $derived(movie.encode)
  /** "02:41:41" → "2h 41m" — every duration carries its unit like the rest
   * of the console; the raw HH:MM:SS string stays the fallback. */
  function fmtDuration(raw: string | null): string | null {
    if (!raw) return null
    const parts = raw.split(':').map(Number)
    if (parts.length !== 3 || parts.some(Number.isNaN)) return raw
    const [h, m] = parts
    return h > 0 ? `${h}h ${m}m` : `${m}m ${parts[2]}s`
  }
  const est = $derived(
    encode?.saved_percent != null
      ? encode.saved_percent <= 0
        ? { text: `+${Math.abs(encode.saved_percent).toFixed(1)}% larger`, grew: true }
        : { text: `${encode.saved_percent.toFixed(1)}% saved`, grew: false }
      : null
  )

  /** The stored plan's predicted saving vs the current source size —
   * the same polarity form as `est` ('grew' => red). Null when the plan
   * carries no estimate (skip verdicts without one, pre-v9 rows). */
  const planEst = $derived(
    plan?.estimated_size && movie.size && movie.size > 0
      ? ((1 - plan.estimated_size / movie.size) * 100).toFixed(1)
      : null
  )
  /** The plan's verdict text for the panel head area: the skip reason in
   * amber, or the CRF it would encode at. */
  const planVerdict = $derived(
    plan?.skipped_reason
      ? plan.skipped_reason
      : plan?.crf != null
        ? `CRF ${plan.crf}`
        : null
  )
  /** The plan-vs-encode comparison line (case 2: both exist) — the plan's
   * predicted size against the latest encode's real output. Signed like
   * every delta in this console. */
  const planVsEncode = $derived.by(() => {
    if (!plan?.estimated_size || !encode?.output_size || plan.skipped_reason) return null
    const diff = plan.estimated_size - encode.output_size
    const pct = (diff / encode.output_size) * 100
    return { text: `${diff <= 0 ? '−' : '+'}${Math.abs(pct).toFixed(1)}% vs encode`, good: diff <= 0 }
  })

  /** Source bitrate in Mbps (size per second) — the honest "how fat is
   * this source" figure next to the encode's own bitrate. Returns a
   * NUMBER so drift-slot math and .toFixed at the display sites see
   * real numbers (a string here silently coerced in arithmetic but
   * crashed on .toFixed — the drift summary was the first caller). */
  function bitrateMbps(size: number | null | undefined, duration: string | null): number | null {
    if (!size || !duration) return null
    const parts = duration.split(':').map(Number)
    if (parts.length !== 3 || parts.some(Number.isNaN)) return null
    const seconds = parts[0] * 3600 + parts[1] * 60 + parts[2]
    if (seconds <= 0) return null
    return (size * 8) / 1_000_000 / seconds
  }

  /** Newest first for the history table. */
  const encodesDesc = $derived(encodes ? [...encodes].reverse() : [])

  // ── drift chart: the provenance chain as a shape ──
  // The chain table carries the numbers; the chart carries the SHAPE —
  // rip 5.0 MBit/s collapsing to 1.0 across generations is the product's
  // thesis, and a column of numbers hides a collapse. Oldest → newest
  // left to right, per-encode slots: bitrate in (seam) vs out (teal)
  // bars, VMAF as points on a fixed 70–100 track (the target clamp's
  // domain) with the amber target mark. Rendered only from ≥2 rows —
  // one encode is a row, not a trend. Pre-v7 rows fall back to the
  // size/duration derivation (marked); unmeasurable slots stay empty
  // (an em-dash vocabulary, not a fabricated bar).
  interface DriftSlot {
    gen: number
    label: string
    inBr: number | null
    outBr: number | null
    inDerived: boolean
    outDerived: boolean
    vmaf: number | null
    i: number
  }
  const driftSlots = $derived.by<DriftSlot[]>(() => {
    if (!encodes || encodesDesc.length < 2) return []
    return [...encodesDesc].reverse().map((rec, i) => {
      const inDerived = rec.input_bitrate == null
      const outDerived = rec.output_bitrate == null
      return {
        gen: rec.generation ?? 1,
        label: `GEN ${rec.generation ?? 1}`,
        inBr: rec.input_bitrate ?? bitrateMbps(rec.input_size, fileDuration),
        outBr: rec.output_bitrate ?? bitrateMbps(rec.output_size, fileDuration),
        inDerived,
        outDerived,
        vmaf: rec.vmaf ?? null,
        i,
      }
    })
  })
  // The staircase: one bar per step of the chain, descending left →
  // right. Round-6.8 finding (user review): per-encode IN/OUT bar pairs
  // duplicated every data point — along a chain out(k) IS in(k+1) — and
  // a metrics-less run inside the steps desynced labels from bands.
  // The staircase tells the MEASURED story only: SOURCE (the first
  // measurable input) + one bar per encode that produced numbers,
  // labeled by generation. Runs without numbers stay in the table
  // below, where the em-dash vocabulary lives — the chart summarizes,
  // the table is the complete record.
  interface DriftStep {
    label: string
    br: number | null
    derived: boolean
    kind: 'source' | 'encode'
    vmaf: number | null
  }
  const driftSteps = $derived.by<DriftStep[]>(() => {
    if (driftSlots.length < 2) return []
    const steps: DriftStep[] = []
    // the source step: the first encode's measurable input (same
    // content, so the viewed file's duration is the right denominator)
    const first = driftSlots.find((s) => s.inBr != null)
    if (first) steps.push({ label: 'SOURCE', br: first.inBr, derived: first.inDerived, kind: 'source', vmaf: null })
    for (const s of driftSlots) {
      if (s.outBr == null) continue
      steps.push({ label: `GEN ${s.gen}`, br: s.outBr, derived: s.outDerived, kind: 'encode', vmaf: s.vmaf })
    }
    return steps
  })
  const driftMaxBr = $derived(Math.max(...driftSteps.map((s) => s.br ?? 0), 0.1))
  /** Chart width: measured container, capped at 220px per step band.
   * Round-6.9 finding (user review): stretched full-panel (~1460px),
   * three bars sat ~480px apart — isolated pillars, not a staircase.
   * Left-aligned (xMinYMid), the descent reads as one connected shape. */
  /** Chart width in real pixels, measured from the container (bind:clientWidth
   * — svelte's measured width, default 560 before first layout). */
  let driftW = $state(0)
  const chartW = $derived(Math.min(driftW || 560, Math.max(driftSteps.length, 2) * 220))
  /** Bar top for a bitrate (bars grow up from the y=66 baseline). */
  const stepTop = (br: number) => 66 - (br / driftMaxBr) * 52
  /** VMAF lane: fixed 85–100 domain, clamped below 85 (the table carries
   * the exact numbers; the lane's job is showing distance-to-target, and
   * a 70–100 domain gave that distance 0.47px per point — unreadable).
   * Group-local y (group translated to y=84): 100 → y6, 85 → y20. */
  const vmafLaneY = (v: number) => 20 - ((Math.min(100, Math.max(85, v)) - 85) / 15) * 14
  const driftSummary = $derived.by(() => {
    if (driftSteps.length < 2) return ''
    // the drift story, told by the staircase: the source step → the last
    // step. Tilde marks a derived endpoint (the table's own convention).
    const firstStep = driftSteps[0]
    const lastStep = [...driftSteps].reverse().find((s) => s.br != null)
    if (firstStep.br != null && lastStep?.br != null) {
      const f = firstStep.derived ? '~' : ''
      const l = lastStep.derived ? '~' : ''
      return `bitrate ${f}${firstStep.br.toFixed(1)} → ${l}${lastStep.br.toFixed(1)} Mbps across ${driftSlots.length} encodes`
    }
    return `${driftSlots.length} encodes, oldest → newest`
  })
  /** Distinct generations in the chain — the head counts GENERATIONS, not
   * rows (two encodes of the same original are one generation, and "2
   * GENERATIONS" over two gen-1 rows was the round-5 finding). */
  const distinctGenerations = $derived(
    new Set(encodesDesc.map((r) => r.generation ?? 1)).size
  )
  /** Deltas between consecutive encodes (newest vs its predecessor).
   * Returns structured parts so the markup can sign AND color them —
   * parenthesized prose made polarity a parse job (round-6.9 review:
   * the battle-proven form is signed+colored, like every diff view). */
  function deltaParts(current: EncodeRecord, previous: EncodeRecord | null): { text: string; good: boolean }[] {
    if (!previous) return []
    const parts: { text: string; good: boolean }[] = []
    if (previous.output_size && current.output_size) {
      const diff = current.output_size - previous.output_size
      const pct = (diff / previous.output_size) * 100
      parts.push({ text: `${diff <= 0 ? '−' : '+'}${Math.abs(pct).toFixed(1)}% size`, good: diff <= 0 })
    }
    if (previous.vmaf != null && current.vmaf != null) {
      const d = current.vmaf - previous.vmaf
      parts.push({ text: `${d >= 0 ? '+' : '−'}${Math.abs(d).toFixed(1)} VMAF`, good: d >= 0 })
    }
    return parts
  }
</script>

<svelte:window onkeydown={handleKeydown} />

<div class="page page-in" tabindex="-1" bind:this={pageEl}>
  <header class="page-head">
    <button class="nav-link back" onclick={onclose}>← {backTo === 'overview' ? 'OVERVIEW' : backTo === 'jobs' ? 'JOBS' : 'LIBRARY'}</button>
    <!-- the trail here is a filtered slice; the full log lives on the jobs page -->
    <button class="nav-link" onclick={onjobs}>ALL JOBS →</button>
  </header>

  <!-- ══ hero: the title as the page header ══ -->
  <section class="hero">
    <div class="hero-main">
      <h2>{movie.name}</h2>
      <p class="mono dim path">{movie.path}</p>
      <div class="chips">
        <span class="chip neutral"><span class="swatch"></span>{movie.category.toUpperCase()}</span>
        {#if fileGeneration > 1}<span class="chip progress" title="This file is itself the output of a previous encode (generation chain)"><span class="swatch"></span>GENERATION {fileGeneration}</span>{/if}
        {#if movie.hdr}<span class="chip neutral"><span class="swatch"></span>HDR</span>{/if}
        {#if movie.missing}<span class="chip missing"><span class="swatch"></span>MISSING SINCE {movie.missing_since?.slice(0, 10) ?? 'LAST SCAN'}</span>{/if}
      </div>
    </div>
    <div class="hero-actions">
      {#if revealAvailable}
        {#if revealError}<span class="mono bad reveal-err">{revealError}</span>{/if}
        <button class="act quiet" onclick={revealInExplorer} disabled={revealBusy} title="Open the containing folder in the file manager">
          {revealBusy ? 'OPENING…' : 'FOLDER'}
        </button>
      {/if}
    </div>
  </section>

  <div class="cols">
    <!-- ══ source facts ══ -->
    <section class="panel">
      <div class="panel-head"><span class="legend">SOURCE</span></div>
      <div class="facts">
        <div class="fact"><span class="legend">RESOLUTION</span><span class="mono">{movie.resolution?.join('×') ?? '—'}</span></div>
        <div class="fact"><span class="legend">DURATION</span><span class="mono">{fmtDuration(movie.duration) ?? '-'}</span></div>
        <div class="fact"><span class="legend">CODEC</span><span class="mono">{movie.codec ?? '—'}</span></div>
        <div class="fact"><span class="legend">SIZE</span><span class="mono">{fmtGb(movie.size)}</span></div>
        {#if bitrateMbps(movie.size, movie.duration) != null}
          <div class="fact">
            <span class="legend">SOURCE BITRATE</span>
            <span class="mono">{bitrateMbps(movie.size, movie.duration)?.toFixed(1)} Mbps</span>
          </div>
        {/if}
        {#if fileGeneration > 1}
          <div class="fact">
            <span class="legend">GENERATION</span>
            <span class="mono">#{fileGeneration} — an encode of an encode</span>
          </div>
        {/if}
      </div>
    </section>

    <!-- ══ latest encode: every recorded metric ══ -->
    <section class="panel">
      <div class="panel-head">
        <span class="legend">{plan ? 'LATEST ENCODE — MEASURED · PLAN BELOW' : 'LATEST ENCODE — MEASURED'}</span>
      </div>
      {#if !encode}
        {#if plan}
          <!-- case 1: no encode yet — the plan IS the prediction; the
               measured panel stays honest by never pretending these
               numbers were measured -->
          <div class="facts">
            {#if plan.skipped_reason}
              <!-- the plan rejected the movie: the verdict IS the headline -->
              <div class="fact big">
                <span class="legend">PREDICTED</span>
                <span class="mono result warn">skip — {plan.skipped_reason}</span>
              </div>
              {#if plan.achieved_vmaf != null && plan.crf != null}
                <div class="fact">
                  <span class="legend">BEST FOUND</span>
                  <span class="mono">CRF {plan.crf} · VMAF {plan.achieved_vmaf.toFixed(1)} (samples)</span>
                </div>
              {/if}
            {:else}
              <div class="fact big">
                <span class="legend">PREDICTED</span>
                <span class="mono result" class:bad={planEst != null && Number(planEst) < 0}>
                  {planEst != null ? (Number(planEst) <= 0 ? `+${Math.abs(Number(planEst))}% larger (est.)` : `${planEst}% saved (est.)`) : '—'}
                </span>
              </div>
              {#if plan.estimated_size != null && planEst != null}
                <div class="fact"><span class="legend">SIZE</span><span class="mono">{fmtGb(movie.size)} → {fmtGb(plan.estimated_size)} (est.)</span></div>
                {#if plan.estimate_note}
                  <div class="fact"><span class="legend">NOTE</span><span class="mono dim">{plan.estimate_note}</span></div>
                {/if}
              {/if}
              {#if plan.crf != null}
                <div class="fact"><span class="legend">CRF</span><span class="mono">{plan.crf}</span></div>
              {/if}
              {#if plan.achieved_vmaf != null}
                <div class="fact">
                  <span class="legend">VMAF</span>
                  <span class="mono measured">{plan.achieved_vmaf.toFixed(1)}</span>
                  <span class="dim">samples — encode will confirm</span>
                </div>
              {/if}
            {/if}
            <div class="fact">
              <span class="legend">PROBED</span>
              <span class="mono">{fmtWhen(plan.created_at)}</span>
              {#if plan.stale}<span class="mono warn">FILE CHANGED SINCE</span>{/if}
            </div>
            <div class="fact">
              <span class="legend">SETTINGS</span>
              <span class="mono">VMAF > {plan.target_vmaf ?? '—'} · {plan.encoder ?? '—'} · {plan.encoder_preset ?? '—'}</span>
            </div>
          </div>
        {:else}
          <div class="dim pad">not encoded yet — plan predicts, encode measures</div>
        {/if}
      {:else}
        <div class="facts">
          <div class="fact big">
            <span class="legend">RESULT</span>
            <span class="mono result" class:bad={est?.grew}>{est?.text ?? '—'}</span>
          </div>
          <div class="fact"><span class="legend">SIZE</span><span class="mono">{fmtGb(encode.input_size)} → {fmtGb(encode.output_size)}</span></div>
          <div class="fact">
            <span class="legend">VMAF</span>
            {#if encode.vmaf != null}
              <span class="gauge" role="img" aria-label="VMAF {encode.vmaf.toFixed(1)} of target {targetVmaf}">
                <span class="gauge-fill" style="width: {Math.min(100, encode.vmaf)}%"></span>
                <span class="gauge-target" style="left: {targetVmaf}%"></span>
              </span>
              <span class="mono measured">{encode.vmaf.toFixed(1)}</span>
            {:else}
              <!-- no gauge for an unmeasured row: an empty instrument track
                   next to the dash read as a broken meter (round-5) -->
              <span class="mono">— fixed mode, not measured</span>
            {/if}
          </div>
          <div class="fact"><span class="legend">CRF</span><span class="mono">{encode.crf ?? '—'}</span></div>
          <div class="fact"><span class="legend">PRESET</span><span class="mono">{encode.preset}</span></div>
          {#if encode.encoder_preset}
            <div class="fact"><span class="legend">ENCODER PRESET</span><span class="mono">{encode.encoder_preset}</span></div>
          {/if}
          {#if encode.encode_seconds != null}
            <div class="fact"><span class="legend">ENCODE TIME</span><span class="mono">{fmtSeconds(encode.encode_seconds)}</span></div>
          {/if}
          {#if bitrateMbps(encode.output_size, movie.duration) != null}
            <div class="fact"><span class="legend">OUTPUT BITRATE</span><span class="mono">{bitrateMbps(encode.output_size, movie.duration)?.toFixed(1)} Mbps</span></div>
          {/if}
          {#if encode.probe_count != null}
            <div class="fact">
              <span class="legend">PROBES</span>
              <span class="mono">{encode.probe_count} · {fmtSeconds(encode.probe_seconds)}</span>
            </div>
          {/if}
          {#if encode.tool_versions}
            <div class="fact"><span class="legend">TOOLCHAIN</span><span class="mono tv">{encode.tool_versions}</span></div>
          {/if}
          {#if plan && planVsEncode}
            <!-- case 2: encode measured AND a plan exists — the next
                 encode's prediction, comparable against the real row -->
            <div class="fact plan-next">
              <span class="legend">NEXT PLAN · {fmtWhen(plan.created_at)}{plan.stale ? ' · FILE CHANGED SINCE' : ''}</span>
              <span class="mono" class:good={planVsEncode.good} class:bad={!planVsEncode.good}>{planVsEncode.text}</span>
              {#if plan.crf != null}<span class="mono dim">· CRF {plan.crf}</span>{/if}
            </div>
          {/if}
        </div>
      {/if}
    </section>

    <!-- ══ encode history: the provenance chain (schema v7) ══ -->
    <section class="panel wide">
      <div class="panel-head"><span class="legend">PROVENANCE CHAIN — {encodesDesc.length || 'NO'} ENCODE{encodesDesc.length === 1 ? '' : 'S'} · {distinctGenerations} GENERATION{distinctGenerations === 1 ? '' : 'S'}</span></div>
      {#if encodes === null}
        <div class="dim pad">loading…</div>
      {:else if encodesDesc.length === 0}
        <div class="dim pad">no encode recorded — the title is untouched</div>
      {:else}
        {#if driftSlots.length >= 2}
          <!-- the drift chart: the provenance chain as a descending
               staircase (SOURCE → GEN 1 → GEN 2 …) + VMAF dots on a
               fixed 85-100 lane below. Geometry in viewBox units on an
               unstretched SVG, width measured from the container. -->
          <div class="drift mono" role="img" aria-label="Drift chart: bitrate staircase and VMAF across {driftSlots.length} encodes, oldest to newest. {driftSummary}">
            <div class="drift-svg" bind:clientWidth={driftW}>
              <svg viewBox="0 0 {chartW} 110" preserveAspectRatio="xMinYMid meet" aria-hidden="true">
              <!-- bitrate staircase: one bar per step, descending left →
                   right on one shared scale; steps labeled by generation,
                   value labeled on top; a no-data step keeps its axis.
                   Headroom: labels sit above bar tops — the chart top is
                   y=8, never 0 (a label at the viewBox edge amputates). -->
              <g class="br-lane">
                {#each driftSteps as s, i (i)}
                  {@const cx = ((i + 0.5) / driftSteps.length) * chartW}
                  <line x1={cx} y1="8" x2={cx} y2="66" class="slot-rule"></line>
                  {#if s.br != null}
                    <rect
                      x={cx - 8}
                      y={stepTop(s.br)}
                      width="16"
                      height={66 - stepTop(s.br)}
                      class="step {s.kind}"
                      class:derived={s.derived}
                    ></rect>
                    <text x={cx} y={stepTop(s.br) - 4} class="step-value" text-anchor="middle">{s.derived ? '~' : ''}{s.br.toFixed(1)}</text>
                  {/if}
                  <text x={cx} y="78" class="step-label" text-anchor="middle">{s.label}</text>
                {/each}
                <!-- the descent, drawn: thin connector across step tops so
                     the eye reads one falling line, not three pillars -->
                <path d="M {driftSteps.map((s, i) => `${i > 0 ? 'L' : ''} ${((i + 0.5) / driftSteps.length) * chartW} ${s.br != null ? stepTop(s.br) : 66}`).join(' ').trim()}" class="step-line"></path>
                <line x1="0" y1="66" x2={chartW} y2="66" class="base-rule"></line>
              </g>
              <!-- VMAF lane below the staircase: fixed 85-100 domain, same
                   slot centers as the steps, so each dot sits under the
                   encode it belongs to (the source has no VMAF) -->
              <g class="vmaf-track" transform="translate(0, 84)">
                <text x="2" y="3" class="lane-label">VMAF 100</text>
                <line x1="0" y1="6" x2={chartW} y2="6" class="track-rule"></line>
                <text x="2" y="17" class="lane-label">85</text>
                <line x1="0" y1="20" x2={chartW} y2="20" class="track-rule"></line>
                <line x1="0" y1="{vmafLaneY(targetVmaf)}" x2={chartW} y2="{vmafLaneY(targetVmaf)}" class="target-rule"></line>
                {#each driftSteps as s, i (i)}
                  {#if s.vmaf != null}
                    <circle cx={((i + 0.5) / driftSteps.length) * chartW} cy={vmafLaneY(s.vmaf)} r="2.5" class="vmaf-dot" class:under={s.vmaf < targetVmaf}></circle>
                  {/if}
                {/each}
              </g>
            </svg>
            </div>
            <div class="drift-legend legend">
              <span><span class="sw sw-src"></span> SOURCE BITRATE</span>
              <span><span class="sw sw-enc"></span> ENCODE OUTPUT</span>
              <span><span class="sw sw-vmaf"></span> VMAF (85–100)</span>
              <span title="dashed bar = value derived from size/duration (pre-v7 rows without ffprobe bitrates)">DASHED = DERIVED</span>
              <span class="spacer"></span>
              <span class="dim">{driftSummary}</span>
            </div>
          </div>
        {/if}
        <div class="enc-table mono">
          <div class="enc-row enc-head legend" aria-hidden="true">
            <span>GEN</span>
            <span>WHEN</span>
            <span class="num">CRF</span>
            <span class="num">VMAF</span>
            <span class="num">BITRATE</span>
            <span class="num">SIZE</span>
            <span class="num">SAVED</span>
            <span class="num">TIME</span>
            <span class="delta"></span>
          </div>
          {#each encodesDesc as rec, i (rec.id ?? `${rec.encoded_at}-${i}`)}
            {@const producedThis = rec.output_file != null && normPath(rec.output_file) === normPath(movie.path)}
            {@const fromElsewhere = rec.file_path != null && normPath(rec.file_path) !== normPath(movie.path)}
            <div class="enc-row">
              <span
                class="gen"
                class:current={producedThis}
                title={producedThis ? 'this row produced the file being viewed' : (rec.generation ?? 1) === 1 ? 'encode of the original source' : `encode of generation ${(rec.generation ?? 1) - 1}`}
                aria-label={producedThis ? `generation ${rec.generation ?? 1}: this row produced the file being viewed` : `generation ${rec.generation ?? 1}`}
              >{rec.generation ?? 1}</span>
              <span class="dim">{fmtWhen(rec.encoded_at)}{fromElsewhere ? ` · ${rec.file_name}` : ''}</span>
              <span class="num">{rec.crf ?? '—'}</span>
              <span class="num" class:measured={rec.vmaf != null}>{rec.vmaf?.toFixed(1) ?? '—'}</span>
              <!-- the drift signal: measured in→out MBit/s (v7); the derived
                   fallback keeps pre-v7 rows honest without pretending -->
              <span
                class="num"
                title={rec.input_bitrate != null ? 'measured by ffprobe (format bitrate)' : 'derived from size/duration (pre-v7 row)'}
                aria-label={rec.input_bitrate != null ? `bitrate ${rec.input_bitrate.toFixed(1)} to ${rec.output_bitrate?.toFixed(1)} Mbps, measured by ffprobe` : 'bitrate derived from size and duration (pre-v7 row)'}
              >
                {#if rec.input_bitrate != null && rec.output_bitrate != null}
                  {rec.input_bitrate.toFixed(1)} → {rec.output_bitrate.toFixed(1)}
                {:else if bitrateMbps(rec.input_size, fileDuration) != null && bitrateMbps(rec.output_size, fileDuration) != null}
                  ~{bitrateMbps(rec.input_size, fileDuration)?.toFixed(1)} → ~{bitrateMbps(rec.output_size, fileDuration)?.toFixed(1)}
                {:else}
                  —
                {/if}
              </span>
              <span class="num">{fmtGb(rec.output_size)}</span>
              <span class="num" class:bad={(rec.saved_percent ?? 0) <= 0}>{rec.saved_percent != null ? `${rec.saved_percent.toFixed(0)}%` : '—'}</span>
              <span class="num">{rec.encode_seconds != null ? fmtSeconds(rec.encode_seconds) : '—'}</span>
              <span class="delta" title="delta vs the previous run (a comparison between consecutive encodes — siblings of one generation are two runs of the same source)">
                {#if i < encodesDesc.length - 1}
                  {#each deltaParts(rec, encodesDesc[i + 1]) as part, pi (pi)}
                    <span class="mono" class:good={part.good} class:bad={!part.good}>{part.text}</span>{pi < deltaParts(rec, encodesDesc[i + 1]).length - 1 ? ' · ' : ''}
                  {/each}
                {:else}
                  <span class="dim">first run</span>
                {/if}
              </span>
            </div>
          {/each}
        </div>
      {/if}
    </section>

    <!-- ══ job audit trail for this title ══ -->
    <section class="panel wide">
      <div class="panel-head"><span class="legend">JOB TRAIL</span></div>
      {#if jobs.length === 0}
        <div class="dim pad">no jobs for this movie yet</div>
      {:else}
        {#each jobs as job (job.id)}
          {@const outcome = job.id != null ? movieOutcome(job, movie.name) : null}
          {@const partial = failedOutcomeNote(job) !== ''}
          <div class="hist-row mono">
            <span class="dim">#{job.id}</span>
            <span>{job.kind}</span>
            <!-- the job's lifecycle state, plus what THIS movie's slice of
                 it did: a batch DONE can hold this movie's failure (the
                 watchdog case read as green 'done' here), a plan DONE
                 holds this movie's verdict -->
            <span class={partial ? 'partial' : job.state === 'done' ? 'ok' : job.state === 'failed' ? 'bad' : 'dim'}>
              {job.state}{#if partial} · partial{/if}
            </span>
            <span class="dim">{fmtWhen(job.finished_at ?? job.started_at)}</span>
            <!-- the movie's own outcome first (it names the real failure),
                 then the job-level error as fallback -->
            <span
              class="dim err"
              class:bad={outcome?.state === 'failed'}
              title={outcome?.error ?? job.error ?? ''}
            >
              {#if outcome?.state === 'failed'}
                {outcome.error ? humanizeError(null, outcome.error) : 'failed'}
              {:else if outcome?.state === 'skip'}
                skip — {outcome.reason ?? 'no size gain'}
              {:else if outcome?.state === 'already_encoded'}
                already encoded
              {:else if outcome?.state === 'cancelled'}
                cancelled
              {:else if job.error}
                {humanizeError(null, job.error)}
              {:else if job.kind === 'plan' && outcome}
                {#if outcome.crf != null}
                  CRF {outcome.crf}{#if outcome.vmaf != null} · VMAF {outcome.vmaf.toFixed(1)}{/if}
                {:else}
                  done
                {/if}
              {/if}
            </span>
          </div>
        {/each}
      {/if}
    </section>
  </div>
</div>

<style>
  .page { display: flex; flex-direction: column; gap: 14px; outline: none; }
  .page-head { display: flex; justify-content: space-between; align-items: baseline; }
  .back {
    background: transparent;
    border: none;
    color: var(--teal-ink);
    cursor: pointer;
    padding: 0;
    align-self: flex-start;
    font-size: var(--fs-legend);
    letter-spacing: var(--track-legend);
    text-transform: uppercase;
  }
  .back:hover { text-decoration: underline; }
  /* the ALL JOBS link shares the back link's voice (two trail ends) */
  .page-head .nav-link {
    background: transparent;
    border: none;
    color: var(--teal-ink);
    cursor: pointer;
    padding: 0;
    font-size: var(--fs-legend);
    letter-spacing: var(--track-legend);
    text-transform: uppercase;
  }
  .page-head .nav-link:hover { text-decoration: underline; }
  .dim { color: var(--ink-dim); }
  .bad { color: var(--red); }
  .ok { color: var(--teal-ink); }

  .hero {
    display: flex;
    justify-content: space-between;
    align-items: flex-start;
    gap: 18px;
    flex-wrap: wrap;
  }
  .hero h2 { font-size: 22px; font-weight: 700; }
  .path { margin: 4px 0 10px; word-break: break-all; }
  .chips { display: flex; gap: 6px; flex-wrap: wrap; }
  .hero-actions { display: flex; gap: 8px; align-items: center; }
  .reveal-err { max-width: 260px; word-break: break-word; }

  .act {
    background: var(--panel-raised);
    border: 1px solid var(--seam);
    border-radius: var(--radius);
    color: var(--ink);
    padding: 6px 16px;
    cursor: pointer;
    font-size: var(--fs-legend);
    letter-spacing: var(--track-legend);
    text-transform: uppercase;
  }
  .act:hover:not(:disabled) { border-color: var(--teal); color: var(--teal-ink); }
  .act.primary { background: var(--teal); border-color: var(--teal); color: var(--ground); }
  /* teal text on the teal fill was unreadable (round-7): the primary
     keeps its ground-on-teal ink and brightens instead */
  .act.primary:hover:not(:disabled) { filter: brightness(1.08); color: var(--ground); }
  .act.quiet { border-color: var(--seam); color: var(--ink-dim); }
  .act:disabled { opacity: 0.4; cursor: default; }

  .cols {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 10px;
  }
  .panel {
    border: 1px solid var(--seam);
    border-radius: var(--radius);
    background: var(--panel);
  }
  .panel.wide { grid-column: 1 / -1; }
  .panel-head {
    padding: 9px 14px;
    border-bottom: 1px solid var(--row-line);
  }
  .facts { display: flex; flex-direction: column; gap: 5px; padding: 12px 14px 14px; }
  .fact { display: flex; justify-content: space-between; gap: 12px; align-items: baseline; }
  .fact.big .result { font-size: 15px; font-weight: 600; color: var(--teal-ink); }
  .fact.big .result.bad { color: var(--red); }
  /* plan predictions: amber marks the file's staleness and skip verdicts —
     visible, never blocking (an encode may still override a skip) */
  .fact.big .result.warn { color: var(--amber); }
  .mono.warn { color: var(--amber); }
  .fact.plan-next {
    border-top: 1px solid var(--row-line);
    padding-top: 7px;
    margin-top: 3px;
    flex-wrap: wrap;
  }
  .fact.plan-next .legend { flex: none; }
  .measured { color: var(--teal-ink); font-weight: 600; }
  .tv { word-break: break-all; text-align: right; }
  .pad { padding: 16px 14px; }
  .hist-row {
    display: flex;
    gap: 14px;
    padding: 6px 14px;
    border-bottom: 1px solid var(--row-line);
    font-size: var(--fs-data);
  }
  .hist-row:last-child { border-bottom: none; }
  .err { flex: 1; overflow: hidden; text-overflow: ellipsis; }
  .err.bad { color: var(--red); }
  /* batch DONE holding this movie's failure — the jobs page's semantics */
  .partial { color: var(--amber); }

  /* VMAF gauge: fill vs the target mark */
  .fact .gauge {
    position: relative;
    flex: 1;
    max-width: 180px;
    height: 8px;
    background: var(--ground);
    border: 1px solid var(--row-line);
    border-radius: 4px;
    overflow: hidden;
  }
  .gauge-fill {
    position: absolute;
    inset: 0 auto 0 0;
    background: var(--teal);
  }
  .gauge-target {
    position: absolute;
    top: -2px;
    bottom: -2px;
    width: 2px;
    background: var(--amber);
  }

  /* encode history table */
  .enc-table { display: flex; flex-direction: column; padding: 6px 14px 12px; }

  /* ── drift chart: the chain as a shape ── */
  .drift { padding: 10px 14px 8px; border-bottom: 1px solid var(--row-line); }
  .drift-svg { width: 100%; max-width: 100%; }
  .drift svg { display: block; width: 100%; height: 108px; }
  .drift .track-rule { stroke: var(--row-line); stroke-width: 1; }
  .drift .track-rule.faint { stroke: var(--row-line); stroke-dasharray: 2 3; }
  .drift .base-rule { stroke: var(--row-line); stroke-width: 1; }
  .drift .step-line { fill: none; stroke: var(--ink-faint); stroke-width: 1; }
  .drift .lane-label { fill: var(--ink-faint); font-size: 6.5px; letter-spacing: 0.5px; }
  .drift .target-rule { stroke: var(--amber); stroke-width: 1; stroke-dasharray: 4 3; }
  .drift .vmaf-dot { fill: var(--teal); }
  /* under-target: amber RING, not amber fill — values cluster just under
     the target, and an amber dot ON the amber target line disappears */
  .drift .vmaf-dot.under { fill: var(--teal); stroke: var(--amber); stroke-width: 1.5; }
  .drift .step { fill: var(--ink-dim); }
  .drift .step.encode { fill: var(--teal); opacity: 0.85; }
  /* derived values (pre-v7, size/duration math) draw dashed — a derived
     bar must not dress up as a measured one */
  .drift .step.derived { fill: none; stroke: var(--ink-dim); stroke-dasharray: 2 2; }
  .drift .step.encode.derived { stroke: var(--teal); }
  .drift .step-value { fill: var(--ink-dim); font-size: 7px; }
  .drift .step-label { fill: var(--ink-faint); font-size: 7px; letter-spacing: 0.5px; }
  .drift .slot-rule { stroke: var(--row-line); stroke-width: 1; }
  .drift-legend {
    display: flex;
    gap: 16px;
    align-items: center;
    margin-top: 6px;
    font-size: 9.5px;
    letter-spacing: 0.8px;
  }
  .drift-legend .spacer { flex: 1; }
  .drift-legend .sw {
    display: inline-block;
    width: 8px;
    height: 8px;
    margin-right: 4px;
    vertical-align: middle;
    border-radius: 1px;
  }
  .drift-legend .sw-src { background: var(--ink-dim); }
  .drift-legend .sw-enc { background: var(--teal); opacity: 0.85; }
  .drift-legend .sw-vmaf { border-radius: 50%; background: var(--teal); }  .enc-row {
    display: grid;
    grid-template-columns: 34px 90px 40px 50px 110px 80px 50px 70px 1fr;
    gap: 10px;
    align-items: center;
    padding: 4px 0;
    border-bottom: 1px solid var(--row-line);
    font-size: 11px;
  }
  .enc-row:last-child { border-bottom: none; }
  .enc-head { border-bottom: 1px solid var(--seam); color: var(--ink-faint); font-size: 10px; letter-spacing: 1px; }
  .num { text-align: right; }
  .gen {
    text-align: center;
    color: var(--ink-faint);
    border: 1px solid var(--row-line);
    border-radius: 2px;
    padding: 0 4px;
    justify-self: start;
  }
  /* the row that produced the file being viewed */
  .gen.current { color: var(--teal-ink); border-color: var(--teal); }
  .delta { text-align: right; font-size: 10px; white-space: nowrap; }
  /* signed, colored deltas: teal = the change is good (smaller, better),
     red = it is not — polarity readable without parsing prose */
  .delta .good { color: var(--teal-ink); }
  .delta .bad { color: var(--red); }

  @media (max-width: 820px) {
    /* same min-content clamp as the overview's 640px rule (round-6 P1
       class): a bare `1fr` track lets nowrap content set the track width */
    .cols { grid-template-columns: minmax(0, 1fr); }
  }
</style>

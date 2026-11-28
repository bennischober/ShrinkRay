import { expect, test, type Page } from '@playwright/test'

/** Issue #661's own acceptance test lives in its own file so the trace can be
 * off for it: the trace's DOM snapshots are measurement noise on a 2 000-row
 * page. The busy fixture's freeze was environment-dependent (the CI runner took
 * 129 s; an unthrottled workstation answers in milliseconds even before the
 * fix), so the test pins a slow device the way the runner behaves: a 12x CPU
 * throttle. It covers both measured causes: `busy` (an unchanged collection —
 * the mock must answer it with null, like the resident app) and
 * `busy-changing` (a library that really moves every poll — the hidden pages
 * must not re-render it). The visible Library page is the measured-tightest
 * (962 ms of the 1 000 ms bound), so the changing payload is read there too.
 * A read that never returns fails through the hard 5 s ceiling; the assertion is
 * the MEDIAN read, not the worst: a single concurrent-load spike is not a
 * regression, while the pre-fix freeze made every read slow.
 *
 * Three tests, one per target (issue #736): a single test ran all three
 * targets serially, so one retry cost the whole ~84 s and desktop-1440's job
 * could exceed the 4-minute browser-phase bound on a single slow target. A
 * retry now re-runs one target's setup and measurement, not all three. */

test.use({ trace: 'off' })

/** A single read that never returns is a hang, not a slow frame: this ceiling
 * is far above every measured concurrent-load spike and below the test timeout. */
const HARD_READ_CEILING_MS = 5_000

interface BusyTarget {
  fixture: string
  hash: string
  boundMs: number
}

const TARGETS: Record<string, BusyTarget> = {
  'library · busy-changing': { fixture: 'busy-changing', hash: '#/library', boundMs: 2_000 },
  'config · busy': { fixture: 'busy', hash: '#/config', boundMs: 1_000 },
  'config · busy-changing': { fixture: 'busy-changing', hash: '#/config', boundMs: 1_000 },
}

async function measureBusyRead(page: Page, target: BusyTarget): Promise<number> {
  const client = await page.context().newCDPSession(page)
  await client.send('Emulation.setCPUThrottlingRate', { rate: 12 })
  // the counter is installed before every navigation, so each scenario counts
  // its own polls; no test-side wait or sleep decides when the page is ready
  await page.addInitScript(() => {
    const state = window as unknown as { __snapshotPolls: number }
    state.__snapshotPolls = 0
    const original = window.fetch
    window.fetch = async (...args) => {
      const response = await original(...args)
      const url = typeof args[0] === 'string' ? args[0] : args[0]?.url
      if (url?.includes('/api/snapshot')) state.__snapshotPolls += 1
      return response
    }
  })

  // The library row keeps its 2,000 ms bound, but the assertion is the median
  // read, not the worst: this dev machine measured 962-984 ms alone and a
  // concurrent gate+pytest run produced a 2,533 ms single spike (2/276 failures at
  // the old worst-read bound). The pre-fix freeze made EVERY read 2,788 ms at 10x
  // throttle, so its median still trips the tripwire; a single concurrent spike
  // does not. A read that never returns still fails through the 5 s ceiling.
  await page.goto(`/?fixture=${target.fixture}${target.hash}`, { waitUntil: 'load' })
  await expect(page.locator('main h1:visible').first()).toBeVisible({ timeout: 60_000 })
  // the busy cadence is 1 s: the second poll is a full refresh cycle.
  // Two frames after it only run once the main thread has settled the render.
  await page.waitForFunction(
    () => ((window as unknown as { __snapshotPolls?: number }).__snapshotPolls ?? 0) >= 2,
    null,
    { timeout: 60_000 },
  )
  await page.evaluate(
    () => new Promise<void>((resolve) => requestAnimationFrame(() => requestAnimationFrame(() => resolve()))),
  )

  const samples: number[] = []
  const until = Date.now() + 6_000
  while (Date.now() < until) {
    const started = Date.now()
    let timer: ReturnType<typeof setTimeout> | undefined
    try {
      await Promise.race([
        page.evaluate(() => document.documentElement.scrollWidth),
        new Promise((_, reject) => {
          timer = setTimeout(
            () =>
              reject(
                new Error(
                  `${target.fixture} ${target.hash}: no DOM read within ${HARD_READ_CEILING_MS} ms`,
                ),
              ),
            HARD_READ_CEILING_MS,
          )
        }),
      ])
    } finally {
      if (timer !== undefined) clearTimeout(timer)
    }
    samples.push(Date.now() - started)
  }
  expect(samples.length, `${target.fixture} ${target.hash}: no samples`).toBeGreaterThan(0)
  return [...samples].sort((a, b) => a - b)[Math.floor(samples.length / 2)]
}

for (const [name, target] of Object.entries(TARGETS)) {
  test(`the busy fixture answers a DOM read within a second: ${name} (issue #661)`, async ({
    page,
  }) => {
    test.setTimeout(120_000)
    const median = await measureBusyRead(page, target)
    expect(median, `${target.fixture} ${target.hash}: median DOM read`).toBeLessThan(target.boundMs)
  })
}

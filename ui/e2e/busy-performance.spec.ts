import { expect, test } from '@playwright/test'

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
 * A read that never returns fails through the 1 s race; a read that takes longer
 * fails the assertion. */
test.use({ trace: 'off' })

test('the busy fixture answers a DOM read within a second (issue #661)', async ({ page }) => {
  test.setTimeout(120_000)
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

  for (const target of [
    { fixture: 'busy-changing', hash: '#/library' },
    { fixture: 'busy', hash: '#/config' },
    { fixture: 'busy-changing', hash: '#/config' },
  ]) {
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

    let worst = 0
    const until = Date.now() + 6_000
    while (Date.now() < until) {
      const started = Date.now()
      let timer: ReturnType<typeof setTimeout> | undefined
      try {
        await Promise.race([
          page.evaluate(() => document.documentElement.scrollWidth),
          new Promise((_, reject) => {
            timer = setTimeout(
              () => reject(new Error(`${target.fixture} ${target.hash}: no DOM read within 1000 ms`)),
              1_000,
            )
          }),
        ])
      } finally {
        if (timer !== undefined) clearTimeout(timer)
      }
      worst = Math.max(worst, Date.now() - started)
    }
    expect(worst, `${target.fixture} ${target.hash}: worst DOM read`).toBeLessThan(1_000)
  }
})

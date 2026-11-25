import { expect, test } from '@playwright/test'

/** Issue #631's binary observable: the same running file shows the same
 * stage, percent, ETA and wording on Overview, Jobs, Library and the title
 * page, and the CRF-search probe table lists the scripted probes. The `busy`
 * fixture is a deterministic stream (no timing-dependent waits): the mock writes
 * the events once per EventSource connection, and every page reload reconnects. */
const TITLE = '/media/Filme/Titel 0 (2000).mkv'

const PAGES = [
  { name: 'overview', hash: '#/overview' },
  { name: 'jobs', hash: '#/jobs' },
  { name: 'library', hash: '#/library' },
  { name: 'title', hash: `#/movie/${encodeURIComponent(TITLE)}` },
]

test('the same running file shows the same progress on all four pages (issue #631)', async ({
  page,
}, testInfo) => {
  test.setTimeout(180_000)
  await page.addInitScript((t) => localStorage.setItem('ms.theme', t), 'dark')

  const summaries: Record<string, string> = {}
  const etas: Record<string, string> = {}
  let jobsDuration = ''
  for (const target of PAGES) {
    await page.goto(`/?fixture=busy${target.hash}`, { waitUntil: 'load' })
    const summary = page.locator('[data-work-summary]').first()
    await expect(summary, `${target.name} has no running-work summary`).toBeVisible({
      timeout: 60_000,
    })
    await expect(summary).toContainText('Encode')
    summaries[target.name] = (await summary.textContent())?.trim() ?? ''
    etas[target.name] =
      summaries[target.name].match(/about [^·]+ left/)?.[0]?.trim() ?? ''
    if (target.name === 'jobs') {
      const duration = page.locator('.entry.running [data-work-duration]').first()
      await expect(duration).toBeVisible()
      jobsDuration = (await duration.textContent())?.trim() ?? ''
    }
    await page.screenshot({
      path: `test-results/screenshots/progress-${target.name}-${testInfo.project.name}.png`,
    })
  }

  // same facts, same words: the one model feeds every page
  const unique = new Set(Object.values(summaries))
  expect(unique.size, `the pages disagree: ${JSON.stringify(summaries)}`).toBe(1)
  expect(Object.values(summaries)[0]).toContain('88.0%')
  expect(Object.values(summaries)[0]).toContain('12.3 fps')
  // the Jobs DURATION cell is the shared summary's ETA, not a second wording
  expect(new Set(Object.values(etas)).size).toBe(1)
  expect(jobsDuration).toBe(Object.values(etas)[0])
  expect(jobsDuration).toContain('about 1 min left')

  // the CRF-search section renders the scripted probe trail on the expanded view
  await page.goto('/?fixture=busy#/overview', { waitUntil: 'load' })
  const probes = page.locator('[data-work-progress] .probes').first()
  await expect(probes).toBeVisible({ timeout: 60_000 })
  await expect(probes).toContainText('CRF 30')
  await expect(probes).toContainText('below target')
  await expect(probes).toContainText('CRF 24')
  await expect(page.locator('[data-chosen]')).toContainText('Found CRF 24')
  await page.screenshot({
    path: `test-results/screenshots/progress-probe-table-${testInfo.project.name}.png`,
  })
})

test('the queue outlook states the files waiting and a rough time only with history (issue #631)', async ({
  page,
}, testInfo) => {
  test.setTimeout(120_000)
  await page.addInitScript((t) => localStorage.setItem('ms.theme', t), 'dark')

  // no measured history: the files waiting, never a fabricated time
  await page.goto('/?fixture=busy#/overview', { waitUntil: 'load' })
  const noHistory = page.locator('[data-queue-outlook]').first()
  await expect(noHistory).toBeVisible({ timeout: 60_000 })
  await expect(noHistory).toContainText('40 files waiting')
  await expect(noHistory).not.toContainText('roughly')

  // measured history: the newest five real encodes give a rough time
  await page.goto('/?fixture=busy-history#/overview', { waitUntil: 'load' })
  const history = page.locator('[data-queue-outlook]').first()
  await expect(history).toBeVisible({ timeout: 60_000 })
  await expect(history).toContainText('40 files waiting')
  await expect(history).toContainText('roughly 40 min at the recent pace')

  // the encoder ETA above 10 min shows the wall-clock finish time
  const summary = page.locator('[data-work-summary]').first()
  await expect(summary).toBeVisible({ timeout: 60_000 })
  await expect(summary).toContainText(/finishes ≈ \d{2}:\d{2}/)
  await page.screenshot({
    path: `test-results/screenshots/progress-queue-history-${testInfo.project.name}.png`,
  })
})

test('a cached probe replay is visibly reused (issue #631)', async ({ page }, testInfo) => {
  test.setTimeout(120_000)
  await page.addInitScript((t) => localStorage.setItem('ms.theme', t), 'dark')
  await page.goto('/?fixture=busy-cached#/overview', { waitUntil: 'load' })
  const reused = page.locator('[data-reused]').first()
  await expect(reused).toBeVisible({ timeout: 60_000 })
  await expect(reused).toContainText('reused from the plan cache (measured')
  const probes = page.locator('[data-work-progress] .probes').first()
  await expect(probes).toBeVisible()
  await expect(probes).toContainText('CRF 30')
  await expect(page.locator('[data-work-summary]').first()).toContainText('reused')
  await page.screenshot({
    path: `test-results/screenshots/progress-reused-${testInfo.project.name}.png`,
  })
})

test('the old Overview elapsed/percent estimate is gone (issue #631)', async ({ page }, testInfo) => {
  test.setTimeout(120_000)
  await page.addInitScript((t) => localStorage.setItem('ms.theme', t), 'dark')
  await page.goto('/?fixture=busy#/overview', { waitUntil: 'load' })
  await expect(page.locator('[data-work-summary]').first()).toBeVisible({
    timeout: 60_000,
  })
  const text = (await page.locator('main').textContent()) ?? ''
  expect(text).not.toContain('remaining')
  expect(text).not.toContain('started')
  await page.screenshot({
    path: `test-results/screenshots/progress-no-old-eta-${testInfo.project.name}.png`,
  })
})

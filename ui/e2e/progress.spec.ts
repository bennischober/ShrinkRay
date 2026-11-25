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
  for (const target of PAGES) {
    await page.goto(`/?fixture=busy${target.hash}`, { waitUntil: 'load' })
    const summary = page.locator('[data-work-summary]').first()
    await expect(summary, `${target.name} has no running-work summary`).toBeVisible({
      timeout: 60_000,
    })
    await expect(summary).toContainText('Encode')
    summaries[target.name] = (await summary.textContent())?.trim() ?? ''
    await page.screenshot({
      path: `test-results/screenshots/progress-${target.name}-${testInfo.project.name}.png`,
    })
  }

  // same facts, same words: the one model feeds every page
  const unique = new Set(Object.values(summaries))
  expect(unique.size, `the pages disagree: ${JSON.stringify(summaries)}`).toBe(1)
  expect(Object.values(summaries)[0]).toContain('88.0%')
  expect(Object.values(summaries)[0]).toContain('12.3 fps')

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

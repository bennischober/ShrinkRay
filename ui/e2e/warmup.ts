import { chromium, type FullConfig } from '@playwright/test'

/** Compiles every page once before the measured run (issue #628).
 *
 * The dev server compiles a page on its first request, and that first request
 * is measurably slower — the same commit reported 28 failures on a cold server
 * and 14 on a warm one. Warming it here makes the verdict a property of the
 * code, not of the order the checks happen to run in. */
export default async function globalSetup(config: FullConfig): Promise<void> {
  // a plain browser has no baseURL: take it from the first project
  const baseURL = config.projects[0]?.use.baseURL ?? 'http://127.0.0.1:4173'
  const browser = await chromium.launch()
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } })
  for (const route of ['', '#/library', '#/jobs', '#/retained', '#/config']) {
    await page.goto(`${baseURL}/?fixture=busy${route}`, { waitUntil: 'networkidle' })
    await page.evaluate(() => document.fonts.ready)
  }
  await browser.close()
}

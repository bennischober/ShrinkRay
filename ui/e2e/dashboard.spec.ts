import AxeBuilder from '@axe-core/playwright'
import { expect, test, type Page } from '@playwright/test'
import knownFailures from './known-failures.json' with { type: 'json' }

/** Cases that fail today and belong to a page issue (#632-#635). The list is
 * in the repository, not in CI settings, so it is reviewable and can only
 * shrink: a case that starts passing fails the run and must be removed. */
const KNOWN: Record<string, string> = knownFailures

/** Every page, against every scenario, at both configured widths (issue #628).
 * A page that scrolls sideways on a phone, that draws text over other text, or
 * that logs an error is a failure here — those are the defects happy-dom
 * cannot see. */

const PAGES = [
  { hash: '', name: 'overview' },
  { hash: '#/library', name: 'library' },
  { hash: '#/jobs', name: 'jobs' },
  { hash: '#/retained', name: 'retained' },
  { hash: '#/config', name: 'config' },
]

const SCENARIOS = ['empty', 'busy', 'nasty-names']

/** The document must not scroll sideways: a page wider than the phone is the
 * defect #628 exists to catch. One pixel of slack absorbs subpixel rounding. */
async function expectNoHorizontalOverflow(page: Page): Promise<void> {
  const overflow = await page.evaluate(
    () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
  )
  expect(overflow, 'the page scrolls sideways').toBeLessThanOrEqual(1)
}

/** No element may draw outside the viewport's right edge. */
async function expectNothingEscapes(page: Page): Promise<void> {
  const escaped = await page.evaluate(() => {
    const out: string[] = []
    for (const el of Array.from(document.querySelectorAll<HTMLElement>('main *'))) {
      const rect = el.getBoundingClientRect()
      if (rect.width === 0 || rect.height === 0) continue
      if (rect.right > window.innerWidth + 1) {
        out.push(`${el.tagName.toLowerCase()}.${el.className} right=${Math.round(rect.right)}`)
      }
    }
    return out.slice(0, 5)
  })
  expect(escaped, `elements draw outside the viewport: ${escaped.join(', ')}`).toHaveLength(0)
}

for (const scenario of SCENARIOS) {
  for (const { hash, name } of PAGES) {
    test(`${name} · ${scenario}`, async ({ page }, testInfo) => {
      const key = `${name} · ${scenario} · ${testInfo.project.name}`
      const known = KNOWN[key]
      const errors: string[] = []
      page.on('console', (message) => {
        if (message.type() === 'error') errors.push(message.text())
      })
      page.on('pageerror', (error) => errors.push(String(error)))

      await page.goto(`/?fixture=${scenario}${hash}`)
      await expect(page.locator('h1').first()).toBeVisible()

      // The check runs whether or not the case is allowlisted: a case that
      // starts passing while it is still in the list is a stale entry and
      // fails the run, so the list can only shrink.
      let failure: string | null = null
      try {
        await expectNoHorizontalOverflow(page)
        await expectNothingEscapes(page)
        expect(errors, `console errors: ${errors.join(' | ')}`).toHaveLength(0)
        const axe = await new AxeBuilder({ page })
          .withTags(['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa'])
          .analyze()
        const serious = axe.violations.filter((v) => v.impact === 'serious' || v.impact === 'critical')
        expect(serious.map((v) => `${v.id}: ${v.help}`), 'serious axe violations').toHaveLength(0)
      } catch (error) {
        failure = error instanceof Error ? error.message.split('\n')[0] : String(error)
      }

      if (known != null) {
        expect(failure, `allowlisted as "${known}" but the page now passes — remove it from known-failures.json`).not.toBeNull()
        testInfo.annotations.push({ type: 'known failure', description: known })
      } else {
        expect(failure, `this case is not in known-failures.json: ${failure}`).toBeNull()
      }

      await page.screenshot({
        path: `test-results/screenshots/${name}-${scenario}-${testInfo.project.name}.png`,
        fullPage: true,
      })
    })
  }
}

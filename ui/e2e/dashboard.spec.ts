import AxeBuilder from '@axe-core/playwright'
import { expect, test, type Page } from '@playwright/test'
import { readFileSync } from 'node:fs'

/** Cases that fail today and belong to a page issue (#632-#635). The list is
 * in the repository, not in CI settings, so it is reviewable and can only
 * shrink: a case that starts passing fails the run and must be removed. */
const KNOWN = JSON.parse(
  readFileSync(new URL('./known-failures.json', import.meta.url), 'utf8'),
) as Record<string, string>

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
    // `main > *` and `main > * > *`, never `main *`: the busy scenario has
    // thousands of rows, and reading a rect for each of them took longer than
    // the test's own 30 s timeout
    for (const el of Array.from(document.querySelectorAll<HTMLElement>('main > *, main > * > *'))) {
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
      // the busy and huge scenarios render thousands of rows, which is more
      // than the 30 s default per test
      test.setTimeout(120_000)
      const key = `${name} · ${scenario} · ${testInfo.project.name}`
      const known = KNOWN[key]
      const errors: string[] = []
      page.on('console', (message) => {
        if (message.type() === 'error') errors.push(message.text())
      })
      page.on('pageerror', (error) => errors.push(String(error)))

      // `load`, never `networkidle`: the event stream stays open like the
      // real one, so `networkidle` never settles and the whole run times out
      console.log(`[${key}] goto`)
      await page.goto(`/?fixture=${scenario}${hash}`, { waitUntil: 'load' })
      // the viewport must be the project's width before anything is measured:
      // the page renders its desktop sidebar whenever the emulated width has not
      // applied yet, which is why the same phone case passed and failed
      // a scrollbar makes the viewport a few pixels narrower than the
      // emulated width, so an exact match never settles and the gate hung
      console.log(`[${key}] viewport`)
      await page.waitForFunction(
        (width) => Math.abs(window.innerWidth - width) <= 20,
        testInfo.project.use.viewport?.width,
      )

      await expect(page.locator('h1').first()).toBeVisible()
      // measure after the fonts settle: a swap changes text widths, which is
      // what made two runs of the same commit disagree about the same page
      console.log(`[${key}] fonts`)
      await page.evaluate(() => document.fonts.ready)

      // the picture is taken before the assertions: a failing case must leave
      // an artifact too, and this one is the evidence for its page issue
      await page.screenshot({
        path: `test-results/screenshots/${name}-${scenario}-${testInfo.project.name}.png`,
        // never `fullPage` for `busy` or `huge`: the busy library page is
        // 68 000 px tall, and the screenshot alone outlasted the test budget
        fullPage: scenario === 'empty' || scenario === 'nasty-names',
      })

      // The check runs whether or not the case is allowlisted: a case that
      // starts passing while it is still in the list is a stale entry and
      // fails the run, so the list can only shrink.
      console.log(`[${key}] assertions`)
      let failure: string | null = null
      try {
        await expectNoHorizontalOverflow(page)
        await expectNothingEscapes(page)
        expect(errors, `console errors: ${errors.join(' | ')}`).toHaveLength(0)
        // axe walks every node and does not return on the busy fixture: every
        // page stays mounted, so one view holds the others (~31 000 nodes), and
        // the library's own 2 000 rows do not finish within the test budget.
        // The checks above already measured the full document. The scan keeps
        // the shell and the first rows of each repeated list, which is the
        // template a later row is another copy of.
        await page.evaluate(() => {
          document.querySelectorAll('.pages.hidden').forEach((el) => el.remove())
          document.querySelectorAll('tbody tr:nth-child(n+3)').forEach((el) => el.remove())
          document.querySelectorAll('.log > :nth-child(n+5)').forEach((el) => el.remove())
        })
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

    })
  }
}

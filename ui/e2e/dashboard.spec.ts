import AxeBuilder from '@axe-core/playwright'
import { expect, test, type Page } from '@playwright/test'
import { readFileSync } from 'node:fs'

/** Cases that fail today and belong to a page issue (#632-#635). The list is
 * in the repository, not in CI settings, so it is reviewable and can only
 * shrink: a case that starts passing fails the run and must be removed. Each
 * entry names the failure it allowlists (`expected`) and how to reproduce it
 * (`repro`) — a case that fails for a DIFFERENT reason is not allowlisted. */
const KNOWN = JSON.parse(
  readFileSync(new URL('./known-failures.json', import.meta.url), 'utf8'),
) as Record<string, { expected: string; repro: string }>

/** Every page, against every scenario, at every configured width, in both
 * themes (issue #628). A page that scrolls sideways on a phone, that draws
 * text over other text, or that logs an error is a failure here — those are
 * the defects happy-dom cannot see. */

const PAGES = [
  { hash: '', name: 'overview' },
  { hash: '#/library', name: 'library' },
  { hash: '#/jobs', name: 'jobs' },
  { hash: '#/retained', name: 'retained' },
  { hash: '#/config', name: 'config' },
]

/** The title page opens a library title's detail; it is part of the matrix
 * only where the scenario has a library to open a title from (the deep link
 * resolves the movie from the snapshot's library by exact path), and its hash
 * carries THAT scenario's own title. */
const SCENARIOS = [
  { name: 'empty', title: null, ready: 'page' as const },
  {
    name: 'busy',
    title: '/media/Filme/Titel 0 (2000).mkv',
    ready: 'page' as const,
  },
  {
    name: 'nasty-names',
    title:
      '/media/Filme/Lang/Ein sehr langer Filmtitel mit Untertitel und Jahreszahl (2019) [EN+FR+DE+ES+IT+JA+KO+ZH] 2160p UHD BluRay x265 10bit HDR DTS-HD MA 7.1-GRP.mkv',
    ready: 'page' as const,
  },
  {
    name: 'failures',
    title: '/media/Filme/Da/Anderer Titel (2021) 2160p WEB-DL.mkv',
    ready: 'page' as const,
  },
  // the API answers 503: the shell never gets a snapshot and stays in its
  // loading state — that state IS the scenario's expected rendering
  { name: 'offline', title: null, ready: 'loading' as const },
  // the API answers 401: the shell shows the token prompt instead of a page
  { name: 'locked', title: null, ready: 'locked' as const },
]

const THEMES = ['dark', 'light']

/** The document must not scroll sideways: a page wider than the phone is the
 * defect #628 exists to catch. One pixel of slack absorbs subpixel rounding. */
async function expectNoHorizontalOverflow(page: Page): Promise<void> {
  const overflow = await page.evaluate(
    () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
  )
  // a thrown message, not expect(): the known-failures allowlist matches the
  // failure string, so the width the defect measures must be part of it
  if (overflow > 1) throw new Error(`the page scrolls sideways: ${overflow} px over`)
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
      // clientWidth, not innerWidth: under mobile emulation innerWidth follows
      // an expanding layout viewport (see the viewport gate)
      if (rect.right > document.documentElement.clientWidth + 1) {
        out.push(`${el.tagName.toLowerCase()}.${el.className} right=${Math.round(rect.right)}`)
      }
    }
    return out.slice(0, 5)
  })
  expect(escaped, `elements draw outside the viewport: ${escaped.join(', ')}`).toHaveLength(0)
}

/** No two boxes in a table row or a card row may overlap (issue #628): the
 * defect this check exists for is text drawn over neighbouring text — the
 * Jobs page's row #4, whose filename wrapped out of its narrow KIND column
 * into the start-time column (#632's fourth bullet). That cause is fixed in
 * the page's own file cell (#632), and no current fixture reproduces an
 * overlap on Jobs (measured 2026-11-23: every jobs case passes, including
 * nasty-names rows carrying a 200-character name and Windows-style paths) —
 * the check stays as the class's regression gate. One pixel of slack absorbs
 * subpixel rounding; merely touching edges are not an overlap. The finding
 * list is the shared seam: the matrix gate asserts it empty, and the
 * synthetic probe test (below) asserts it names the mock's labeled pair. */
async function findOverlappingBoxes(page: Page): Promise<string[]> {
  return page.evaluate(() => {
    const out: string[] = []
    const overlaps = (a: DOMRect, b: DOMRect) =>
      Math.min(a.right, b.right) - Math.max(a.left, b.left) > 1 &&
      Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top) > 1
    // a row's own children: table cells (`tr`) and the card-like rows
    // (the jobs page's `.row`, the overview's `.fail-row`, `.plan-row`,
    // `.bar-row` and `.stat`)
    const rows = document.querySelectorAll<HTMLElement>(
      'main tr, main .entry .row, main .fail-row, main .plan-row, main .bar-row, main .stat',
    )
    for (const row of rows) {
      const rects = Array.from(row.children)
        .map((el) => el.getBoundingClientRect())
        .filter((r) => r.width > 0 && r.height > 0)
      for (let i = 0; i < rects.length; i += 1) {
        for (let j = i + 1; j < rects.length; j += 1) {
          if (overlaps(rects[i], rects[j])) {
            out.push(`${row.className || row.tagName.toLowerCase()}: child ${i} over child ${j}`)
            if (out.length >= 5) return out
          }
        }
      }
    }
    return out
  })
}

/** The matrix's gate on the finding above: zero overlapping pairs. */
async function expectNoOverlappingText(page: Page): Promise<void> {
  const overlapping = await findOverlappingBoxes(page)
  expect(overlapping, `overlapping text boxes: ${overlapping.join(', ')}`).toHaveLength(0)
}

for (const scenario of SCENARIOS) {
  const pages = scenario.title
    ? [...PAGES, { hash: `#/movie/${encodeURIComponent(scenario.title)}`, name: 'title' }]
    : PAGES
  for (const { hash, name } of pages) {
    for (const theme of THEMES) {
      test(`${name} · ${scenario.name} · ${theme}`, async ({ page }, testInfo) => {
        // the busy and huge scenarios render thousands of rows, which is more
        // than the 30 s default per test
        test.setTimeout(120_000)
        const key = `${name} · ${scenario.name} · ${testInfo.project.name} · ${theme}`
        const known = KNOWN[key]
        const errors: string[] = []
        page.on('console', (message) => {
          if (message.type() !== 'error') return
          // offline and locked ARE the scenario: the browser logs the failed
          // resource load (503/401) that the app then reports in its UI. That
          // expected log is not a defect; any other console error is.
          const expectedStatus = scenario.name === 'offline' ? '503' : scenario.name === 'locked' ? '401' : null
          if (expectedStatus !== null && message.text().includes(`status of ${expectedStatus}`)) return
          // the request URL rides along, so a leaked status code names the
          // route it hit
          errors.push(`${message.text()} (${message.location()?.url ?? 'no url'})`)
        })
        page.on('pageerror', (error) => errors.push(String(error)))

        // the theme rides in localStorage before the app boots: App.svelte
        // reads `ms.theme` once on startup, so a later toggle would not be
        // the page's own first render
        await page.addInitScript((t) => localStorage.setItem('ms.theme', t), theme)

        // `load`, never `networkidle`: the event stream stays open like the
        // real one, so `networkidle` never settles and the whole run times out
        console.log(`[${key}] goto`)
        await page.goto(`/?fixture=${scenario.name}${hash}`, { waitUntil: 'load' })
        // the viewport must be the project's width before anything is measured:
        // the page renders its desktop sidebar whenever the emulated width has not
        // applied yet, which is why the same phone case passed and failed
        // a scrollbar makes the viewport a few pixels narrower than the
        // emulated width, so an exact match never settles and the gate hung
        console.log(`[${key}] viewport`)
        // `clientWidth`, never `innerWidth`: under mobile emulation a page
        // that overflows horizontally EXPANDS the layout viewport
        // (innerWidth follows the content — measured 474 on a 390 px phone),
        // so an innerWidth gate never settles exactly when the defect this
        // check exists for is present. clientWidth stays pinned to the
        // emulated width; a scrollbar makes it a few pixels narrower, so the
        // match keeps its slack.
        const projectWidth = testInfo.project.use.viewport?.width ?? 0
        await page.waitForFunction(
          (width) => width > 0 && Math.abs(document.documentElement.clientWidth - width) <= 20,
          projectWidth,
        )

        // measure after the fonts settle: a swap changes text widths, which is
        // what made two runs of the same commit disagree about the same page
        console.log(`[${key}] fonts`)
        await page.evaluate(() => document.fonts.ready)

        // the picture is taken before the assertions: a failing case must leave
        // an artifact too, and this one is the evidence for its page issue
        await page.screenshot({
          path: `test-results/screenshots/${name}-${scenario.name}-${theme}-${testInfo.project.name}.png`,
          // never `fullPage` for `busy` or `huge`: the busy library page is
          // 68 000 px tall, and the screenshot alone outlasted the test budget
          fullPage: scenario.name === 'empty' || scenario.name === 'nasty-names' || scenario.name === 'failures',
        })

        // The check runs whether or not the case is allowlisted: a case that
        // starts passing while it is still in the list is a stale entry and
        // fails the run, so the list can only shrink.
        console.log(`[${key}] assertions`)
        let failure: string | null = null
        try {
          // the shell's readiness depends on the scenario: a page needs its
          // snapshot (its own heading), offline never gets one (loading), and
          // locked renders the token prompt instead of a page. `:visible`
          // matters: every page stays mounted, so the DOM's FIRST h1 is the
          // hidden overview's — the current page's heading is the visible one.
          // A readiness failure (e.g. a heading collapsed to zero width at
          // 320 px) is a defect like any other and lands in `failure`, so it
          // can be allowlisted like the rest.
          if (scenario.ready === 'page') {
            if (name === 'title') {
              // the detail page has no PageHeader h1; its hero names the movie
              await expect(page.locator('main .hero h2')).toBeVisible()
            } else {
              // the brand h1 lives in the rail and is hidden at phone widths by
              // design; the page's own h1 is what must be visible everywhere
              await expect(page.locator('main h1:visible').first()).toBeVisible()
            }
          } else if (scenario.ready === 'loading') {
            await expect(page.locator('main .loading')).toContainText('Loading')
          } else {
            await expect(page.locator('main form h2')).toContainText('token')
          }
          await expectNoHorizontalOverflow(page)
          await expectNothingEscapes(page)
          await expectNoOverlappingText(page)
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
          // a thrown message, not expect(): the allowlist matches the failure
          // string, so the violation ids the page actually has must be part of it
          if (serious.length > 0) {
            throw new Error(`serious axe violations: ${serious.map((v) => v.id).join(', ')}`)
          }
        } catch (error) {
          // Playwright's expect errors carry ANSI styling; the list reporter
          // strips it, `includes` does not — so the allowlist would never
          // match a styled message. Strip it before the first line is taken.
          const message = error instanceof Error ? error.message.replace(/\u001B\[[0-9;]*[A-Za-z]/g, '') : String(error)
          failure = message.split('\n')[0].trim()
        }

        if (known != null) {
          expect(
            failure,
            `allowlisted as "${known.expected}" but the page now passes — remove it from known-failures.json`,
          ).not.toBeNull()
          // a manual compare, not expect().toContain(): a mismatch must quote
          // BOTH strings, or an invisible-character mismatch is undebuggable
          if (!(failure ?? '').includes(known.expected)) {
            throw new Error(
              `the failure changed — the entry allowlists ${JSON.stringify(known.expected)} (${known.repro}) but the page failed with ${JSON.stringify(failure)}`,
            )
          }
          testInfo.annotations.push({ type: 'known failure', description: known.expected })
        } else {
          expect(failure, `this case is not in known-failures.json: ${failure}`).toBeNull()
        }

      })
    }
  }
}

/** The overlap check's firing proof (issue #628, review round 5 B3): the real
 * Jobs row #4 overlap is NOT reproducible on any fixture — #632 fixed its
 * cause before this harness existed — so the check's sensitivity is proven
 * synthetically, in the shape of a mutation test. The mock's `overlap-probe`
 * fixture renders a clearly-labeled two-box overlap over a Jobs row (see
 * fixtureMock.ts), and this test EXPECTS the overlap finding, naming the
 * intersecting boxes: it passes while the check works and fails the moment
 * the check is removed or blinded. A real page whose own layout overlaps
 * fails a matrix run with the same finding. This is deliberately NOT in
 * known-failures.json: the allowlist is for real page defects only. */
test('the overlap check catches an overlap (synthetic probe)', async ({ page }) => {
  test.setTimeout(60_000)
  await page.addInitScript((t) => localStorage.setItem('ms.theme', t), 'dark')
  await page.goto('/?fixture=overlap-probe#/jobs', { waitUntil: 'load' })
  // the mock injects the labeled pair once a Jobs row has rendered; the wait
  // IS the proof the overlay is the mock's work, not this test's
  await expect(page.locator('.synthetic-overlap-probe')).toHaveCount(2)
  await page.evaluate(() => document.fonts.ready)
  // the evidence artifact: the two labeled boxes visible over the row
  await page.screenshot({ path: 'test-results/screenshots/overlap-probe-synthetic.png' })

  // the labeled pair must actually intersect before the check is judged
  const probe = await page.evaluate(() => {
    const row = document.querySelector<HTMLElement>('main .entry .row')
    if (row == null) throw new Error('no Jobs row rendered')
    const children = Array.from(row.children)
    const index = (cls: string) => children.findIndex((el) => el.classList.contains(cls))
    const a = index('overlap-probe-a')
    const b = index('overlap-probe-b')
    const ra = children[a].getBoundingClientRect()
    const rb = children[b].getBoundingClientRect()
    return {
      a,
      b,
      rowClass: row.className,
      x: Math.min(ra.right, rb.right) - Math.max(ra.left, rb.left),
      y: Math.min(ra.bottom, rb.bottom) - Math.max(ra.top, rb.top),
    }
  })
  expect(probe.x, 'the synthetic boxes do not intersect — the probe fixture is broken').toBeGreaterThan(1)
  expect(probe.y, 'the synthetic boxes do not intersect — the probe fixture is broken').toBeGreaterThan(1)

  // the finding, the way a real page's overlap would be named. The overlay
  // covers the row's own cells too (that is what "drawn over the row" means),
  // so the check reports every pair a labeled box takes part in — the proof
  // is that (1) EVERY finding involves one of the two labeled boxes (the
  // page's own layout contributes none) and (2) the labeled pair itself is
  // among them, named with the row and the two child indexes.
  const findings = await findOverlappingBoxes(page)
  const synthetic = new Set([probe.a, probe.b])
  const otherFindings = findings.filter((f) => {
    const m = /child (\d+) over child (\d+)$/.exec(f)
    return m == null || ![Number(m[1]), Number(m[2])].some((i) => synthetic.has(i))
  })
  expect(
    otherFindings,
    `the check reported overlaps the synthetic overlay does not explain: ${otherFindings.join(', ')}`,
  ).toHaveLength(0)
  expect(
    findings,
    `the overlap check missed the synthetic overlap (findings: ${findings.join(', ')})`,
  ).toContain(`${probe.rowClass}: child ${probe.a} over child ${probe.b}`)
})

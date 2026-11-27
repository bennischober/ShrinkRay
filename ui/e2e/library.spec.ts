import AxeBuilder from '@axe-core/playwright'
import { expect, test } from '@playwright/test'

/** Issue #698: the library's phone cards must keep the table structure the
 * desktop table had. The roles live on divs, so the accessibility tree does
 * not depend on the display mode that turns rows into cards below 768px. The
 * issue's acceptance also names the document's scrollWidth/innerWidth bound at
 * 320, 390 and 1440 px; clientWidth is measured beside it because a phone
 * layout viewport expands with the overflowing content. */
const WIDTHS = [
  { width: 320, height: 700 },
  { width: 390, height: 844 },
  { width: 1440, height: 900 },
]

for (const { width, height } of WIDTHS) {
  test(`library keeps its table roles and width at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height })
    await page.goto('/?fixture=nasty-names#/library', { waitUntil: 'load' })
    await expect(page.locator('main h1:visible').first()).toBeVisible()
    await page.evaluate(() => document.fonts.ready)

    // The role structure is present at this width, not just in the desktop DOM.
    const table = page.getByRole('table', { name: /library catalog/i })
    await expect(table).toBeVisible()
    await expect(table.getByRole('rowgroup')).toHaveCount(2)
    await expect(table.getByRole('row')).toHaveCount(13)
    await expect(table.getByRole('columnheader')).toHaveCount(8)
    await expect(table.getByRole('cell')).toHaveCount(96)

    const measured = await page.evaluate(() => ({
      scrollWidth: document.documentElement.scrollWidth,
      innerWidth: window.innerWidth,
      clientWidth: document.documentElement.clientWidth,
    }))
    expect(
      measured.scrollWidth,
      `scrollWidth ${measured.scrollWidth} exceeds innerWidth ${measured.innerWidth} by more than 1 px`,
    ).toBeLessThanOrEqual(measured.innerWidth + 1)
    expect(
      measured.scrollWidth,
      `scrollWidth ${measured.scrollWidth} exceeds clientWidth ${measured.clientWidth} by more than 1 px`,
    ).toBeLessThanOrEqual(measured.clientWidth + 1)

    const axe = await new AxeBuilder({ page })
      .withTags(['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa'])
      .analyze()
    const serious = axe.violations.filter(
      (violation) => violation.impact === 'serious' || violation.impact === 'critical',
    )
    expect(
      serious,
      `serious axe violations: ${serious.map((violation) => violation.id).join(', ')}`,
    ).toHaveLength(0)
  })
}

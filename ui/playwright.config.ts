import { defineConfig, devices } from '@playwright/test'

/** The dashboard's browser checks (issue #628): every page at a desktop and a
 * phone width, against a fixture, so a layout defect is caught before a user
 * sees it. Screenshots and traces are CI artifacts, never committed. */
export default defineConfig({
  testDir: './e2e',
  globalSetup: './e2e/warmup.ts',
  // one worker: the checks share one dev server, and a parallel run made the
  // same commit report different failure sets (measured 29/1 and 14/16)
  fullyParallel: false,
  workers: 1,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  reporter: process.env.CI ? [['list'], ['html', { open: 'never' }]] : 'list',
  use: {
    baseURL: 'http://127.0.0.1:4173',
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
  },
  projects: [
    { name: 'desktop', use: { ...devices['Desktop Chrome'], viewport: { width: 1440, height: 900 } } },
    { name: 'phone', use: { ...devices['Pixel 7'], viewport: { width: 390, height: 844 } } },
  ],
  webServer: {
    // the fixture mock is a dev-server plugin, so the checks run against `dev`
    // bind to IPv4 explicitly: a runner's Vite listens on ::1 while the check
    // polls 127.0.0.1, so the server never answers and the step times out
    command: 'npm run dev -- --port 4173 --host 127.0.0.1',
    url: 'http://127.0.0.1:4173',
    // never reuse: a server left over from the previous run carries a stale
    // module graph, and the same commit then reported 14 and 28 failures
    reuseExistingServer: false,
    // a cold `npm run dev` on a CI runner compiles every page on its first
    // request and needs longer than the 60 s default
    timeout: 180_000,
  },
})

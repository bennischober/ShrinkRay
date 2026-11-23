import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import type { Plugin } from 'vite'
import { busy, huge } from './fixtures/generated.js'
import { FIXTURES, type Fixture } from './src/lib/fixtures.js'

/** Which fixture key answers which GET route. */
const ROUTES: Record<string, keyof Fixture> = {
  '/api/status': 'status',
  '/api/library': 'library',
  '/api/jobs': 'jobs',
  '/api/retained': 'retained',
  '/api/config': 'config',
  '/api/movie': 'movie',
}

/** Scenarios that answer a status code instead of fixture data (issue #628):
 * `offline` is the app still starting (503 on every route), `locked` the
 * missing or refused token (401). The status code IS the scenario; the app
 * renders its stale notice / token prompt against it. */
const STATUS_SCENARIOS: Record<string, number> = {
  offline: 503,
  locked: 401,
}

/** The synthetic overlap probe's overlay (issue #628, review round 5 B3).
 * The real Jobs row #4 overlap is not reproducible on any fixture — #632
 * fixed its cause (the narrow KIND column) before this harness existed — so
 * the overlap check's sensitivity is proven on a deliberately-defective page
 * instead: for THIS fixture name only, the mock's page carries a script that
 * draws two clearly-labeled, absolutely-positioned text boxes over a Jobs
 * row's cells. The e2e probe test expects the overlap check to report exactly
 * that pair, the way a real page's own overlap would be reported; the check
 * itself and the allowlist are untouched. The mock is `apply: 'serve'`, so a
 * production build never carries any of this. */
const OVERLAP_PROBE_FIXTURE = 'overlap-probe'

const OVERLAY_SCRIPT = `
// SYNTHETIC overlap probe (issue #628, review round 5 B3) — dev fixture only.
// Two labeled boxes over a Jobs row give the e2e overlap check a defect to
// catch on a page whose real layout has none. Not product UI, not in the
// known-failures allowlist.
;(function () {
  var tries = 0
  var timer = setInterval(function () {
    tries += 1
    // the Jobs page renders after the snapshot fetch; take the first VISIBLE
    // row (every page stays mounted, hidden ones measure zero)
    var rows = document.querySelectorAll('main .entry .row')
    var row = null
    for (var i = 0; i < rows.length; i += 1) {
      if (rows[i].getBoundingClientRect().width > 0) { row = rows[i]; break }
    }
    if (row === null) {
      if (tries > 100) clearInterval(timer)
      return
    }
    clearInterval(timer)
    row.style.position = 'relative'
    var boxes = [
      ['overlap-probe-a', 'SYNTHETIC OVERLAP PROBE A', '8px'],
      ['overlap-probe-b', 'SYNTHETIC OVERLAP PROBE B', '56px'],
    ]
    for (var j = 0; j < boxes.length; j += 1) {
      var el = document.createElement('span')
      el.className = 'synthetic-overlap-probe ' + boxes[j][0]
      el.textContent = boxes[j][1]
      // loud, hand-set styling on purpose: the overlay is test scaffolding
      // that must be unmistakable in a screenshot, never mistaken for the
      // page's design
      el.style.cssText =
        'position:absolute;top:0;left:' + boxes[j][2] +
        ';width:120px;height:22px;z-index:9;background:#f2c94c;color:#111;' +
        'font:600 9px/22px monospace;text-align:center;white-space:nowrap;overflow:hidden;'
      row.appendChild(el)
    }
  }, 50)
})()
`

/** Dev-only mock of the dashboard's API, so a page renders against a
 * fixture with no resident process (issue #628). `?fixture=<name>` on any
 * request selects the scenario and it rides a cookie, so it sticks for the
 * BROWSER SESSION: two sessions can look at two scenarios at once, and a
 * server-global answer mixed one session's scenario into another's
 * (measured: a nasty-names page logged the offline scenario's 503s). Every
 * route is then answered from `ui/fixtures/<name>.json`.
 *
 * `apply: 'serve'` keeps this out of a production build: `dist/` never
 * contains it. */
export function fixtureMock(dir = resolve(process.cwd(), 'fixtures')): Plugin {
  const cache = new Map<string, Fixture>()
  /** fallback for cookieless clients (curl): the last `?fixture=` seen */
  let scenario: string | null = null

  function load(name: string): Fixture {
    const cached = cache.get(name)
    if (cached) return cached
    // the scale scenarios are generated (20 000 titles is megabytes of
    // invented JSON for no extra coverage); the small ones are files
    const generated = name === 'busy' ? busy : name === 'huge' ? huge : null
    const fixture = generated
      ? generated()
      : (JSON.parse(readFileSync(resolve(dir, `${name}.json`), 'utf8')) as Fixture)
    cache.set(name, fixture)
    return fixture
  }

  return {
    name: 'shrinkray-fixture-mock',
    apply: 'serve',
    transformIndexHtml(html, ctx) {
      // the overlay rides the served page, so the probe's DOM is the mock's
      // work, not the test's; only the probe fixture's page carries it
      const fixture = new URL(ctx.originalUrl ?? '/', 'http://localhost').searchParams.get('fixture')
      if (fixture !== OVERLAP_PROBE_FIXTURE) return html
      return { html, tags: [{ tag: 'script', children: OVERLAY_SCRIPT, injectTo: 'head' }] }
    },
    configureServer(server) {
      server.middlewares.use((req, res, next) => {
        const url = new URL(req.url ?? '/', 'http://localhost')
        const requested = url.searchParams.get('fixture')
        if (requested !== null) {
          // invariant 3: the name is untrusted input. Only a known scenario
          // name is accepted — anything else (a typo, a `../` traversal)
          // never reaches the filesystem.
          if (!(FIXTURES as readonly string[]).includes(requested)) {
            res.statusCode = 400
            res.end(`unknown fixture: ${requested}`)
            return
          }
          scenario = requested
          res.setHeader('Set-Cookie', `shrinkray-fixture=${requested}; Path=/; SameSite=Lax`)
        }
        if (!url.pathname.startsWith('/api/')) return next()
        // the browser's cookie wins (it is the session that asked); the
        // sticky variable is the fallback for clients that send no cookie.
        // A cookie value the server never set (hand-edited, stale) is
        // untrusted input like the query parameter: ignored, not read from
        // the filesystem.
        const cookie = /(?:^|;\s*)shrinkray-fixture=([^;\s]+)/.exec(req.headers.cookie ?? '')
        const active =
          cookie && (FIXTURES as readonly string[]).includes(cookie[1]) ? cookie[1] : scenario
        if (active == null) return next()

        const forced = STATUS_SCENARIOS[active]
        if (forced !== undefined) {
          res.statusCode = forced
          res.setHeader('Content-Type', 'application/json')
          res.end(JSON.stringify({
            detail: forced === 503 ? 'the app is still starting' : 'unauthorized',
          }))
          return
        }

        let fixture: Fixture
        try {
          fixture = load(active)
        } catch (error) {
          res.statusCode = 500
          res.end(`fixture ${active} is unreadable: ${String(error)}`)
          return
        }

        res.setHeader('Content-Type', 'application/json')
        res.setHeader('ETag', `"fixture-${active}"`)
        if (url.pathname === '/api/snapshot') {
          res.end(JSON.stringify({
            status: fixture.status,
            jobs: fixture.jobs,
            jobs_etag: `fixture-${active}`,
            library: fixture.library,
            library_etag: `fixture-${active}`,
          }))
          return
        }
        if (url.pathname === '/api/events') {
          // An event stream stays open, like the real one: ending it makes the
          // client reconnect, and whether that reconnect logs an error depends
          // on timing — which made one case's verdict change between runs.
          res.setHeader('Content-Type', 'text/event-stream')
          res.setHeader('Cache-Control', 'no-store')
          res.setHeader('Connection', 'keep-alive')
          res.write(fixture.events.map((event: unknown) => `data: ${JSON.stringify(event)}\n\n`).join(''))
          req.on('close', () => res.end())
          return
        }
        const jobMatch = url.pathname.match(/^\/api\/jobs\/(\d+)$/)
        if (jobMatch) {
          // the real route is GET /api/jobs/{job_id} (app.py): one job, or 404
          const found = fixture.jobs.find((j) => String(j.id) === jobMatch[1])
          if (!found) {
            res.statusCode = 404
            res.end(JSON.stringify({ detail: 'job not found' }))
            return
          }
          res.end(JSON.stringify(found))
          return
        }
        const key = ROUTES[url.pathname]
        if (!key) return next()
        res.end(JSON.stringify(fixture[key]))
      })
    },
  }
}

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

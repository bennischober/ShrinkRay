import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import type { Plugin } from 'vite'
import type { Fixture } from './src/lib/fixtures.js'

/** Which fixture key answers which GET route. */
const ROUTES: Record<string, keyof Fixture> = {
  '/api/status': 'status',
  '/api/library': 'library',
  '/api/jobs': 'jobs',
  '/api/retained': 'retained',
  '/api/config': 'config',
  '/api/movie': 'movie',
  '/api/job': 'job',
}

/** Dev-only mock of the dashboard's API, so a page renders against a
 * fixture with no resident process (issue #628). `?fixture=<name>` on any
 * request selects the scenario and sticks for the session; every route is then
 * answered from `ui/fixtures/<name>.json`.
 *
 * `apply: 'serve'` keeps this out of a production build: `dist/` never
 * contains it. */
export function fixtureMock(dir = resolve(process.cwd(), 'fixtures')): Plugin {
  const cache = new Map<string, Fixture>()
  let scenario: string | null = null

  function load(name: string): Fixture {
    const cached = cache.get(name)
    if (cached) return cached
    const fixture = JSON.parse(readFileSync(resolve(dir, `${name}.json`), 'utf8')) as Fixture
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
        if (requested) scenario = requested
        if (scenario == null || !url.pathname.startsWith('/api/')) return next()

        let fixture: Fixture
        try {
          fixture = load(scenario)
        } catch (error) {
          res.statusCode = 500
          res.end(`fixture ${scenario} is unreadable: ${String(error)}`)
          return
        }

        res.setHeader('Content-Type', 'application/json')
        res.setHeader('ETag', `"fixture-${scenario}"`)
        if (url.pathname === '/api/snapshot') {
          res.end(JSON.stringify({
            status: fixture.status,
            jobs: fixture.jobs,
            jobs_etag: `fixture-${scenario}`,
            library: fixture.library,
            library_etag: `fixture-${scenario}`,
          }))
          return
        }
        if (url.pathname === '/api/events') {
          res.setHeader('Content-Type', 'text/event-stream')
          res.setHeader('Cache-Control', 'no-store')
          res.end(fixture.events.map((event: unknown) => `data: ${JSON.stringify(event)}\n\n`).join(''))
          return
        }
        const key = ROUTES[url.pathname]
        if (!key) return next()
        res.end(JSON.stringify(fixture[key]))
      })
    },
  }
}

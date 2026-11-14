import emptyJson from '../../fixtures/empty.json' with { type: 'json' }
import type { Config, Job, Movie, MovieDetail, RetainedFile, Status } from './api.js'

/** One scenario a page can be rendered against with no resident process
 * (issue #628): every GET route's body, plus the events the SSE stream
 * would have sent. The `satisfies` check is the contract — a fixture that
 * drifts from `api.ts` fails `npm run check`. */
export interface Fixture {
  status: Status
  library: Movie[]
  jobs: Job[]
  retained: RetainedFile[]
  config: Config
  movie: MovieDetail | null
  job: Job | null
  events: unknown[]
}

/** The scenarios `?fixture=<name>` knows. Adding one is adding its JSON here. */
// A JSON import widens `"running"` to `string`, so the literal unions need an
// assertion; a missing or misspelled field still fails it, which is the point.
const empty = emptyJson as Fixture

export const fixtures: Record<string, Fixture> = { empty }

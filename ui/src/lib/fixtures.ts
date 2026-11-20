import emptyJson from '../../fixtures/empty.json' with { type: 'json' }
import nastyNamesJson from '../../fixtures/nasty-names.json' with { type: 'json' }
import type { Config, Job, Movie, MovieDetail, RetainedFile, Status } from './api.js'
import { busy, huge } from '../../fixtures/generated.js'

/** A scenario a page can be rendered against with no resident process
 * (issue #628): every GET route's body, plus the events the SSE stream would
 * have sent.
 *
 * JSON cannot carry a tuple or a literal union, so exactly two fields are
 * widened here (`resolution` arrives as `number[]`, `watcher` as `string`).
 * Everything else is the API's own type: a field added to `Movie`, `Job` or
 * `Status` without a matching fixture value fails `npm run check`, which is how
 * a fixture cannot drift from the contract. */
export type Fixture = Omit<FixtureBody, 'status' | 'library'> & {
  status: Omit<Status, 'watcher'> & { watcher: string }
  library: (Omit<Movie, 'resolution'> & { resolution: number[] | null })[]
}

type FixtureBody = {
  status: Status
  library: Movie[]
  jobs: Job[]
  retained: RetainedFile[]
  config: Config
  movie: MovieDetail | null
  job: Job | null
  events: unknown[]
}

const empty = emptyJson satisfies Fixture
const nastyNames = nastyNamesJson satisfies Fixture

/** The scenarios `?fixture=<name>` knows. Adding one is adding its JSON here. */
export const fixtures: Record<string, Fixture> = {
  empty,
  'nasty-names': nastyNames,
  busy: busy(),
  huge: huge(),
}

import emptyJson from '../../fixtures/empty.json' with { type: 'json' }
import nastyNamesJson from '../../fixtures/nasty-names.json' with { type: 'json' }
import failuresJson from '../../fixtures/failures.json' with { type: 'json' }
import type { Config, Job, Movie, MovieDetail, RetainedFile, Status } from './api.js'

/** A scenario a page can be rendered against with no resident process
 * (issue #628): every GET route's body, plus the events the SSE stream would
 * have sent.
 *
 * JSON cannot carry a tuple or a literal union, so exactly four fields are
 * widened here (`resolution` arrives as `number[]`, `watcher`/`worker`/
 * `kind`/`state` as `string`). Everything else is the API's own type: a field added to
 * `Movie`, `Job` or `Status` without a matching fixture value fails
 * `npm run check`, which is how a fixture cannot drift from the contract. */
export type Fixture = Omit<FixtureBody, 'status' | 'library' | 'jobs' | 'movie'> & {
  status: Omit<Status, 'watcher' | 'worker'> & { watcher: string; worker?: string }
  library: (Omit<Movie, 'resolution'> & { resolution: number[] | null })[]
  jobs: (Omit<Job, 'kind' | 'state'> & { kind: string; state: string })[]
  movie:
    | null
    | (Omit<MovieDetail, 'file'> & {
        file: Omit<Movie, 'encode' | 'resolution'> & { resolution: number[] | null; generation?: number }
      })
}

type FixtureBody = {
  status: Status
  library: Movie[]
  jobs: Job[]
  retained: RetainedFile[]
  config: Config
  movie: MovieDetail | null
  events: unknown[]
}

// The hand-written fixtures, checked against the contract above. Type-only:
// the dev mock reads the JSON files itself, so these statements are the
// drift check the DoD asks for, not a runtime map.
emptyJson satisfies Fixture
nastyNamesJson satisfies Fixture
failuresJson satisfies Fixture

/** The scenarios `?fixture=<name>` knows — the one list the dev mock
 * validates the query parameter against before it touches the filesystem
 * (invariant 3: the name is untrusted input). Adding a scenario is adding
 * its JSON (or generator, or status mapping) plus its name here. */
export const FIXTURES = [
  'empty',
  'nasty-names',
  'failures',
  'busy',
  'huge',
  'offline',
  'locked',
] as const

export type FixtureName = (typeof FIXTURES)[number]

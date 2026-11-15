import { describe, expect, it } from 'vitest'
import { absolute, bitrate, bytes, crf, duration, eta, etaShort, remainingSeconds, groupJobs, isStageRow, kindWord, percent, progressLabel, queueLabel, relative, resultSummary, savedPercent, span, stageWord, stateWord, statusAnswer, timeColumns, titleBuckets, verdictWord, vmaf } from './format'

describe('duration', () => {
  it('renders the unit boundaries', () => {
    expect(duration(0)).toBe('0 s')
    expect(duration(59)).toBe('59 s')
    expect(duration(60)).toBe('1 min')
    expect(duration(3599)).toBe('59 min')
    expect(duration(3600)).toBe('1 h')
    expect(duration(86399)).toBe('23 h 59 min')
    expect(duration(86400)).toBe('1 d')
    expect(duration(86400 + 4 * 3600)).toBe('1 d 4 h')
  })

  it('drops the empty trailing unit', () => {
    expect(duration(7200)).toBe('2 h')
    expect(duration(2 * 86400)).toBe('2 d')
  })

  it('is an em dash for missing or negative input', () => {
    expect(duration(null)).toBe('—')
    expect(duration(undefined)).toBe('—')
    expect(duration(-1)).toBe('—')
    expect(duration(Number.NaN)).toBe('—')
  })
})

describe('relative', () => {
  const now = new Date(2026, 10, 14, 12, 0, 0)

  it('renders the recent past', () => {
    expect(relative(new Date(2026, 10, 14, 11, 59, 30).toISOString(), now)).toBe('just now')
    expect(relative(new Date(2026, 10, 14, 11, 56, 0).toISOString(), now)).toBe('4 min ago')
    expect(relative(new Date(2026, 10, 14, 9, 0, 0).toISOString(), now)).toBe('3 h ago')
  })

  it('renders yesterday by calendar day, not by 24 hours', () => {
    expect(relative(new Date(2026, 10, 13, 21, 12, 0).toISOString(), now)).toBe('yesterday 21:12')
  })

  it('renders the days of the last week and then a date', () => {
    expect(relative(new Date(2026, 10, 11, 12, 0, 0).toISOString(), now)).toBe('3 days ago')
    expect(relative(new Date(2026, 10, 1, 12, 0, 0).toISOString(), now)).toBe('1 Nov')
  })

  it('is an em dash for a missing or unparseable stamp', () => {
    expect(relative(null, now)).toBe('—')
    expect(relative('not-a-date', now)).toBe('—')
  })
})

describe('absolute', () => {
  const now = new Date(2026, 10, 14, 12, 0, 0)

  it('omits the year inside the current year', () => {
    expect(absolute(new Date(2026, 10, 13, 19, 40).toISOString(), now)).toBe('13 Nov, 19:40')
  })

  it('keeps the year outside it', () => {
    expect(absolute(new Date(2025, 10, 13, 19, 40).toISOString(), now)).toBe('13 Nov 2025, 19:40')
  })
})

describe('bytes', () => {
  it('labels the 1024 divisor honestly', () => {
    expect(bytes(0)).toBe('—')
    expect(bytes(512)).toBe('512 B')
    expect(bytes(1024)).toBe('1.00 KiB')
    expect(bytes(1024 ** 2)).toBe('1.00 MiB')
    expect(bytes(1024 ** 3 * 1.24)).toBe('1.24 GiB')
    expect(bytes(1024 ** 4)).toBe('1.00 TiB')
  })

  it('is an em dash for missing input', () => {
    expect(bytes(null)).toBe('—')
    expect(bytes(-5)).toBe('—')
  })
})

describe('percent, vmaf, crf, bitrate, eta', () => {
  it('renders percentages with one decimal', () => {
    expect(percent(42.53)).toBe('42.5%')
    expect(percent(null)).toBe('—')
  })

  it('keeps the printed VMAF on its side of the target (issue #599)', () => {
    expect(vmaf(95.96, 96)).toBe('95.9')
    expect(vmaf(96.01, 96)).toBe('96.1')
    expect(vmaf(96.04)).toBe('96.0')
    expect(vmaf(null, 96)).toBe('—')
  })

  it('renders CRF values and an em dash', () => {
    expect(crf(21)).toBe('21')
    expect(crf(null)).toBe('—')
  })

  it('renders bitrates in the larger unit when it helps', () => {
    expect(bitrate(4200)).toBe('4.2 Mbps')
    expect(bitrate(640)).toBe('640 kb/s')
    expect(bitrate(null)).toBe('—')
  })

  it('renders an estimate only when it is known', () => {
    expect(eta(2100)).toBe('about 35 min left')
    expect(eta(null)).toBe('')
    expect(eta(0)).toBe('')
  })
})

describe('span', () => {
  it('renders the elapsed time between two stamps', () => {
    expect(span('2026-11-14T10:00:00Z', '2026-11-14T12:14:00Z')).toBe('2 h 14 min')
    expect(span('2026-11-14T10:00:00Z', '2026-11-14T10:00:30Z')).toBe('30 s')
  })

  it('is an em dash when either stamp is missing', () => {
    expect(span(null, '2026-11-14T12:00:00Z')).toBe('—')
    expect(span('2026-11-14T12:00:00Z', null)).toBe('—')
  })
})

describe('savedPercent', () => {
  it('renders a saving and a growth with its sign', () => {
    expect(savedPercent(64.3)).toEqual({ text: '64.3%', grew: false })
    expect(savedPercent(-5)).toEqual({ text: '+5.0% larger', grew: true })
  })

  it('is an em dash when there is no value', () => {
    expect(savedPercent(null)).toEqual({ text: '—', grew: false })
  })

  it('calls zero zero, not growth (issue #630)', () => {
    expect(savedPercent(0)).toEqual({ text: '0.0%', grew: false })
  })
})

describe('stateWord', () => {
  it('gives every job state its glossary word, in sentence case', () => {
    expect(stateWord('running')).toBe('Running')
    expect(stateWord('queued')).toBe('Queued')
    expect(stateWord('done')).toBe('Done')
    expect(stateWord('partial')).toBe('Partial')
    expect(stateWord('failed')).toBe('Failed')
    expect(stateWord('cancelled')).toBe('Cancelled')
  })

  it('keeps an unknown state visible instead of blanking it', () => {
    expect(stateWord('reconciling')).toBe('reconciling')
  })
})

describe('stageWord', () => {
  // issue #631: a multi-step job must say which step it is on, so a CRF
  // probe cannot look like the final encode
  it('names the step the job is on', () => {
    expect(stageWord('probe')).toBe('probing')
    expect(stageWord('encode')).toBe('encoding')
    expect(stageWord('scan')).toBe('scanning')
    expect(stageWord('paused')).toBe('paused')
  })

  it('shows a stage it has never heard of as itself, not as nothing', () => {
    expect(stageWord('verify')).toBe('verify')
    expect(stageWord(null)).toBe('')
  })
})

describe('progressLabel', () => {
  // issue #631: the bar must say what is happening, not just how far
  it('names the step, the file and what the engine reported', () => {
    expect(progressLabel('probe', 'a.mkv', 'CRF 24: VMAF 96.41', 42)).toBe(
      'probing · a.mkv · CRF 24: VMAF 96.41',
    )
  })

  it('does not say the step twice when the engine names it already', () => {
    // two live emissions do exactly this: "scanning library", "probing a.mkv"
    expect(progressLabel('scan', null, 'scanning library', 10)).toBe('scanning library')
    expect(progressLabel('probe', 'a.mkv', 'probing a.mkv', 20)).toBe('a.mkv · probing a.mkv')
  })

  it('falls back to a state word only when the payload carries nothing else', () => {
    expect(progressLabel(null, null, null, 42)).toBe('working')
    expect(progressLabel(null, null, null, null)).toBe('starting…')
  })
})

describe('groupJobs', () => {
  // issue #632: the page answers three questions, not one id-ordered list
  const j = (id: number | null, state: string) => ({ id, state })

  it('separates what runs now, what comes next and what happened', () => {
    const g = groupJobs([
      j(9, 'running'),
      j(10, 'queued'),
      j(12, 'queued'),
      j(4, 'done'),
      j(5, 'failed'),
    ])
    expect(g.running.map((x) => x.id)).toEqual([9])
    expect(g.queued.map((x) => x.id)).toEqual([10, 12])
    expect(g.history.map((x) => x.id)).toEqual([4, 5])
  })

  it('puts every job in exactly one group, so ordering cannot drop one', () => {
    // the invariant the first version broke: a queued job that is not the
    // oldest vanished from the page
    const input = [j(9, 'running'), j(10, 'queued'), j(12, 'queued'), j(4, 'done'), j(5, 'failed')]
    const g = groupJobs(input)
    const seen = [...g.running, ...g.queued, ...g.history].map((x) => x.id)
    expect(seen.sort((a, b) => (a as number) - (b as number))).toEqual([4, 5, 9, 10, 12])
  })

  it('makes the oldest queued job the next one, whatever the list order', () => {
    const g = groupJobs([j(12, 'queued'), j(10, 'queued'), j(9, 'running')])
    expect(g.next?.id).toBe(10)
  })

  it('has no next job when nothing is queued', () => {
    expect(groupJobs([j(9, 'running'), j(4, 'done')]).next).toBeNull()
  })
})

describe('resultSummary', () => {
  // issue #632: a finished row must say what happened, not make the
  // operator click DETAIL and parse raw JSON
  it('summarises what the job did, with the average saving', () => {
    const s = resultSummary([
      { movie: 'a', state: 'encoded', input_size: 1000, output_size: 600 },
      { movie: 'b', state: 'encoded', input_size: 1000, output_size: 558 },
      { movie: 'c', state: 'skipped', reason: 'no size gain' },
    ])
    expect(s).toContain('2 encoded')
    expect(s).toContain('42.1% size')
    expect(s).toContain('1 skipped')
  })

  it('counts a declined swap as failed, never as a clean skip', () => {
    const s = resultSummary([
      { movie: 'a', state: 'encoded', input_size: 1000, output_size: 900 },
      { movie: 'b', state: 'skipped', error: 'the swap was declined: subtitles' },
    ])
    expect(s).toContain('1 failed')
    expect(s).not.toContain('1 skipped')
  })

  it('names an already-encoded title, which is its own outcome', () => {
    // jobs.py writes state 'already_encoded': without its own bucket such a
    // run rendered no summary at all
    expect(resultSummary([{ movie: 'a', state: 'already_encoded' }])).toBe('1 already encoded')
  })

  it('says nothing when the payload carries no outcomes', () => {
    expect(resultSummary([])).toBe('')
  })
})

describe('timeColumns', () => {
  // issue #632: a row must not print a column it cannot fill. Driven by the
  // timestamps, so a state the UI has not heard of still shows what it has.
  it('gives a queued row neither a start nor a duration', () => {
    expect(timeColumns({ started_at: null, finished_at: null })).toEqual({
      started: false,
      duration: false,
    })
  })

  it('gives a running row a start but no duration yet', () => {
    expect(timeColumns({ started_at: '2026-10-24T10:00:01Z', finished_at: null })).toEqual({
      started: true,
      duration: false,
    })
  })

  it('gives a cancelled-while-running row both, though it is no state of its own', () => {
    // a cancelled job keeps both timestamps; the old state table dropped them
    expect(timeColumns({ started_at: '2026-10-24T10:00:01Z', finished_at: '2026-10-24T10:02:00Z' })).toEqual({
      started: true,
      duration: true,
    })
  })
})

describe('titleBuckets', () => {
  // issue #633: the band's numbers must add up to the title count
  it('puts every title in exactly one bucket, so the four add up', () => {
    const b = titleBuckets([
      { encode: { outcome: 'encoded' } },
      { encode: { outcome: 'discarded' } },
      { encode: { preset: 'skipped (no size gain)' } },
      { encode: null },
      { encode: null },
    ])
    expect(b).toEqual({ encoded: 1, discarded: 1, skipped: 1, untouched: 2, total: 5 })
    expect(b.encoded + b.discarded + b.skipped + b.untouched).toBe(b.total)
  })

  it('counts a skip record as skipped, never as an encode', () => {
    // the band said 1 encoded against 7 titles: the two skips were in no bucket
    expect(titleBuckets([{ encode: { preset: 'skipped (hdr not supported)' } }])).toEqual({
      encoded: 0,
      discarded: 0,
      skipped: 1,
      untouched: 0,
      total: 1,
    })
  })
})

describe('statusAnswer', () => {
  // issue #633: the page must answer "is everything OK?" first, in one line
  it('says what is running, how long is left and what is waiting', () => {
    expect(
      statusAnswer({ running: { movie: 'a.mkv' }, etaSeconds: 6600, queued: 7, watching: true }),
    ).toBe('● Working — encoding a.mkv, about 1 h 50 min left · 7 files waiting')
  })

  it('says it is idle when nothing is running, and whether it is watching', () => {
    expect(statusAnswer({ running: null, etaSeconds: null, queued: 0, watching: true })).toBe(
      '● Idle — all caught up · watching for new files',
    )
    expect(statusAnswer({ running: null, etaSeconds: null, queued: 0, watching: false })).toBe(
      '● Idle — all caught up · the watcher is off',
    )
  })

  it('names one waiting file in the singular', () => {
    expect(
      statusAnswer({ running: { movie: 'a.mkv' }, etaSeconds: null, queued: 1, watching: true }),
    ).toBe('● Working — encoding a.mkv · 1 file waiting')
  })
})

describe('kindWord and verdictWord', () => {
  it('gives every job kind its glossary word', () => {
    expect(kindWord('scan')).toBe('Library scan')
    expect(kindWord('crf-search')).toBe('CRF search')
    expect(kindWord('encode')).toBe('Encode')
    expect(kindWord('legacy')).toBe('Legacy')
  })

  it('gives every plan verdict its screen word and keeps an unknown one visible', () => {
    expect(verdictWord('encode')).toBe('Encode')
    expect(verdictWord('skip')).toBe('Skip')
    expect(verdictWord('failed')).toBe('Failed')
    expect(verdictWord('deferred')).toBe('deferred')
  })
})

describe('queueLabel', () => {
  it('says what the rail count counts, in the right number', () => {
    expect(queueLabel(1)).toBe('1 job in the queue')
    expect(queueLabel(3)).toBe('3 jobs in the queue')
  })
})

describe('remainingSeconds', () => {
  const now = new Date('2026-11-15T12:00:00Z')

  it('scales the elapsed time by the percent done', () => {
    // 30 min elapsed at 25% implies 2 h total, so 90 min left
    expect(remainingSeconds('2026-11-15T11:30:00Z', 25, now)).toBe(90 * 60)
  })

  it('says nothing it cannot estimate', () => {
    expect(remainingSeconds('2026-11-15T11:30:00Z', null, now)).toBeNull()
    expect(remainingSeconds('2026-11-15T11:30:00Z', 0, now)).toBeNull()
    expect(remainingSeconds(null, 40, now)).toBeNull()
  })
})

describe('etaShort', () => {
  it('drops the sentence the column header already carries', () => {
    expect(etaShort(90 * 60)).toBe('~1 h 30 min')
    expect(etaShort(null)).toBe('')
  })
})

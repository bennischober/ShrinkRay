import { describe, expect, it } from 'vitest'
import { absolute, bitrate, bytes, crf, duration, eta, percent, relative, kindWord, savedPercent, span, stateWord, verdictWord, vmaf } from './format'

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
    expect(bitrate(4200)).toBe('4.2 Mb/s')
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

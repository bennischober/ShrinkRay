import { describe, expect, it } from 'vitest'
import { absolute, bitrate, bytes, crf, duration, eta, percent, relative, vmaf } from './format'

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

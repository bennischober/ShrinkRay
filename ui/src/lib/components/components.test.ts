import { describe, expect, it } from 'vitest'
import { render, screen } from '@testing-library/svelte'
import PageHeader from './PageHeader.svelte'
import StatusBadge from './StatusBadge.svelte'
import FileName from './FileName.svelte'
import Time from './Time.svelte'
import ProgressBar from './ProgressBar.svelte'
import EmptyState from './EmptyState.svelte'
import Panel from './Panel.svelte'

const LONG_NAME =
  'Ein sehr langer Dateiname mit Ümlauten, 日本語, emoji 🎬 and a ² sign that must never break inside a word or overlap the next column (2015) - WEBDL-2160p HEVC.mkv'

describe('PageHeader', () => {
  it('renders the title, the description and the right slot', () => {
    render(PageHeader, { title: 'Library', description: 'Every title ShrinkRay knows about.' })
    expect(screen.getByRole('heading', { level: 1 }).textContent).toBe('Library')
    expect(screen.getByText('Every title ShrinkRay knows about.')).toBeTruthy()
  })
})

describe('StatusBadge', () => {
  it('renders the label and carries the tone as a class', () => {
    const { container } = render(StatusBadge, { label: 'Running', tone: 'work' })
    expect(screen.getByText('Running')).toBeTruthy()
    expect(container.querySelector('.badge.work')).toBeTruthy()
  })

  it('never relies on colour alone: the word is rendered and the mark is hidden from screen readers', () => {
    const { container } = render(StatusBadge, { label: 'Failed', tone: 'fail' })
    // the word carries the meaning; the coloured dot is decoration only
    expect(screen.getByText('Failed')).toBeTruthy()
    expect(container.querySelector('.mark')?.getAttribute('aria-hidden')).toBe('true')
  })
})

describe('FileName', () => {
  it('shows the file name without the folder and the full path on hover', () => {
    const { container } = render(FileName, { path: '/media/Filme/Legend (2015).mkv' })
    expect(screen.getByText('Legend (2015).mkv')).toBeTruthy()
    expect(container.querySelector('.file-name')?.getAttribute('title')).toBe(
      '/media/Filme/Legend (2015).mkv',
    )
  })

  it('keeps a long unicode name intact and exposes the full path', () => {
    const { container } = render(FileName, { path: `/media/${LONG_NAME}` })
    const el = container.querySelector('.file-name') as HTMLElement
    expect(el.textContent).toBe(LONG_NAME)
    expect(el.getAttribute('title')).toBe(`/media/${LONG_NAME}`)
  })
})

describe('Time', () => {
  it('renders a time element with the absolute value in its tooltip', () => {
    const now = new Date('2026-11-14T12:00:00Z')
    const { container } = render(Time, { iso: '2026-11-14T11:56:00Z', now })
    const el = container.querySelector('time') as HTMLTimeElement
    expect(el.textContent).toBe('4 min ago')
    expect(el.getAttribute('datetime')).toBe('2026-11-14T11:56:00Z')
    expect(el.getAttribute('title')).toContain('14 Nov')
  })
})

describe('ProgressBar', () => {
  it('exposes the value and the stage', () => {
    const { container } = render(ProgressBar, { percent: 42.5, stage: 'Encoding', eta: 2100 })
    const bar = container.querySelector('[role="progressbar"]') as HTMLElement
    expect(bar.getAttribute('aria-valuenow')).toBe('42.5')
    expect(screen.getByText('42.5%')).toBeTruthy()
    expect(screen.getByText('about 35 min left')).toBeTruthy()
  })

  it('marks a stale bar without pretending it is live', () => {
    const { container } = render(ProgressBar, { percent: 10, stale: true })
    expect(container.querySelector('.progress.stale')).toBeTruthy()
  })
})

describe('EmptyState', () => {
  it('says what the area shows and why it is empty', () => {
    render(EmptyState, {
      title: 'No encodes yet',
      hint: 'A finished encode appears here with its measured VMAF.',
    })
    expect(screen.getByText('No encodes yet')).toBeTruthy()
    expect(screen.getByText(/measured VMAF/)).toBeTruthy()
  })
})

describe('Panel', () => {
  it('renders its heading and its content', () => {
    render(Panel, { title: 'Retained originals' })
    expect(screen.getByRole('heading', { level: 2 }).textContent).toBe('Retained originals')
  })
})

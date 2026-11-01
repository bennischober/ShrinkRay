import { describe, expect, it, vi } from 'vitest'
import { render, screen } from '@testing-library/svelte'
import ConfigPage from './ConfigPage.svelte'
import type { Config } from '../api'

// the page fetches its config itself; the fixture answers without a server
vi.mock('../api', async (importOriginal) => {
  const mod = await importOriginal<typeof import('../api')>()
  return { ...mod, api: { ...mod.api, config: () => Promise.resolve(configFixture) } }
})

/** Issue #239/#236: the config page rendered editable inputs whose values
 * were silently discarded (no save function exists; PUT /api/config was
 * removed with #134) under "applies on SAVE CONFIG" labels. Since #256 the
 * page is read-only per PRODUCT.md — the render test pins the absence of
 * any editable affordance. */

const configFixture = vi.hoisted(() => ({
  codec: { hdr: { color_space: ['bt2020nc'], color_transfer: ['smpte2084'] } },
  file: { extensions: ['.mkv', '.mp4'], output: { separator: ';' } },
  ffmpeg: { black_bars: { MIN_WIDTH: 24, MIN_HEIGHT: 24 }, crop_sample_count: 10 },
  handbrake: { preset: 'Fast 1080p30', encoder: 'svt_av1', encoder_preset: '3', max_threads: 0 },
  resources: { idle_priority: true },
  quality: { target_vmaf: 96, sample_every: 600, sample_count: 3, sample_seconds: 10 },
  eligibility: { min_source_height: 1080, min_source_size_gb: 0, candidate_codecs: ['h264'] },
  language: 'en',
}))

const config: Config = {
  codec: { hdr: { color_space: ['bt2020nc'], color_transfer: ['smpte2084'] } },
  file: { extensions: ['.mkv', '.mp4'], output: { separator: ';' } },
  ffmpeg: { black_bars: { MIN_WIDTH: 24, MIN_HEIGHT: 24 }, crop_sample_count: 10 },
  handbrake: { preset: 'Fast 1080p30', encoder: 'svt_av1', encoder_preset: '3', max_threads: 0 },
  resources: { idle_priority: true },
  quality: { sample_every: 600, sample_count: 3, sample_seconds: 10 },
  eligibility: { min_source_height: 1080, min_source_size_gb: 0, candidate_codecs: ['h264'] },
  language: 'en',
}

describe('ConfigPage render (issue #239)', () => {
  it('renders no editable input anywhere on the page', () => {
    render(ConfigPage, {
      scanPaths: ['/m'],
      outputDir: '/out',
      targetVmaf: 96,
    })
    // the editable-tool-settings form is gone; the page displays values
    expect(screen.queryByText('SAVE CONFIG')).toBeNull()
    expect(screen.queryByText(/applies on SAVE CONFIG/i)).toBeNull()
  })

  it('shows the config values it displays (read-only, honest)', () => {
    render(ConfigPage, {
      scanPaths: ['/m'],
      outputDir: '/out',
      targetVmaf: 96,
    })
    // a value display: the effective settings are visible
    expect(screen.getByText('svt_av1')).toBeTruthy()
    expect(screen.getByText('.mkv')).toBeTruthy()
  })

  it('invites no click on the read-only rows (issue #535)', async () => {
    // the page is a mirror (issue #331): a pointer cursor on a value that
    // cannot change is an affordance that lies. The stylesheet must not
    // declare one on .option-row.
    const css = (await import('./ConfigPage.svelte?raw')).default
    expect(css).not.toMatch(/\.option-row\s*{[^}]*cursor:\s*pointer/s)
  })
})

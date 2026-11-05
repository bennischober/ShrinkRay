import { beforeEach, describe, expect, it, vi } from 'vitest'
import { render, screen } from '@testing-library/svelte'
import ConfigPage from './ConfigPage.svelte'
import type { Config } from '../api'

// the page fetches its config itself; the fixture answers without a server.
// A single hoisted mock, overridable per test (issue #541's banner case).
const apiConfig = vi.hoisted(() => ({ impl: async (): Promise<unknown> => ({}) }))
vi.mock('../api', async (importOriginal) => {
  const mod = await importOriginal<typeof import('../api')>()
  return {
    ...mod,
    api: { ...mod.api, config: () => apiConfig.impl() },
  }
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
  beforeEach(() => {
    apiConfig.impl = async () => configFixture
  })

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
    // declare one on .option-row, nor on any state of it (issue #541: the
    // original pin missed `.option-row:hover` and descendant rules).
    const css = (await import('./ConfigPage.svelte?raw')).default
    const pointerRules = css.match(/[^{}]*\{[^}]*cursor:\s*pointer[^}]*\}/gs) ?? []
    const offending = pointerRules.filter((rule) => rule.includes('option-row'))
    expect(offending).toEqual([])
  })

  it('renders the loader error as a banner, not a raw body (issue #541)', async () => {
    apiConfig.impl = async () => ({
      ...configFixture,
      config_error: "'/data/config.json' was not applied, the previous settings stay in effect: x",
    })
    render(ConfigPage, { scanPaths: [], outputDir: '/out', targetVmaf: 96 })
    expect(await screen.findByText(/^config.json has an error and was not applied\.$/)).toBeTruthy()
    expect(screen.getByText(/ShrinkRay keeps running with the previous settings/)).toBeTruthy()
  })
})

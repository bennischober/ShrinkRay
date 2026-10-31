# Encode settings: the reference preset and what we do with it

`handbrake-preset-reference.json` in this folder is a verbatim export of a
working HandBrake preset that handles everything except the video stream the way
ShrinkRay should: Matroska container, every audio track copied, every subtitle
track kept, chapters and metadata preserved. It is the "this is what we want"
artifact for the non-video half of an encode.

It is a **reference document, not a runtime dependency**. Nothing in `src/`
reads it. The command ShrinkRay actually runs is built in
`src/shrinkray/tools/handbrake.py` (`build_encode_command`), and this file is the
contract that command is measured against. Issue #240 carries the work of closing
the gaps listed here.

## Why the JSON is not imported at runtime

`--preset-import-file <file> -Z "<name>"` would carry every key in one flag, and
it does work: the import is in-memory (`hb_presets_add_path`), so a resident
server is safe from a preset-store write. It is still the wrong shape for us:

- **The preset's language lists are filters.** `hb_preset_job_add_audio` iterates
  `AudioLanguageList` and adds only matching tracks; `--all-audio` only sets
  `AudioTrackSelectionBehavior` to `all`, it does not bypass the list. `und` is
  the `Unknown` entry, not the wildcard — the wildcard is the string `any`. An
  imported preset would therefore narrow the tracks we keep, invisibly, from a
  file our config does not describe.
- **`AudioEncoderFallback: none` drops tracks.** When a source codec is outside
  the copy mask and the fallback is none, `hb_autopassthru_apply_settings` logs
  "passthru not possible and no valid fallback specified, dropping track" and
  removes it. Losing a track is the one outcome we can never undo.
- **The behaviour depends on the installed HandBrake version.** An imported preset
  runs HandBrake's version-migration chain and then `preset_clean` against the
  installed `preset_template.json`: keys absent from that template are stripped,
  and newer migrations rewrite older presets. This export is `VersionMajor: 72`,
  which is above HandBrake 1.11.2's last migration (69), so it survives intact
  on the version the Dockerfile pins — but an older local install strips keys and
  a future release may transform them. An explicit flag fails loudly on an unknown
  argument; a preset changes behaviour silently.

Explicit flags are also reviewable: every setting we send is one line in
`build_encode_command`, greppable and testable.

## The command we run today

No `--preset` is passed, so every encode runs on HandBrakeCLI's built-in
`CLI Default` preset (`hb_presets_get_default`). Against that baseline:

| Reference key | Value | Today | Verdict |
|---|---|---|---|
| `VideoEncoder` | `svt_av1_10bit` | `encoder_for(bit_depth, hdr)` decides; the config's `--encoder` value is not consulted at the encode path | **we differ** — the SOURCE's bit depth decides; HDR forces 10-bit (`engine/encoders.py`, #234) |
| `VideoQualityType` / `VideoQualitySlider` | `2` / `28.0` | `--quality <measured CRF>` | **we differ** — the CRF is measured per file, never a constant |
| `VideoPreset` | `5` | probe `5`, final `3` | **we differ** — probe and output are separate settings (#22) |
| `VideoTune` | `ssim` | no `--encoder-tune` (SVT tune 0) | **open** — see "Open decisions" |
| `VideoMultiPass` | `true` | not passed | no effect — HandBrake forces single-pass for SVT-AV1 in CRF mode |
| `VideoColorRange` | `limited` | `CLI Default` also `limited` | already matches |
| `VideoPasshtruHDRDynamicMetadata` | `all` | template default `all` | already matches |
| `FileFormat` | `av_mkv` | pinned `av_mkv` for an `.mkv` source; extension-derived for anything else | landed — issue #240: the muxer can never drift from the name on an MKV source; a non-MKV source keeps bytes/name agreement and the 1:1 check catches what its muxer drops (a dropped track blocks the swap) |
| `Optimize` | `false` | `--optimize` always | no effect on MKV — the fast-start option is an MP4 muxer setting |
| `AudioTrackSelectionBehavior` | `all` | `--all-audio` | already matches |
| `AudioCopyMask` | 10 codecs incl. `mp2`, `opus` | `--audio-copy-mask` with the 12 names 1.6-1.11 knows | landed — issue #240: `mp2` and `opus` pass through instead of being transcoded to AAC |
| `AudioEncoderFallback` | `none` | `--audio-fallback flac24` | landed — issue #240: a track with no passthru is re-encoded losslessly (same samples); `none` would drop it, `aac` would re-encode lossily |
| `AudioLanguageList` | `deu, eng, und, jpn` | not passed by default (the keys ship empty since #297) | the reference list is the documented example; an operator opts into the narrowing via config.json or `shrinkray setup` |
| `SubtitleTrackSelectionBehavior` | `all` | `--all-subtitles` | already matches |
| `SubtitleLanguageList` | `eng, deu, und` | not passed by default (the keys ship empty since #297) | same rule as audio |
| `SubtitleBurnBehavior` | `foreign` | not passed (`CLI Default`'s `none`) | **never** — burning changes pixels and breaks the VMAF comparison |
| `SubtitleAddForeignAudioSearch` | `true` | not passed | **never** — it derives an extra forced-subtitle output track on top of the source's tracks, and with burning enabled it also changes pixels; we keep every source subtitle track, so there is nothing for the search to find |
| `PictureDeinterlaceFilter` | `decomb` | not passed (`CLI Default`'s `off`) | **never** — it interpolates picture content on combed frames, and the VMAF comparison measures the output against the *unfiltered* source, so every probe would score against a reference the encoder was told to diverge from |
| `PictureCombDetectPreset` | `default` | not passed | inert without the deinterlacer |
| `PictureAutoCrop` / `PictureCropMode` | `true` / `0` | `--crop` with the measured rect, or `0:0:0:0` | **we differ** — bars come from our own detector, and an auto-crop would break the VMAF geometry |
| `PictureWidth` / `PictureHeight` | `3840` / `2160` | not passed (no cap) | **we differ** — a cap would downscale anything above 4K; ShrinkRay never changes resolution except by cropping |
| `PictureAllowUpscaling` | `false` | not passed (`CLI Default` does not upscale either) | already matches |
| `ChapterMarkers` | `true` | `--markers` | landed — issue #240: chapters survive the encode by flag, not by CLI Default's default |
| `MetadataPassthru` | `true` | absent from `CLI Default`, filled `true` by the template | already matches |
| `PresetName` / `Default` / `Type` / `Folder` | preset metadata | n/a | not a setting |
| `AudioList[0].AudioMixdown` | `stereo` | not passed (Auto Passthru copies, no mixdown) | **never** — a stereo mixdown of a 5.1 track is not a copy |
| `AudioList[0].AudioBitrate` | `192` | not passed | irrelevant while the encoder is `copy` |

Two rows deserve their own sentence, because they look like settings but are not:

- **`VideoQualitySlider: 28`** is a fixed CRF. That is exactly what ShrinkRay
  replaces with the measured one: `--quality` overrides both `VideoQualityType` and
  `VideoQualitySlider` on the command line, so a preset's slider is never sticky.
- **`AudioList[0].AudioEncoder: "copy"`** matches `--aencoder copy`, but the
  mixdown and bitrate beside it only apply if a track actually reaches the encoder.
  Under Auto Passthru they do not, so they are inert here — and harmful if the
  preset is ever imported with the mask changed.

## What "copied 1:1" means in practice

`--aencoder copy` is HandBrake's Auto Passthru (`HB_ACODEC_AUTO_PASS`). It resolves
each source track through the copy mask and then the fallback encoder:

- in the mask → the track's bytes are copied;
- not in the mask, fallback set → re-encoded with the fallback;
- not in the mask, fallback `none` → the track is dropped.

The passthru codecs HandBrake 1.11.2 knows are `aac, ac3, alac, dts, dtshd, eac3,
flac, mp2, mp3, opus, pcm, truehd, vorbis`. `copy:pcm` is not in 1.9's list (which
has the other twelve), and an unknown name in the mask makes HandBrake fail the whole
job rather than degrade, so a mask has to stay within the vocabulary of the oldest
supported version (`MIN_HANDBRAKE_VERSION = "1.6"`).

A lossless fallback (`flac24`) keeps a track that has no passthru: same samples,
same channel layout up to 7.1, no bitrate, no DRC. `flac16` would truncate a 24-bit
source (`encavcodecaudio.c` maps it to `AV_SAMPLE_FMT_S16`), and the bare name
`flac` is not an encoder id at all — an invalid fallback fails job initialisation.

## Colour tags on the way out

For 1080p-class 8-bit 4:2:0 SDR sources — the shape the measurements below used
(10-bit sources are encoded too, and their fill behaviour is unmeasured) — a source
with unset colour components comes out of HandBrake with them **written as BT.709**: a fully untagged `yuv420p,tv,unknown` source is written as
`yuv420p,tv,bt709`, and a source tagged for space only (`yuv420p,tv,bt709` with
transfer/primaries `unknown`) comes out with the two unset components filled in as
`bt709` too. HandBrake assumes profile 1-1-1 at scan and carries it through for the HD sample —
measured on HandBrake 1.11.2, issue #258's acceptance run (2026-10-27); at SD-sized
resolutions the assumed profile differs (320x240-1024x576 come out with matrix
and primaries bt601-family - `smpte170m`/`bt470bg` - while the transfer tag is
bt709 at every measured size; 1280x720 measured all-bt709, so the boundary is not
the class line; the tag triple per size is recorded in PR #441's review thread
comment `6460170853`), and all of it is out of ShrinkRay's encode scope. Already-set components pass through unchanged —
pinned by `TestSmokeColourRetag.test_set_components_pass_through` (all three
components set to values the unset-fill at the fixture's 1080p size would never
write — `smpte170m`, `smpte170m`, `bt470bg` — survive the encode; at SD sizes
the fill writes `smpte170m`/`bt470bg` itself, so there the premise would not
hold — see the measurements above), not inferable from the acceptance
run's tagged row, whose set value equals the fill value.

This is signalling, not conversion. The measurement run that settled #258 found no
conversion filter in HandBrake's verbose log, and `--color-matrix`/`--color-range`
are output-signalling options: the run measured VMAF byte-identical with and
without them, which shows the flags add nothing (not that no pixel changed — VMAF
reads luma only and cannot see colour). On real untagged content the HandBrake
encode scored 95.9739 against a direct ffmpeg libsvtav1 encode of the same window
at 95.8941 - a same-window luma comparison (different SVT presets, same CRF)
showing no colour-driven deficit.
Two residuals stay open: the seed-pinned synthetic gradients clip still measures
2.31 VMAF apart between its untagged and tagged rows, unexplained, on that clip
only; and the real-title low readings that opened #258 (title B ~87 vs ab-av1's
~97 at CRF 14) are tracked as issue #443, unexplained. The mechanism the issue
suspected is refuted; those numbers are not.

Consequence for operators: on a 1080p-class 8-bit 4:2:0 SDR source (the measured
shape), its unset components claim BT.709 on the way out.
Whether players resolve an untagged HD source to BT.709 is player behaviour this
repo did not measure; the tags themselves are what this section documents. No flags
are added to `build_encode_command` for this. On the untagged sample the flags
were a measured no-op (they set the tags the encode sets anyway). On a source
with a set component the measured behaviour is the same fill: `--color-matrix
bt709` on a `smpte170m`-tagged 1080p clip leaves the space tag alone and fills
the unset transfer/primaries with the flag's value (measured with HandBrake
1.11.2; record in PR #441's review thread comment `6461940399`) — it does not
rewrite a set tag.

## Open decisions

**Language filtering defaults — DECIDED (issue #297, 2026-10-25).** The two keys
`handbrake.audio_languages` / `handbrake.subtitle_languages` ship EMPTY: every track in
every language survives an encode, which is the 1:1 promise PRODUCT.md makes and what
invariant 3 (a destructive action is opt-in) requires — a track-removing default would
delete content the operator never agreed to lose. The reference preset's lists
(audio `deu, eng, und, jpn`; subtitle `eng, deu, und`) remain documented here as the
example for operators who want the narrowing; they are a filter an operator opts into
via config.json or `shrinkray setup` (editable there since issue #337). A language
outside an active filter is dropped before the lossless fallback can rescue it, and the
track-count verification does not flag a track the configured filter removed on purpose.
`--audio-lang-list` on its own also sets the selection behaviour to `first`; only
`--all-audio` restores `all`, so the two flags must always travel together.

**Encoder tune.** `VideoTune: ssim` is a real encoder change (SVT `tune=2` instead of
`0`). If it is adopted, the plan fingerprint (`db.get_valid_plan`) must gain the tune,
or every stored plan keeps a CRF measured under a different tune.

**Atmos and DTS:X metadata.** Whether `copy:truehd` / `copy:dtshd` preserves the
Atmos or DTS:X extension metadata byte-for-byte is unverified. It needs a real file
and an ffprobe comparison before it is claimed.

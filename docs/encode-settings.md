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
| `VideoEncoder` | `svt_av1_10bit` | `--encoder` from config, 10-bit only for HDR | **we differ** — HDR decides the depth (`engine/encoders.py`) |
| `VideoQualityType` / `VideoQualitySlider` | `2` / `28.0` | `--quality <measured CRF>` | **we differ** — the CRF is measured per file, never a constant |
| `VideoPreset` | `5` | probe `5`, final `3` | **we differ** — probe and output are separate settings (#22) |
| `VideoTune` | `ssim` | no `--encoder-tune` (SVT tune 0) | **open** — see "Open decisions" |
| `VideoMultiPass` | `true` | not passed | no effect — HandBrake forces single-pass for SVT-AV1 in CRF mode |
| `VideoColorRange` | `limited` | `CLI Default` also `limited` | already matches |
| `VideoPasshtruHDRDynamicMetadata` | `all` | template default `all` | already matches |
| `FileFormat` | `av_mkv` | muxer autodetected from the output extension | **gap** — an `.mp4` source yields MP4, which drops PGS/DVB subtitles |
| `Optimize` | `false` | `--optimize` always | no effect on MKV — the fast-start option is an MP4 muxer setting |
| `AudioTrackSelectionBehavior` | `all` | `--all-audio` | already matches |
| `AudioCopyMask` | 10 codecs incl. `mp2`, `opus` | `CLI Default`'s 8 (no `mp2`, no `opus`) | **gap** — those two are transcoded to AAC |
| `AudioEncoderFallback` | `none` | `CLI Default`'s `aac` | **gap** — a track with no passthru is lossily re-encoded; `none` would drop it instead |
| `AudioLanguageList` | `deu, eng, und, jpn` | no list (all languages) | **open** — see "Open decisions" |
| `SubtitleTrackSelectionBehavior` | `all` | `--all-subtitles` | already matches |
| `SubtitleLanguageList` | `eng, deu, und` | no list (all languages) | **open** — see "Open decisions" |
| `SubtitleBurnBehavior` | `foreign` | not passed (`CLI Default`'s `none`) | **never** — burning changes pixels and breaks the VMAF comparison |
| `SubtitleAddForeignAudioSearch` | `true` | not passed | **never** — it derives an extra forced-subtitle output track on top of the source's tracks, and with burning enabled it also changes pixels; we keep every source subtitle track, so there is nothing for the search to find |
| `PictureDeinterlaceFilter` | `decomb` | not passed (`CLI Default`'s `off`) | **never** — it interpolates picture content on combed frames, and the VMAF comparison measures the output against the *unfiltered* source, so every probe would score against a reference the encoder was told to diverge from |
| `PictureCombDetectPreset` | `default` | not passed | inert without the deinterlacer |
| `PictureAutoCrop` / `PictureCropMode` | `true` / `0` | `--crop` with the measured rect, or `0:0:0:0` | **we differ** — bars come from our own detector, and an auto-crop would break the VMAF geometry |
| `PictureWidth` / `PictureHeight` | `3840` / `2160` | not passed (no cap) | **we differ** — a cap would downscale anything above 4K; ShrinkRay never changes resolution except by cropping |
| `PictureAllowUpscaling` | `false` | not passed (`CLI Default` does not upscale either) | already matches |
| `ChapterMarkers` | `true` | not passed (`CLI Default` also `true`) | already matches — worth pinning explicitly |
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

## Open decisions

**Language filtering.** The reference preset lists four audio and three subtitle
languages. Today ShrinkRay keeps every track in every language, because both lists are
empty in `CLI Default` and an empty list falls through to `any`. Adopting the lists
narrows that, and a dropped track cannot be rescued by the lossless fallback — the
filter runs first. The intended shape is a config key (empty = no filter) defaulting to
the reference lists, so the narrowing is visible in `config.json` rather than implied by
a preset. `--audio-lang-list` on its own also sets the selection behaviour to `first`;
only `--all-audio` restores `all`, so the two flags must always travel together.

**Encoder tune.** `VideoTune: ssim` is a real encoder change (SVT `tune=2` instead of
`0`). If it is adopted, the plan fingerprint (`db.get_valid_plan`) must gain the tune,
or every stored plan keeps a CRF measured under a different tune.

**Atmos and DTS:X metadata.** Whether `copy:truehd` / `copy:dtshd` preserves the
Atmos or DTS:X extension metadata byte-for-byte is unverified. It needs a real file
and an ffprobe comparison before it is claimed.

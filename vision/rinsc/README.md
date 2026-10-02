# vision/rinsc: RINSC corpus replay (roadmap item 8)

TL;DR: offline tracking of the four-finger bender fixture in the RINSC
Cs-137 campaign footage. `campaign.py` traces every selected clip in
parallel, `campaign_post.py` turns the traces into per-clip metrics vs
dose. The code lives here; the footage, the outputs and the campaign
findings do not (run data never enters the repo).

This is a fixture-specific pipeline. The general bender pipeline is
`vision/bender.py` (+ `vision/bender_replay.py`); roadmap item 6 is the
background-agnostic successor that should eventually replace this.

## Input

- Raw H.264 elementary streams named `actuation_YYYYmmdd_HHMMSS.h264`.
  Keep the original names: the timestamp in the name is the only clock
  the footage has.
- 2592x1944, yuv420p tv-range, 1152 frames per clip at a true 32 fps
  (36.0 s; the 30 fps the recorder was asked for is wrong).
- `fingers.json`: the fixture layout in raw-frame pixels (crop, one box
  per finger at rest, the CN9018 strip's band-tracker seed), per-finger
  material, source distance and dose rate, and the approximate scale.

## Run

Paths come only from the environment, so nothing machine-specific is
committed:

| variable | meaning | needed by |
|---|---|---|
| `RINSC_SHARE` | folder holding the clips | `campaign.py` (tracing) |
| `RINSC_OUT` | output folder (off OneDrive) | everything |
| `RINSC_TMP` | scratch for local clip copies | `campaign.py` (tracing) |
| `RINSC_SRC` | clip folder for single-clip runs | `trace_fingers.py` without clip args |
| `FFMPEG` | ffmpeg binary (else PATH, else the `imageio-ffmpeg` wheel) | tracing |

    python vision/rinsc/campaign.py --dry          # selection only
    python vision/rinsc/campaign.py [--workers N] [--from ISO_TIME]
    python vision/rinsc/campaign_post.py [--plot-only]
    python vision/rinsc/trace_fingers.py --out=DIR clip.h264 ...

`campaign.py` is resumable: a clip whose `<clip>_traces.csv` exists is
summarized, not re-traced (the CSV is written last, so it is the done
marker). When the share is unreachable or `RINSC_SHARE` is unset, the
clip list comes from `RINSC_OUT/clip_list.csv`.

Selection: series A (one clip per 240 s) is traced in full through
T = 165 h and hourly after that; series B (an extra clip 1 s before
each A, early campaign only) every 5th clip. T = hours since the first
clip.

## Output (in `RINSC_OUT`)

- `<clip>_traces.csv`: per frame, per finger: LK dx, dy, signed
  displacement, surviving point count; for the line-tracked strip, tip
  position and displacement (`tipx`/`ddx`... are the tracked point,
  `tip_inset_px` inboard of the band end), tip angle and `reach`
  (traced arc / rest arc).
- `<clip>_nodes.npz`: 16 centerline nodes of the line-tracked strip on
  every 2nd frame (for angle and curvature; the per-frame tip angle is
  too noisy for that).
- `campaign_summary.csv`, `progress.log` (campaign.py),
  `campaign_post.csv`, `campaign_vs_dose.png` (campaign_post.py).

## Method

- Plates and the UV-RSE strip: Shi-Tomasi corners on each finger's free
  end at rest, pyramidal Lucas-Kanade frame to frame with a
  forward-backward check (< 1 px; a point that fails once is dropped),
  median displacement from rest.
- CN9018 strip: `band_tracker.py` marches along the dark band from its
  visible root for the rest-frame arc length, so it follows curls the
  point tracker loses and lands on the same material point. The root
  is seeded on the strip edge line nearest `y_root_hint`
  (`root_center`), and the march only accepts a line within `search`
  px of its prediction, re-centered by at most `max_shift` px, and at
  least `rel_depth` x the running median depth. Without these guards
  (the merged 2026-10-01 campaign) the march hopped onto a wire line
  16 px below the strip near the root and ran past the tip into shadow
  until the crop margin, so the rest arc length, and with it the
  tracked material point, varied by ~40 px between clips. Displacement
  is taken `tip_inset_px` inboard of the rest band end: the end itself
  fades, and a march that stops there jitters (~1 px in no-drive
  clips). To rerun the merged-campaign code bit for bit, drop
  `y_root_hint` and `tip_inset_px` and set
  `"march": {"search": null, "max_shift": null, "rel_depth": null}` in
  `fingers.json`.
- Bends: `trace_fingers.bends()` detects the three drive pulses per
  clip on the clean #4 trace. #3 is measured inside #4's drive windows
  (`campaign_post.windowed_bends()`), never by detection on its own
  trace.

## Known limitations

- At the largest bends the #3 band tracker can stop short of the tip
  (`reach` < 1), so late #3 displacement may be under-read; check
  `reach` before trusting a late value.
- ffmpeg `format=gray` differs between builds (one uses chroma), so two
  machines trace slightly different pixels. A re-trace should decode
  the raw Y plane instead, which is bit-exact across builds.
- mm use the approximate scale in `fingers.json` with no fisheye
  correction (about +/-10%).
- Do not use file size as an actuation detector: it also follows a
  daily lighting cycle.

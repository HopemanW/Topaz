# Adaptive Model Selector

This document describes the quality-analysis and model-selection layer used by `adaptive_topaz.py`.

## Design goals

1. **Explainable** — every model choice should be traceable to explicit signals and thresholds.
2. **Portable** — Python standard library + FFmpeg/FFprobe only.
3. **Cheap** — analyze low-resolution samples instead of decoding the full source at native resolution.
4. **Graceful** — if visual sampling fails, rendering can continue using metadata-only signals.
5. **Version-aware** — inspect installed Topaz model definitions rather than assuming every model ID exists.

## Metadata features

`ffprobe` supplies:

- width / height
- frame rate
- codec
- stream or container bitrate
- duration
- pixel format
- field order

A compression-density feature is calculated as:

```text
bits_per_pixel_frame = bitrate / (fps * width * height)
```

The value is normalized by an approximate codec-efficiency factor before being converted to a `[0, 1]` bitrate score.

## Visual sampling

FFmpeg samples up to eight frames by default and converts them to `320x180` grayscale raw video. The analyzer computes all visual metrics directly on the byte arrays.

### Edge energy

Mean absolute horizontal and vertical neighboring-pixel difference, normalized to `[0, 1]`. This acts as a cheap local-contrast/sharpness proxy.

### Laplacian energy

A sparse 4-neighbor Laplacian response captures high-frequency image structure. It is logged as a diagnostic feature even though the current policy primarily uses edge energy for the quality score.

### 8-pixel blockiness

The analyzer compares absolute pixel differences across 8-pixel grid boundaries with differences at interior positions:

```text
blockiness = mean(boundary differences) / mean(interior differences)
```

A value near `1` is neutral. A substantially higher ratio is consistent with visible block-boundary artifacts, though it is not a calibrated codec-quality metric.

### Temporal change

A sparse mean absolute difference between sampled frames is recorded as a low-cost motion/activity descriptor.

## Quality score

```text
quality_score =
    0.28 * resolution_score
  + 0.42 * bitrate_score
  + 0.16 * sharpness_score
  + 0.14 * block_score
```

Compression risk is tracked separately:

```text
compression_risk =
    0.62 * (1 - bitrate_score)
  + 0.38 * (1 - block_score)
```

These numbers are engineering heuristics, not calibrated perceptual probabilities.

## Decision policy

Current default policy:

```text
animation hint
    -> Artemis Aliasing/Moire

faces hint + quality < 0.78
    -> Iris (LQ or MQ by quality)

compression risk >= 0.72 and quality < 0.48
    -> Iris LQ

quality < 0.34
    -> Artemis LQ

quality < 0.52
    -> Artemis MQ

quality < 0.80
    -> Proteus

otherwise
    -> Artemis HQ
```

The CLI prints both the selected model and a human-readable explanation.

## Model ID resolution

The selector scans the local Topaz model directory for JSON model definitions. Preferred IDs are ordered by use case rather than blindly taking the numerically newest version.

Example preferences:

```text
Proteus     : prob-4 -> prob-3 -> prob-2
Iris LQ     : iris-3 -> iris-2 -> iris-1
Iris MQ     : iris-2 -> iris-3 -> iris-1
Artemis HQ  : ahq-12 -> ahq-11 -> ahq-10
Artemis MQ  : amq-13 -> amq-12 -> amq-10
Artemis LQ  : alq-13 -> alq-12 -> alq-10
Artemis AA  : aaa-10 -> aaa-9
```

This is deliberate: model version numbers are not always like-for-like quality upgrades across all input regimes.

If the preferred family is not found but an installed Proteus model exists, Proteus is used as the general-purpose fallback.

## Semantic hints

`--content faces` and `--content animation` are explicit user hints. The default `--content auto` does **not** claim semantic detection that the code does not perform.

A future extension could add an optional face detector or lightweight scene classifier as a separate dependency while keeping the current no-dependency analyzer available.

## Future research direction

The current rule system is intentionally modular. The renderer only consumes a selected model ID, so the policy can later be replaced by:

- pairwise human preference learning
- VMAF/LPIPS/DISTS-assisted ranking
- a learned classifier over source features
- contextual bandit model selection
- per-scene rather than per-video model selection

A useful benchmark dataset would store source clips, candidate Topaz outputs, model parameters, runtime, perceptual metrics, and human pairwise preferences.

# Topaz Adaptive Video Pipeline

![CI](https://github.com/HopemanW/Topaz/actions/workflows/ci.yml/badge.svg)

A production-style automation layer around **Topaz Video / Topaz Video AI's bundled FFmpeg**. The project can inspect a source video, estimate quality/compression characteristics, choose an enhancement model, build a Topaz filter graph, select a hardware encoder, batch-process files, and save machine-readable telemetry.

The adaptive workflow targets **1080p / 60 FPS MP4** by default and can choose among **Proteus, Iris, and Artemis** before applying frame interpolation.

> This repository does **not** distribute Topaz software, models, or licenses. A licensed local Topaz installation is required.

## What this demonstrates

- explainable heuristic model selection instead of a hard-coded AI preset
- FFprobe media introspection and bitrate-per-pixel-frame analysis
- lightweight visual feature extraction from FFmpeg-sampled luma frames
- edge-energy, Laplacian, 8-pixel blockiness, and temporal-change metrics
- local Topaz model inventory discovery with version-aware fallback rules
- subprocess orchestration and robust CLI design
- dynamic hardware encoder selection (NVIDIA / Intel / AMD / software fallback)
- Topaz `tvai_up` + `tvai_fi` filter-graph construction
- aspect-ratio-safe resize and padding
- recursive batch processing
- dry-run / reproducibility support
- JSONL decision and render telemetry
- unit tests and GitHub Actions CI

## Adaptive pipeline

```text
input video
    |
    +----------------------+
    |                      |
    v                      v
 ffprobe              FFmpeg sampler
 metadata             320x180 luma frames
    |                      |
    |              +-------+-------+----------------+
    |              |               |                |
    |              v               v                v
    |         edge energy     blockiness      temporal change
    |              \               |                /
    |               +--------------+---------------+
    |                              |
    +----------> quality assessment
                   |
                   v
          explainable model policy
          Proteus / Iris / Artemis
                   |
                   v
             Topaz FFmpeg
                   |
          +--------+---------+
          |                  |
          v                  v
      tvai_up             tvai_fi
    enhancement        interpolation
          |                  |
          +--------+---------+
                   |
                   v
          hardware encoder
                   |
                   v
        MP4 + adaptive_jobs.jsonl
```

The selector is intentionally **explainable and auditable**. It is a heuristic policy, not a claim of a calibrated perceptual-quality model.

## Adaptive signals

The analyzer combines metadata and sampled-frame signals:

| Signal | Purpose |
|---|---|
| resolution score | distinguishes SD/HD/UHD source information density |
| bits per pixel per frame | codec-normalized compression proxy |
| edge energy | lightweight sharpness/local-contrast proxy |
| Laplacian energy | high-frequency detail diagnostic |
| 8 px blockiness ratio | compression-block boundary proxy |
| temporal change | cheap motion/activity descriptor |

The quality score is currently:

```text
quality = 0.28 * resolution
        + 0.42 * bitrate
        + 0.16 * sharpness
        + 0.14 * block_score
```

See [`docs/ADAPTIVE_SELECTOR.md`](docs/ADAPTIVE_SELECTOR.md) for the decision rules and design rationale.

## Model policy

The policy uses Topaz's model strengths rather than treating model names as interchangeable:

| Condition | Preferred family |
|---|---|
| high compression risk + low quality | Iris LQ |
| very low quality | Artemis LQ |
| medium-low quality | Artemis MQ |
| mixed / medium-high quality | Proteus |
| high-quality source | Artemis HQ |
| `--content faces` on low/medium quality | Iris |
| `--content animation` | Artemis Aliasing/Moire |

The script scans the local Topaz model directory and selects an installed compatible model ID when possible. This matters because Topaz model version numbers do not always represent like-for-like replacements; for example, different Iris versions may target different input-quality regimes.

## Requirements

- Python 3.10+
- Topaz Video / Topaz Video AI installed and activated
- Topaz's bundled FFmpeg with `tvai_up` and `tvai_fi`
- `ffprobe`

No NumPy, OpenCV, PyTorch, or external Python package is required for the adaptive analyzer.

## Quick start: adaptive mode

### Analyze + choose model + convert to 1080p60

```powershell
python .\adaptive_topaz.py "D:\video\input.mkv" `
  --preset 1080p60 `
  --overwrite
```

The CLI prints the selected model and the reason, for example:

```text
Visual signals : edge=0.0412, block=1.31, motion=0.0874, frames=8
AI selection   : iris-3 (iris-lq, tier=low, quality=0.29, compression-risk=0.78, confidence=0.74)
Why            : high compression risk and low quality favor Iris artifact recovery
```

### Dry-run the adaptive decision and exact render command

```powershell
python .\adaptive_topaz.py "D:\video\input.mkv" --dry-run
```

### Bias the selector toward face recovery

```powershell
python .\adaptive_topaz.py "input.mkv" --content faces --dry-run
```

`--content faces` is an explicit semantic hint. The project does **not** pretend that it runs a face detector when it does not.

### Animation / aliasing-oriented input

```powershell
python .\adaptive_topaz.py "animation.mkv" --content animation
```

### Disable sampled-frame analysis

```powershell
python .\adaptive_topaz.py "input.mkv" --no-visual-analysis --dry-run
```

The selector then falls back to metadata, resolution, codec, and bitrate signals.

### Force a model manually

```powershell
python .\adaptive_topaz.py "input.mkv" --up-model prob-4
```

## Original deterministic CLI

The original deterministic pipeline remains available:

```powershell
python .\topaz_auto.py "D:\video\input.mkv" `
  --output "D:\video\input_1080p60.mp4" `
  --preset 1080p60 `
  --overwrite
```

This is useful when you want a fully fixed model configuration rather than adaptive selection.

## Batch processing

```powershell
python .\adaptive_topaz.py "D:\video\input" `
  --output "D:\video\output" `
  --recursive `
  --overwrite
```

Each file is analyzed independently, so a mixed directory can receive different Topaz enhancement models per video.

## Hardware encoding

`--encoder auto` inspects the Topaz FFmpeg build and prefers:

1. `hevc_nvenc`
2. `h264_nvenc`
3. `hevc_qsv`
4. `h264_qsv`
5. `hevc_amf`
6. `h264_amf`
7. `libx265`
8. `libx264`

Example:

```powershell
python .\adaptive_topaz.py "input.mkv" --encoder hevc_nvenc --quality 18
```

## Topaz installation and model discovery

The core script checks `TOPAZ_FFMPEG`, common Windows/macOS Topaz locations, and finally `PATH`.

```powershell
python .\adaptive_topaz.py "input.mkv" `
  --ffmpeg "C:\Program Files\Topaz Labs LLC\Topaz Video AI\ffmpeg.exe"
```

The adaptive selector also scans the discovered model directory for `*.json` definitions so it can prefer model IDs that are actually installed. Use `--model-dir` when your installation stores models elsewhere.

Topaz model IDs can change between releases. When in doubt, configure a job in the Topaz GUI and inspect **Process -> Show Export Command**.

## Useful adaptive options

| Option | Purpose |
|---|---|
| `--up-model auto` | adaptive model selection (default in `adaptive_topaz.py`) |
| `--content auto` | quality-driven selection without semantic claims |
| `--content faces` | bias low/medium-quality input toward Iris |
| `--content animation` | prefer Artemis Aliasing/Moire |
| `--analysis-frames 8` | number of low-res luma samples |
| `--analysis-interval 5` | seconds between samples |
| `--no-visual-analysis` | metadata-only fallback |
| `--preset 1080p60` | 1920x1080 at 60 fps |
| `--resolution 2560x1440` | custom even-numbered dimensions |
| `--fps 60` | custom interpolation target |
| `--fi-model chf-3` | Topaz interpolation model ID |
| `--order` | `enhance-first` or `interpolate-first` |
| `--encoder auto` | automatically choose output encoder |
| `--dry-run` | analyze and print the command without rendering |

## Decision telemetry

Adaptive jobs write to `adaptive_jobs.jsonl`. Each record includes:

- source resolution, FPS, codec, pixel format, bitrate
- bits per pixel per frame
- sampled visual metrics
- quality and compression-risk scores
- chosen model family + exact model ID
- confidence heuristic
- human-readable selection reason
- target resolution / FPS
- encoder
- elapsed time
- exact FFmpeg command
- success/failure status

The line-oriented format is easy to load into Python, DuckDB, pandas, or a dashboard for later benchmarking.

## Testing

```bash
python -m unittest discover -s tests -v
```

The test suite covers both the render-command builder and adaptive policy, including synthetic block-artifact frames and installed-model fallback behavior. CI runs the same test discovery on pushes and pull requests.

## References

- Topaz CLI documentation: https://docs.topazlabs.com/video-ai/advanced-functions-in-topaz-video-ai/command-line-interface
- Topaz filter/model overview: https://docs.topazlabs.com/topaz-video/filters
- Topaz enhancement models: https://docs.topazlabs.com/topaz-video/filters/enhancement

## Engineering notes

The adaptive score and thresholds are explicit on purpose: they can be benchmarked, tuned, or replaced later by a learned ranking model without changing the rendering layer. A natural next experiment is to build a labeled preference dataset from paired Topaz outputs and learn the model-selection policy from human or perceptual-quality rankings.

Topaz's FFmpeg/model interface is not a stable public SDK. For production use, pin a known Topaz release and validate generated commands against **Show Export Command** after upgrades.

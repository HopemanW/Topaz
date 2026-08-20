# Topaz Auto Enhance

A small production-style automation layer around **Topaz Video / Topaz Video AI's bundled FFmpeg**. It probes source media, builds a Topaz filter graph for AI enhancement + frame interpolation, selects an available hardware encoder, supports batch processing, and records machine-readable job logs.

The default workflow targets **1080p / 60 FPS MP4** using **Proteus (`prob-4`)** + **Chronos Fast (`chf-3`)**.

> This project does **not** distribute Topaz software, models, or licenses. A licensed local Topaz installation is required.

## Why this project

Topaz exposes its enhancement and interpolation models as FFmpeg filters (`tvai_up` and `tvai_fi`). That makes it possible to build reproducible media pipelines instead of manually configuring every export in the GUI.

This repository demonstrates:

- subprocess orchestration and robust CLI design
- FFprobe-based media introspection
- dynamic hardware encoder selection (NVIDIA / Intel / AMD / software fallback)
- Topaz model filter-graph construction
- aspect-ratio-safe resize + letterbox/pillarbox handling
- recursive batch processing
- dry-run / reproducibility support
- JSONL job telemetry
- unit tests and GitHub Actions CI

## Pipeline

```text
input video
    |
    v
ffprobe --------> source metadata
    |
    v
Topaz FFmpeg
    |
    +--> tvai_up  (AI enhancement / upscale)
    |
    +--> Lanczos fit + aspect-ratio-safe padding
    |
    +--> tvai_fi  (AI frame interpolation)
    |
    v
hardware encoder (auto)
    |
    v
1080p60 MP4 + JSONL job log
```

The filter order is configurable. `enhance-first` is the default; `interpolate-first` can reduce interpolation cost because it processes the lower-resolution source before upscaling.

## Requirements

- Python 3.10+
- Topaz Video / Topaz Video AI installed and activated
- Topaz's bundled FFmpeg with `tvai_up` and `tvai_fi`
- `ffprobe` (usually bundled or available from a normal FFmpeg installation)

The script checks that the chosen FFmpeg actually exposes the Topaz filters, which prevents accidentally running against a standard FFmpeg build.

## Quick start

### One 720p MKV -> 1080p60 MP4

```powershell
python .\topaz_auto.py "D:\video\input.mkv" `
  --output "D:\video\input_1080p60.mp4" `
  --preset 1080p60 `
  --overwrite
```

### Preview the exact FFmpeg command without rendering

```powershell
python .\topaz_auto.py "D:\video\input.mkv" --dry-run
```

### Batch process a directory

```powershell
python .\topaz_auto.py "D:\video\input" `
  --output "D:\video\output" `
  --recursive `
  --overwrite
```

### Override Topaz model IDs

```powershell
python .\topaz_auto.py "input.mkv" `
  --up-model prob-4 `
  --fi-model chf-3 `
  --preset 1080p60
```

Topaz model IDs can change between app releases. The CLI intentionally exposes them as arguments. If your installed version uses different IDs, configure a job in the Topaz GUI and inspect **Process -> Show Export Command**, then copy the model IDs into `--up-model` and `--fi-model`.

## Hardware encoding

`--encoder auto` inspects the Topaz FFmpeg build and chooses the first supported encoder in this order:

1. `hevc_nvenc`
2. `h264_nvenc`
3. `hevc_qsv`
4. `h264_qsv`
5. `hevc_amf`
6. `h264_amf`
7. `libx265`
8. `libx264`

You can force one explicitly:

```powershell
python .\topaz_auto.py "input.mkv" --encoder hevc_nvenc --quality 18
```

## Topaz installation discovery

The script checks `TOPAZ_FFMPEG`, common Windows/macOS Topaz install paths, and finally `PATH`.

You can always specify the binary manually:

```powershell
python .\topaz_auto.py "input.mkv" `
  --ffmpeg "C:\Program Files\Topaz Labs LLC\Topaz Video AI\ffmpeg.exe"
```

It also attempts to configure `TVAI_MODEL_DIR` and `TVAI_MODEL_DATA_DIR` from common Topaz model directories. Use `--model-dir` when your installation stores models elsewhere.

## Useful options

| Option | Purpose |
|---|---|
| `--preset 1080p60` | 1920x1080 at 60 fps |
| `--resolution 2560x1440` | custom even-numbered dimensions |
| `--fps 60` | custom interpolation target |
| `--up-model` | Topaz enhancement/upscale model ID |
| `--fi-model` | Topaz frame-interpolation model ID |
| `--order` | `enhance-first` or `interpolate-first` |
| `--device 0` | Topaz processing device index |
| `--vram 1.0` | VRAM fraction passed to Topaz |
| `--instances 1` | Topaz model instances |
| `--encoder auto` | automatically choose an output encoder |
| `--quality 18` | quality target; lower generally means higher quality |
| `--dry-run` | print commands without rendering |
| `--recursive` | recurse through input directories |

## Job telemetry

Every completed/failed render appends a JSON object to `topaz_jobs.jsonl`, including:

- source resolution / frame rate / codec
- selected Topaz models
- target resolution / frame rate
- selected encoder
- elapsed processing time
- exact command used
- success or failure status

That log is intentionally line-oriented so it can later be loaded into Python, DuckDB, pandas, or a monitoring dashboard.

## Testing

The unit tests validate the command builder without requiring Topaz itself:

```bash
python -m unittest discover -s tests -v
```

CI runs the same tests on every push and pull request.

## References

- Topaz CLI documentation: https://docs.topazlabs.com/video-ai/advanced-functions-in-topaz-video-ai/command-line-interface
- Topaz filter overview: https://docs.topazlabs.com/topaz-video/filters
- Topaz 60 FPS guide: https://docs.topazlabs.com/video-ai/how-to-guide/convert-2425-fps-to-60-fps

## Notes

The defaults are intentionally conservative and version-tolerant, but Topaz's FFmpeg/model interface is not a stable public SDK. For production use, pin a known Topaz release and validate the generated command against **Show Export Command** after upgrades.

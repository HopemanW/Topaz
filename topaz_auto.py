#!/usr/bin/env python3
"""
Topaz Auto Enhance
Automate Topaz Video / Topaz Video AI enhancement + frame interpolation
through Topaz's bundled FFmpeg filters.

Requires a licensed Topaz installation. No proprietary Topaz models are
distributed by this project.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shlex
import shutil
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable, Sequence

VIDEO_EXTENSIONS = {
    ".mp4", ".mkv", ".mov", ".avi", ".m4v", ".webm", ".mts", ".m2ts", ".ts"
}

PRESETS = {
    "1080p60": (1920, 1080, 60.0),
    "1080p30": (1920, 1080, 30.0),
    "1440p60": (2560, 1440, 60.0),
    "4k60": (3840, 2160, 60.0),
}

WINDOWS_TOPAZ_FFMPEG = [
    r"C:\Program Files\Topaz Labs LLC\Topaz Video\ffmpeg.exe",
    r"C:\Program Files\Topaz Labs LLC\Topaz Video AI\ffmpeg.exe",
]
MAC_TOPAZ_FFMPEG = [
    "/Applications/Topaz Video.app/Contents/MacOS/ffmpeg",
    "/Applications/Topaz Video AI.app/Contents/MacOS/ffmpeg",
]
WINDOWS_MODEL_DIRS = [
    r"C:\ProgramData\Topaz Labs LLC\Topaz Video\models",
    r"C:\ProgramData\Topaz Labs LLC\Topaz Video AI\models",
]


@dataclass(frozen=True)
class VideoInfo:
    width: int
    height: int
    fps: float
    codec: str
    duration_seconds: float | None


@dataclass(frozen=True)
class Job:
    input: str
    output: str
    source: VideoInfo
    preset: str
    width: int
    height: int
    fps: float
    up_model: str
    fi_model: str
    encoder: str


def parse_resolution(value: str) -> tuple[int, int]:
    try:
        w, h = value.lower().split("x", 1)
        width, height = int(w), int(h)
    except (ValueError, AttributeError) as exc:
        raise argparse.ArgumentTypeError("Resolution must look like 1920x1080.") from exc
    if width <= 0 or height <= 0 or width % 2 or height % 2:
        raise argparse.ArgumentTypeError("Resolution must use positive even dimensions.")
    return width, height


def parse_fps(rate: str | None) -> float:
    if not rate or rate in {"0/0", "N/A"}:
        return 0.0
    if "/" in rate:
        num, den = rate.split("/", 1)
        return float(num) / float(den) if float(den) else 0.0
    return float(rate)


def shell_join(parts: Sequence[str]) -> str:
    if os.name == "nt":
        return subprocess.list2cmdline(list(parts))
    return shlex.join(parts)


def _first_existing(paths: Iterable[str]) -> str | None:
    for candidate in paths:
        if candidate and Path(candidate).is_file():
            return str(Path(candidate))
    return None


def find_topaz_ffmpeg(explicit: str | None = None) -> str:
    candidates = []
    if explicit:
        candidates.append(explicit)
    env_path = os.environ.get("TOPAZ_FFMPEG")
    if env_path:
        candidates.append(env_path)
    candidates.extend(WINDOWS_TOPAZ_FFMPEG if os.name == "nt" else MAC_TOPAZ_FFMPEG)

    found = _first_existing(candidates)
    if found:
        return found

    # PATH is last because ordinary FFmpeg builds do not contain Topaz filters.
    path_ffmpeg = shutil.which("ffmpeg")
    if path_ffmpeg:
        return path_ffmpeg

    raise FileNotFoundError(
        "Could not locate Topaz FFmpeg. Use --ffmpeg PATH or set TOPAZ_FFMPEG."
    )


def find_ffprobe(ffmpeg: str, explicit: str | None = None) -> str:
    if explicit and Path(explicit).is_file():
        return explicit
    sibling = Path(ffmpeg).with_name("ffprobe.exe" if os.name == "nt" else "ffprobe")
    if sibling.is_file():
        return str(sibling)
    path_ffprobe = shutil.which("ffprobe")
    if path_ffprobe:
        return path_ffprobe
    raise FileNotFoundError(
        "ffprobe was not found beside FFmpeg or on PATH. Use --ffprobe PATH."
    )


def configure_model_environment(model_dir: str | None = None) -> str | None:
    if model_dir:
        selected = Path(model_dir)
    elif os.environ.get("TVAI_MODEL_DIR"):
        selected = Path(os.environ["TVAI_MODEL_DIR"])
    elif os.name == "nt":
        selected = next((Path(p) for p in WINDOWS_MODEL_DIRS if Path(p).is_dir()), None)
    else:
        candidates = [
            Path("/Applications/Topaz Video.app/Contents/Resources/models"),
            Path("/Applications/Topaz Video AI.app/Contents/Resources/models"),
        ]
        selected = next((p for p in candidates if p.is_dir()), None)

    if selected and selected.is_dir():
        os.environ.setdefault("TVAI_MODEL_DIR", str(selected))
        os.environ.setdefault("TVAI_MODEL_DATA_DIR", str(selected))
        return str(selected)
    return None


def run_capture(command: Sequence[str]) -> str:
    completed = subprocess.run(
        list(command), check=True, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace"
    )
    return completed.stdout


def verify_topaz_filters(ffmpeg: str) -> None:
    output = run_capture([ffmpeg, "-hide_banner", "-filters"])
    missing = [name for name in ("tvai_up", "tvai_fi") if name not in output]
    if missing:
        raise RuntimeError(
            f"FFmpeg does not expose required Topaz filter(s): {', '.join(missing)}. "
            "Point --ffmpeg at the FFmpeg bundled with Topaz Video."
        )


def probe_video(ffprobe: str, input_path: Path) -> VideoInfo:
    command = [
        ffprobe, "-v", "error", "-select_streams", "v:0",
        "-show_entries",
        "stream=width,height,codec_name,avg_frame_rate:format=duration",
        "-of", "json", str(input_path),
    ]
    payload = json.loads(run_capture(command))
    streams = payload.get("streams", [])
    if not streams:
        raise RuntimeError(f"No video stream found in {input_path}")
    stream = streams[0]
    duration = payload.get("format", {}).get("duration")
    return VideoInfo(
        width=int(stream["width"]),
        height=int(stream["height"]),
        fps=parse_fps(stream.get("avg_frame_rate")),
        codec=str(stream.get("codec_name", "unknown")),
        duration_seconds=float(duration) if duration not in (None, "N/A") else None,
    )


def encoder_inventory(ffmpeg: str) -> str:
    return run_capture([ffmpeg, "-hide_banner", "-encoders"])


def detect_encoder(ffmpeg: str, requested: str) -> str:
    if requested != "auto":
        return requested

    available = encoder_inventory(ffmpeg)
    # Prefer GPU encoders; NVIDIA first because it is common on high-end workstations.
    for encoder in (
        "hevc_nvenc", "h264_nvenc",
        "hevc_qsv", "h264_qsv",
        "hevc_amf", "h264_amf",
        "libx265", "libx264",
    ):
        if encoder in available:
            return encoder
    raise RuntimeError("No supported output encoder found in this FFmpeg build.")


def encoder_args(encoder: str, quality: int) -> list[str]:
    if not 0 <= quality <= 51:
        raise ValueError("quality must be between 0 and 51")

    if encoder == "hevc_nvenc":
        return ["-c:v", encoder, "-preset", "p6", "-tune", "hq",
                "-rc:v", "vbr", "-cq:v", str(quality), "-b:v", "0",
                "-profile:v", "main", "-tag:v", "hvc1"]
    if encoder == "h264_nvenc":
        return ["-c:v", encoder, "-preset", "p6", "-tune", "hq",
                "-rc:v", "vbr", "-cq:v", str(quality), "-b:v", "0",
                "-profile:v", "high"]
    if encoder in {"hevc_qsv", "h264_qsv"}:
        return ["-c:v", encoder, "-preset", "medium",
                "-global_quality", str(quality)]
    if encoder in {"hevc_amf", "h264_amf"}:
        return ["-c:v", encoder, "-quality", "quality",
                "-rc", "cqp", "-qp_i", str(quality), "-qp_p", str(quality + 2)]
    if encoder in {"libx265", "libx264"}:
        return ["-c:v", encoder, "-preset", "slow", "-crf", str(quality)]
    return ["-c:v", encoder]


def topaz_filter(
    width: int,
    height: int,
    fps: float,
    up_model: str,
    fi_model: str,
    device: int,
    vram: float,
    instances: int,
    order: str,
) -> str:
    common = f"device={device}:vram={vram:g}:instances={instances}"
    upscale = (
        f"tvai_up=model={up_model}:scale=0:w={width}:h={height}:"
        f"preblur=0:noise=0:details=0:halo=0:blur=0:compression=0:"
        f"estimate=8:blend=0.2:{common}"
    )
    interpolate = f"tvai_fi=model={fi_model}:fps={fps:g}:{common}"
    finish = (
        f"scale=w={width}:h={height}:flags=lanczos:threads=0:"
        "force_original_aspect_ratio=decrease,"
        f"pad={width}:{height}:-1:-1:color=black"
    )
    if order == "interpolate-first":
        return ",".join((interpolate, upscale, finish))
    return ",".join((upscale, finish, interpolate))


def build_command(
    ffmpeg: str,
    input_path: Path,
    output_path: Path,
    *,
    width: int,
    height: int,
    fps: float,
    up_model: str,
    fi_model: str,
    device: int,
    vram: float,
    instances: int,
    order: str,
    encoder: str,
    quality: int,
    overwrite: bool,
) -> list[str]:
    filters = topaz_filter(
        width, height, fps, up_model, fi_model, device, vram, instances, order
    )
    command = [
        ffmpeg, "-hide_banner", "-nostdin",
        "-y" if overwrite else "-n",
        "-hwaccel", "auto",
        "-i", str(input_path),
        "-sws_flags", "spline+accurate_rnd+full_chroma_int",
        "-filter_complex", filters,
        "-map", "0:v:0",
        "-map", "0:a?",
        *encoder_args(encoder, quality),
        "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "192k",
        "-map_metadata", "0",
        "-movflags", "+faststart",
        "-metadata", (
            f"comment=Enhanced with Topaz automation; "
            f"up={up_model}; interpolation={fi_model}; target={width}x{height}@{fps:g}"
        ),
        str(output_path),
    ]
    return command


def collect_inputs(path: Path, recursive: bool) -> list[Path]:
    if path.is_file():
        return [path]
    if not path.is_dir():
        raise FileNotFoundError(path)
    iterator = path.rglob("*") if recursive else path.glob("*")
    return sorted(p for p in iterator if p.is_file() and p.suffix.lower() in VIDEO_EXTENSIONS)


def output_for(input_file: Path, base_input: Path, output: Path | None) -> Path:
    if output is None:
        return input_file.with_name(f"{input_file.stem}_topaz_1080p60.mp4")
    if base_input.is_file():
        return output if output.suffix else output / f"{input_file.stem}_topaz.mp4"
    relative = input_file.relative_to(base_input)
    return (output / relative).with_suffix(".mp4")


def log_job(log_path: Path, job: Job, command: Sequence[str], status: str, seconds: float) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "status": status,
        "elapsed_seconds": round(seconds, 3),
        "job": {
            **asdict(job),
            "source": asdict(job.source),
        },
        "command": list(command),
    }
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def create_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Batch enhancement + FPS interpolation using Topaz's FFmpeg filters."
    )
    parser.add_argument("input", type=Path, help="Video file or directory.")
    parser.add_argument("-o", "--output", type=Path, help="Output file or directory.")
    parser.add_argument("--preset", choices=sorted(PRESETS), default="1080p60")
    parser.add_argument("--resolution", type=parse_resolution,
                        help="Override preset resolution, e.g. 1920x1080.")
    parser.add_argument("--fps", type=float, help="Override preset FPS.")
    parser.add_argument("--up-model", default="prob-4",
                        help="Topaz enhancement model id (default: prob-4 / Proteus).")
    parser.add_argument("--fi-model", default="chf-3",
                        help="Topaz interpolation model id (default: chf-3 / Chronos Fast).")
    parser.add_argument("--order", choices=("enhance-first", "interpolate-first"),
                        default="enhance-first")
    parser.add_argument("--device", type=int, default=0)
    parser.add_argument("--vram", type=float, default=1.0,
                        help="Topaz VRAM fraction passed to filters (default: 1.0).")
    parser.add_argument("--instances", type=int, default=1)
    parser.add_argument("--encoder", default="auto",
                        help="auto, hevc_nvenc, h264_nvenc, hevc_qsv, ...")
    parser.add_argument("--quality", type=int, default=18,
                        help="Encoder quality value (default: 18; lower is higher quality).")
    parser.add_argument("--ffmpeg", help="Path to Topaz-bundled ffmpeg executable.")
    parser.add_argument("--ffprobe", help="Path to ffprobe executable.")
    parser.add_argument("--model-dir", help="Topaz model directory.")
    parser.add_argument("--recursive", action="store_true", help="Scan input directory recursively.")
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print commands without processing videos.")
    parser.add_argument("--skip-filter-check", action="store_true",
                        help="Skip validation that FFmpeg exposes tvai_up/tvai_fi.")
    parser.add_argument("--log", type=Path, default=Path("topaz_jobs.jsonl"))
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = create_parser().parse_args(argv)

    if args.fps is not None and args.fps <= 0:
        raise SystemExit("--fps must be positive.")
    if not 0 < args.vram <= 1:
        raise SystemExit("--vram must be in (0, 1].")
    if args.instances < 1:
        raise SystemExit("--instances must be >= 1.")

    ffmpeg = find_topaz_ffmpeg(args.ffmpeg)
    ffprobe = find_ffprobe(ffmpeg, args.ffprobe)
    model_dir = configure_model_environment(args.model_dir)

    if not args.skip_filter_check:
        verify_topaz_filters(ffmpeg)

    encoder = detect_encoder(ffmpeg, args.encoder)
    preset_w, preset_h, preset_fps = PRESETS[args.preset]
    width, height = args.resolution or (preset_w, preset_h)
    fps = args.fps or preset_fps

    inputs = collect_inputs(args.input, args.recursive)
    if not inputs:
        raise SystemExit("No supported video files found.")

    print(f"Platform       : {platform.platform()}")
    print(f"Topaz FFmpeg  : {ffmpeg}")
    print(f"Model dir     : {model_dir or 'not auto-detected (existing env may still work)'}")
    print(f"Encoder       : {encoder}")
    print(f"Target        : {width}x{height} @ {fps:g} fps")
    print(f"Jobs          : {len(inputs)}")

    failures = 0
    for index, input_file in enumerate(inputs, 1):
        output_file = output_for(input_file, args.input, args.output)
        output_file.parent.mkdir(parents=True, exist_ok=True)

        source = probe_video(ffprobe, input_file)
        job = Job(
            input=str(input_file),
            output=str(output_file),
            source=source,
            preset=args.preset,
            width=width,
            height=height,
            fps=fps,
            up_model=args.up_model,
            fi_model=args.fi_model,
            encoder=encoder,
        )
        command = build_command(
            ffmpeg, input_file, output_file,
            width=width, height=height, fps=fps,
            up_model=args.up_model, fi_model=args.fi_model,
            device=args.device, vram=args.vram, instances=args.instances,
            order=args.order, encoder=encoder, quality=args.quality,
            overwrite=args.overwrite,
        )

        print(f"\n[{index}/{len(inputs)}] {input_file.name}")
        print(
            f"Source         : {source.width}x{source.height} @ {source.fps:.3f} fps "
            f"({source.codec})"
        )
        print(f"Output         : {output_file}")
        print(f"Command        : {shell_join(command)}")

        if args.dry_run:
            continue

        started = time.perf_counter()
        try:
            subprocess.run(command, check=True)
        except subprocess.CalledProcessError as exc:
            failures += 1
            elapsed = time.perf_counter() - started
            log_job(args.log, job, command, f"failed:{exc.returncode}", elapsed)
            print(f"FAILED (exit {exc.returncode})", file=sys.stderr)
        else:
            elapsed = time.perf_counter() - started
            log_job(args.log, job, command, "success", elapsed)
            print(f"Completed in {elapsed / 60:.1f} min")

    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())

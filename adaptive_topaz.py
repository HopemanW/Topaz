#!/usr/bin/env python3
"""Adaptive Topaz Video automation CLI.

This entry point adds an explainable quality-analysis/model-selection layer on
Topaz Auto Enhance while reusing the core render pipeline in topaz_auto.py.
"""

from __future__ import annotations

import argparse
import json
import platform
import subprocess
import sys
import time
from dataclasses import asdict
from pathlib import Path
from typing import Sequence

import adaptive_quality as aq
import topaz_auto as core


def output_for(input_file: Path, base_input: Path, output: Path | None, preset: str) -> Path:
    if output is None:
        return input_file.with_name(f"{input_file.stem}_adaptive_{preset}.mp4")
    if base_input.is_file():
        return output if output.suffix else output / f"{input_file.stem}_adaptive_{preset}.mp4"
    relative = input_file.relative_to(base_input)
    return (output / relative).with_suffix(".mp4")


def log_job(
    log_path: Path,
    *,
    source: aq.MediaProfile,
    input_file: Path,
    output_file: Path,
    command: Sequence[str],
    selection: dict,
    target: dict,
    encoder: str,
    status: str,
    elapsed_seconds: float,
) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "status": status,
        "elapsed_seconds": round(elapsed_seconds, 3),
        "input": str(input_file),
        "output": str(output_file),
        "source": asdict(source),
        "selection": selection,
        "target": target,
        "encoder": encoder,
        "command": list(command),
    }
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def create_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Analyze source quality, choose an explainable Topaz enhancement model, "
            "then render with Topaz's bundled FFmpeg."
        )
    )
    parser.add_argument("input", type=Path, help="Video file or directory.")
    parser.add_argument("-o", "--output", type=Path, help="Output file or directory.")
    parser.add_argument("--preset", choices=sorted(core.PRESETS), default="1080p60")
    parser.add_argument("--resolution", type=core.parse_resolution,
                        help="Override preset resolution, e.g. 1920x1080.")
    parser.add_argument("--fps", type=float, help="Override preset FPS.")
    parser.add_argument(
        "--up-model", default="auto",
        help="auto (default) or an explicit Topaz model id such as prob-4 or iris-3.",
    )
    parser.add_argument("--fi-model", default="chf-3")
    parser.add_argument(
        "--content", choices=("auto", "general", "faces", "animation"), default="auto",
        help="Optional semantic hint. Auto never pretends to run face detection.",
    )
    parser.add_argument("--analysis-frames", type=int, default=8,
                        help="Low-resolution luma frames sampled for quality analysis.")
    parser.add_argument("--analysis-interval", type=float, default=5.0,
                        help="Seconds between analysis samples (default: 5).")
    parser.add_argument("--no-visual-analysis", action="store_true",
                        help="Choose from metadata/bitrate only; skip sampled-frame metrics.")
    parser.add_argument("--order", choices=("enhance-first", "interpolate-first"),
                        default="enhance-first")
    parser.add_argument("--device", type=int, default=0)
    parser.add_argument("--vram", type=float, default=1.0)
    parser.add_argument("--instances", type=int, default=1)
    parser.add_argument("--encoder", default="auto")
    parser.add_argument("--quality", type=int, default=18)
    parser.add_argument("--ffmpeg", help="Path to Topaz-bundled ffmpeg executable.")
    parser.add_argument("--ffprobe", help="Path to ffprobe executable.")
    parser.add_argument("--model-dir", help="Topaz model directory.")
    parser.add_argument("--recursive", action="store_true")
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--skip-filter-check", action="store_true")
    parser.add_argument("--log", type=Path, default=Path("adaptive_jobs.jsonl"))
    return parser


def _selection_summary(decision: aq.ModelDecision) -> str:
    a = decision.assessment
    return (
        f"{decision.model_id} ({decision.family}, tier={decision.quality_tier}, "
        f"quality={a.quality_score:.2f}, compression-risk={a.compression_risk:.2f}, "
        f"confidence={decision.confidence:.2f})"
    )


def main(argv: Sequence[str] | None = None) -> int:
    args = create_parser().parse_args(argv)

    if args.fps is not None and args.fps <= 0:
        raise SystemExit("--fps must be positive.")
    if not 0 < args.vram <= 1:
        raise SystemExit("--vram must be in (0, 1].")
    if args.instances < 1:
        raise SystemExit("--instances must be >= 1.")
    if args.analysis_frames < 0:
        raise SystemExit("--analysis-frames must be >= 0.")
    if args.analysis_interval <= 0:
        raise SystemExit("--analysis-interval must be positive.")

    ffmpeg = core.find_topaz_ffmpeg(args.ffmpeg)
    ffprobe = core.find_ffprobe(ffmpeg, args.ffprobe)
    model_dir = core.configure_model_environment(args.model_dir)
    if not args.skip_filter_check:
        core.verify_topaz_filters(ffmpeg)

    encoder = core.detect_encoder(ffmpeg, args.encoder)
    preset_w, preset_h, preset_fps = core.PRESETS[args.preset]
    width, height = args.resolution or (preset_w, preset_h)
    fps = args.fps or preset_fps
    inputs = core.collect_inputs(args.input, args.recursive)
    if not inputs:
        raise SystemExit("No supported video files found.")

    installed_models = aq.discover_installed_models(model_dir)

    print(f"Platform       : {platform.platform()}")
    print(f"Topaz FFmpeg  : {ffmpeg}")
    print(f"Model dir     : {model_dir or 'not auto-detected'}")
    print(f"Models indexed: {len(installed_models) if installed_models else 'unknown'}")
    print(f"Encoder       : {encoder}")
    print(f"Target        : {width}x{height} @ {fps:g} fps")
    print(f"Selector      : {'adaptive' if args.up_model == 'auto' else 'manual override'}")
    print(f"Jobs          : {len(inputs)}")

    failures = 0
    for index, input_file in enumerate(inputs, 1):
        output_file = output_for(input_file, args.input, args.output, args.preset)
        output_file.parent.mkdir(parents=True, exist_ok=True)
        media = aq.probe_media(ffprobe, input_file)

        visual = None
        if args.up_model == "auto" and not args.no_visual_analysis:
            visual = aq.analyze_visual_quality(
                ffmpeg,
                input_file,
                sample_frames=args.analysis_frames,
                interval_seconds=args.analysis_interval,
            )

        if args.up_model == "auto":
            decision = aq.select_model(
                media,
                visual,
                content=args.content,
                available_models=installed_models,
            )
            selected_model = decision.model_id
            selection = {"mode": "adaptive", **decision.as_dict()}
        else:
            selected_model = args.up_model
            decision = None
            selection = {
                "mode": "manual",
                "model_id": selected_model,
                "content_hint": args.content,
            }

        command = core.build_command(
            ffmpeg,
            input_file,
            output_file,
            width=width,
            height=height,
            fps=fps,
            up_model=selected_model,
            fi_model=args.fi_model,
            device=args.device,
            vram=args.vram,
            instances=args.instances,
            order=args.order,
            encoder=encoder,
            quality=args.quality,
            overwrite=args.overwrite,
        )

        print(f"\n[{index}/{len(inputs)}] {input_file.name}")
        bppf = media.bits_per_pixel_frame
        if bppf is not None:
            print(
                f"Source         : {media.width}x{media.height} @ {media.fps:.3f} fps "
                f"({media.codec}, bppf={bppf:.4f})"
            )
        else:
            print(
                f"Source         : {media.width}x{media.height} @ {media.fps:.3f} fps "
                f"({media.codec})"
            )
        if visual:
            print(
                f"Visual signals : edge={visual.edge_energy:.4f}, block={visual.blockiness:.2f}, "
                f"motion={visual.temporal_change:.4f}, frames={visual.sampled_frames}"
            )
        elif args.up_model == "auto":
            print("Visual signals : unavailable/disabled; metadata fallback active")

        if decision:
            print(f"AI selection   : {_selection_summary(decision)}")
            print(f"Why            : {decision.reason}")
        else:
            print(f"AI selection   : {selected_model} (manual override)")
        print(f"Output         : {output_file}")
        print(f"Command        : {core.shell_join(command)}")

        target = {
            "preset": args.preset,
            "width": width,
            "height": height,
            "fps": fps,
            "fi_model": args.fi_model,
        }

        if args.dry_run:
            continue

        started = time.perf_counter()
        try:
            subprocess.run(command, check=True)
        except subprocess.CalledProcessError as exc:
            failures += 1
            elapsed = time.perf_counter() - started
            log_job(
                args.log,
                source=media,
                input_file=input_file,
                output_file=output_file,
                command=command,
                selection=selection,
                target=target,
                encoder=encoder,
                status=f"failed:{exc.returncode}",
                elapsed_seconds=elapsed,
            )
            print(f"FAILED (exit {exc.returncode})", file=sys.stderr)
        else:
            elapsed = time.perf_counter() - started
            log_job(
                args.log,
                source=media,
                input_file=input_file,
                output_file=output_file,
                command=command,
                selection=selection,
                target=target,
                encoder=encoder,
                status="success",
                elapsed_seconds=elapsed,
            )
            print(f"Completed in {elapsed / 60:.1f} min")

    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())

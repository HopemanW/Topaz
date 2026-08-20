#!/usr/bin/env python3
"""Adaptive quality analysis and explainable Topaz model selection.

The module deliberately uses only the Python standard library plus FFmpeg/
FFprobe so the analysis layer stays portable and easy to audit.
"""

from __future__ import annotations

import json
import math
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable, Sequence


MODEL_PREFERENCES: dict[str, tuple[str, ...]] = {
    "proteus": ("prob-4", "prob-3", "prob-2"),
    # Iris versions are not monotonic quality upgrades: iris-3 is commonly used
    # for LQ, while iris-2 is often preferred for MQ material.
    "iris-lq": ("iris-3", "iris-2", "iris-1"),
    "iris-mq": ("iris-2", "iris-3", "iris-1"),
    "artemis-hq": ("ahq-12", "ahq-11", "ahq-10"),
    "artemis-mq": ("amq-13", "amq-12", "amq-10"),
    "artemis-lq": ("alq-13", "alq-12", "alq-10"),
    "artemis-aa": ("aaa-10", "aaa-9"),
}


@dataclass(frozen=True)
class MediaProfile:
    width: int
    height: int
    fps: float
    codec: str
    bit_rate_bps: int | None
    duration_seconds: float | None
    pixel_format: str
    field_order: str

    @property
    def pixels(self) -> int:
        return self.width * self.height

    @property
    def bits_per_pixel_frame(self) -> float | None:
        if not self.bit_rate_bps or self.fps <= 0 or self.pixels <= 0:
            return None
        return self.bit_rate_bps / (self.fps * self.pixels)


@dataclass(frozen=True)
class VisualMetrics:
    sampled_frames: int
    edge_energy: float
    laplacian_energy: float
    blockiness: float
    temporal_change: float


@dataclass(frozen=True)
class QualityAssessment:
    quality_score: float
    resolution_score: float
    bitrate_score: float
    sharpness_score: float
    block_score: float
    compression_risk: float


@dataclass(frozen=True)
class ModelDecision:
    model_id: str
    family: str
    quality_tier: str
    confidence: float
    reason: str
    assessment: QualityAssessment
    visual: VisualMetrics | None
    available_model_count: int

    def as_dict(self) -> dict:
        return asdict(self)


def clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def parse_fps(rate: str | None) -> float:
    if not rate or rate in {"0/0", "N/A"}:
        return 0.0
    if "/" in rate:
        num, den = rate.split("/", 1)
        denominator = float(den)
        return float(num) / denominator if denominator else 0.0
    return float(rate)


def _run_text(command: Sequence[str]) -> str:
    result = subprocess.run(
        list(command),
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return result.stdout


def probe_media(ffprobe: str, input_path: Path) -> MediaProfile:
    command = [
        ffprobe,
        "-v", "error",
        "-select_streams", "v:0",
        "-show_entries",
        "stream=width,height,codec_name,avg_frame_rate,bit_rate,pix_fmt,field_order:format=duration,bit_rate",
        "-of", "json",
        str(input_path),
    ]
    payload = json.loads(_run_text(command))
    streams = payload.get("streams", [])
    if not streams:
        raise RuntimeError(f"No video stream found in {input_path}")

    stream = streams[0]
    fmt = payload.get("format", {})
    raw_bitrate = stream.get("bit_rate") or fmt.get("bit_rate")
    raw_duration = fmt.get("duration")

    return MediaProfile(
        width=int(stream["width"]),
        height=int(stream["height"]),
        fps=parse_fps(stream.get("avg_frame_rate")),
        codec=str(stream.get("codec_name", "unknown")),
        bit_rate_bps=int(raw_bitrate) if raw_bitrate not in (None, "N/A") else None,
        duration_seconds=(
            float(raw_duration) if raw_duration not in (None, "N/A") else None
        ),
        pixel_format=str(stream.get("pix_fmt", "unknown")),
        field_order=str(stream.get("field_order", "unknown")),
    )


def discover_installed_models(model_dir: str | Path | None) -> set[str]:
    if not model_dir:
        return set()
    root = Path(model_dir)
    if not root.is_dir():
        return set()
    return {path.stem for path in root.glob("*.json") if path.is_file()}


def _frame_statistics(frame: bytes, width: int, height: int) -> tuple[float, float, float]:
    """Return edge energy, Laplacian energy, and 8px blockiness for one frame."""
    if len(frame) != width * height:
        raise ValueError("Unexpected raw frame size")

    data = frame
    edge_total = 0
    edge_count = 0
    boundary_total = 0
    boundary_count = 0
    interior_total = 0
    interior_count = 0
    lap_total = 0
    lap_count = 0

    # Horizontal differences. Classify 8-pixel boundaries separately so the
    # ratio is useful as a codec-blocking proxy.
    for y in range(height):
        row = y * width
        for x in range(1, width):
            diff = abs(data[row + x] - data[row + x - 1])
            edge_total += diff
            edge_count += 1
            if x % 8 == 0:
                boundary_total += diff
                boundary_count += 1
            elif x % 8 in {3, 4, 5}:
                interior_total += diff
                interior_count += 1

    # Vertical differences and a sparse Laplacian to keep the analyzer cheap.
    for y in range(1, height):
        row = y * width
        prev = (y - 1) * width
        for x in range(width):
            diff = abs(data[row + x] - data[prev + x])
            edge_total += diff
            edge_count += 1
            if y % 8 == 0:
                boundary_total += diff
                boundary_count += 1
            elif y % 8 in {3, 4, 5}:
                interior_total += diff
                interior_count += 1

    for y in range(1, height - 1, 2):
        row = y * width
        up = (y - 1) * width
        down = (y + 1) * width
        for x in range(1, width - 1, 2):
            center = data[row + x]
            lap = abs(
                4 * center
                - data[row + x - 1]
                - data[row + x + 1]
                - data[up + x]
                - data[down + x]
            )
            lap_total += lap
            lap_count += 1

    edge = (edge_total / max(edge_count, 1)) / 255.0
    laplacian = (lap_total / max(lap_count, 1)) / 1020.0
    boundary = boundary_total / max(boundary_count, 1)
    interior = interior_total / max(interior_count, 1)
    blockiness = boundary / max(interior, 1e-6)
    return edge, laplacian, blockiness


def visual_metrics_from_frames(frames: Sequence[bytes], width: int, height: int) -> VisualMetrics:
    if not frames:
        return VisualMetrics(0, 0.0, 0.0, 1.0, 0.0)

    edge_values: list[float] = []
    lap_values: list[float] = []
    block_values: list[float] = []
    temporal_values: list[float] = []

    previous: bytes | None = None
    for frame in frames:
        edge, lap, block = _frame_statistics(frame, width, height)
        edge_values.append(edge)
        lap_values.append(lap)
        block_values.append(block)
        if previous is not None:
            # Sparse frame-to-frame MAD; enough to separate static interviews
            # from high-motion material without adding NumPy/OpenCV.
            stride = 8
            change = sum(abs(frame[i] - previous[i]) for i in range(0, len(frame), stride))
            count = math.ceil(len(frame) / stride)
            temporal_values.append((change / max(count, 1)) / 255.0)
        previous = frame

    mean = lambda xs: sum(xs) / len(xs) if xs else 0.0
    return VisualMetrics(
        sampled_frames=len(frames),
        edge_energy=mean(edge_values),
        laplacian_energy=mean(lap_values),
        blockiness=mean(block_values),
        temporal_change=mean(temporal_values),
    )


def analyze_visual_quality(
    ffmpeg: str,
    input_path: Path,
    *,
    sample_frames: int = 8,
    width: int = 320,
    height: int = 180,
    interval_seconds: float = 5.0,
) -> VisualMetrics | None:
    """Sample low-resolution luma frames and compute lightweight quality signals.

    Returns None if the sampling command is unsupported or fails. Rendering can
    therefore continue even when the local FFmpeg build lacks a rawvideo path.
    """
    if sample_frames <= 0:
        return None
    fps_expr = 1.0 / max(interval_seconds, 0.1)
    command = [
        ffmpeg,
        "-hide_banner", "-loglevel", "error", "-nostdin",
        "-i", str(input_path),
        "-vf", f"fps={fps_expr:.8f},scale={width}:{height}:flags=bilinear,format=gray",
        "-frames:v", str(sample_frames),
        "-f", "rawvideo", "-pix_fmt", "gray", "pipe:1",
    ]
    try:
        result = subprocess.run(
            command,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except (OSError, subprocess.CalledProcessError):
        return None

    frame_size = width * height
    if frame_size <= 0 or len(result.stdout) < frame_size:
        return None
    count = min(sample_frames, len(result.stdout) // frame_size)
    frames = [
        result.stdout[i * frame_size:(i + 1) * frame_size]
        for i in range(count)
    ]
    return visual_metrics_from_frames(frames, width, height)


def _resolution_score(profile: MediaProfile) -> float:
    # Log-scaled against SD -> UHD so resolution is informative but does not
    # dominate the codec/visual evidence.
    sd_pixels = 640 * 480
    uhd_pixels = 3840 * 2160
    pixels = max(profile.pixels, sd_pixels)
    return clamp(math.log(pixels / sd_pixels) / math.log(uhd_pixels / sd_pixels))


def _codec_efficiency(codec: str) -> float:
    name = codec.lower()
    if name in {"av1", "av01"}:
        return 0.55
    if name in {"hevc", "h265", "h.265", "vp9"}:
        return 0.68
    if name in {"h264", "avc", "h.264"}:
        return 1.0
    if name in {"mpeg2video", "mpeg4", "vc1"}:
        return 1.25
    return 1.0


def _bitrate_score(profile: MediaProfile) -> float:
    bppf = profile.bits_per_pixel_frame
    if bppf is None:
        return 0.55
    # Approximate H.264-like thresholds, then adjust for more/less efficient
    # codecs. This is intentionally a heuristic, not a perceptual-quality claim.
    equivalent = bppf / _codec_efficiency(profile.codec)
    return clamp((equivalent - 0.025) / (0.14 - 0.025))


def assess_quality(profile: MediaProfile, visual: VisualMetrics | None) -> QualityAssessment:
    resolution = _resolution_score(profile)
    bitrate = _bitrate_score(profile)

    if visual and visual.sampled_frames:
        # Edge energy around 0.015 is very soft; >=0.075 is usually already rich
        # in local contrast at the 320x180 analysis scale.
        sharpness = clamp((visual.edge_energy - 0.015) / 0.060)
        # Block ratio ~=1 is neutral. Ratios >1.3 are increasingly suspicious.
        block_score = 1.0 - clamp((visual.blockiness - 1.02) / 0.55)
    else:
        sharpness = 0.55
        block_score = 0.55

    score = clamp(
        0.28 * resolution
        + 0.42 * bitrate
        + 0.16 * sharpness
        + 0.14 * block_score
    )
    compression_risk = clamp(
        0.62 * (1.0 - bitrate)
        + 0.38 * (1.0 - block_score)
    )
    return QualityAssessment(
        quality_score=score,
        resolution_score=resolution,
        bitrate_score=bitrate,
        sharpness_score=sharpness,
        block_score=block_score,
        compression_risk=compression_risk,
    )


def resolve_model_id(family: str, available_models: Iterable[str] | None = None) -> tuple[str, bool]:
    preferences = MODEL_PREFERENCES[family]
    available = set(available_models or ())
    if not available:
        return preferences[0], False
    for candidate in preferences:
        if candidate in available:
            return candidate, True

    # If the exact preferred family is unavailable, choose an installed Proteus
    # as a safe general-purpose fallback before returning an unresolved default.
    for candidate in MODEL_PREFERENCES["proteus"]:
        if candidate in available:
            return candidate, True
    return preferences[0], False


def select_model(
    profile: MediaProfile,
    visual: VisualMetrics | None,
    *,
    content: str = "auto",
    available_models: Iterable[str] | None = None,
) -> ModelDecision:
    if content not in {"auto", "general", "faces", "animation"}:
        raise ValueError("content must be auto, general, faces, or animation")

    assessment = assess_quality(profile, visual)
    q = assessment.quality_score
    compression = assessment.compression_risk

    if content == "animation":
        family, tier = "artemis-aa", "aliasing"
        reason = "animation hint favors Artemis Aliasing/Moire for edge and aliasing cleanup"
    elif content == "faces" and q < 0.78:
        family = "iris-lq" if q < 0.46 else "iris-mq"
        tier = "low" if q < 0.46 else "medium"
        reason = "face-oriented low/medium quality input favors Iris face/compression recovery"
    elif compression >= 0.72 and q < 0.48:
        family, tier = "iris-lq", "low"
        reason = "high compression risk and low quality favor Iris artifact recovery"
    elif q < 0.34:
        family, tier = "artemis-lq", "low"
        reason = "very low overall quality favors the aggressive Artemis LQ variant"
    elif q < 0.52:
        family, tier = "artemis-mq", "medium"
        reason = "medium-low quality favors balanced Artemis MQ cleanup"
    elif q < 0.80:
        family, tier = "proteus", "medium-high"
        reason = "mixed/medium-high quality favors tunable general-purpose Proteus"
    else:
        family, tier = "artemis-hq", "high"
        reason = "high-quality source favors lighter Artemis HQ enhancement"

    model_id, installed_match = resolve_model_id(family, available_models)
    available_count = len(set(available_models or ()))
    if available_count and not installed_match:
        reason += f"; preferred family was not discovered locally, using configured fallback {model_id}"
    elif available_count:
        reason += f"; selected installed model {model_id}"

    # Confidence increases away from decision boundaries and when visual samples
    # are present. It is an explainability aid, not a calibrated probability.
    boundaries = (0.34, 0.48, 0.52, 0.78, 0.80)
    boundary_distance = min(abs(q - b) for b in boundaries)
    confidence = clamp(0.58 + min(boundary_distance, 0.18) * 1.6)
    if visual and visual.sampled_frames >= 4:
        confidence = clamp(confidence + 0.08)
    if content in {"faces", "animation"}:
        confidence = clamp(confidence + 0.08)

    return ModelDecision(
        model_id=model_id,
        family=family,
        quality_tier=tier,
        confidence=confidence,
        reason=reason,
        assessment=assessment,
        visual=visual,
        available_model_count=available_count,
    )

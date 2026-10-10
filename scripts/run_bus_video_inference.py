"""
file_path: scripts/run_bus_video_inference.py

녹화된 MP4·WebM 영상에 실시간 버스 번호 인식 파이프라인을 적용한다.
실시간 화면과 같은 버스 박스·번호 카드 오버레이와 프레임별 JSONL을 저장한다.

[영상 테스트 실행]
# sample1 폴더에 있는 모든 MP4·WebM을 143번 버스 기준으로 처리
python -m scripts.run_bus_video_inference --sample-dir data/samples/input/sample1 --target-route 143

[영상 테스트 결과물]
data/samples/output/샘플폴더명/에 result_bus_원본파일명.mp4를 저장한다.
샘플폴더명/jsonl/에 result_bus_원본파일명.bus.jsonl을 저장한다.
"""
from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import subprocess
import tempfile
import uuid

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

from backend.bus.base import InferenceContext, ModelSpec
from backend.bus.config import DEFAULT_BUS_CONFIG, load_bus_config
from backend.bus.pipeline import BusPipeline
from backend.bus.recognition import normalize_route
from src.pipeline import find_sample_videos, resolve_path
from src.video_audio import ffmpeg_executable


PROJECT_DIR = Path(__file__).resolve().parents[1]
VIDEO_SUFFIXES = {".mp4", ".webm"}
FONT_CANDIDATES = (
    Path("/mnt/c/Windows/Fonts/malgunbd.ttf"),
    Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc"),
    Path("/usr/share/fonts/truetype/nanum/NanumGothicBold.ttf"),
    Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
)


# 버스 JSONL 경로 계산
def bus_log_path(video_output: Path) -> Path:
    """
    결과 MP4와 같은 폴더의 jsonl 하위에 버스 인식 로그 경로를 만든다.
    """
    return video_output.parent / "jsonl" / f"{video_output.stem}.bus.jsonl"


# 기존 결과를 보존할 출력 경로 선택
def next_available_output(output_path: Path, reserved: set[Path]) -> Path:
    """
    같은 이름의 영상이나 버스 JSONL이 있으면 번호를 올려 새 출력 경로를 선택한다.
    """
    number = 0
    while True:
        candidate = (output_path if number == 0
                     else output_path.with_name(f"{output_path.stem}({number}){output_path.suffix}"))
        if candidate not in reserved and not candidate.exists() and not bus_log_path(candidate).exists():
            return candidate
        number += 1


# 한글 표시용 글꼴 선택
def resolve_font_path(value: Path | None) -> Path:
    """
    사용자가 지정한 글꼴이나 실행 환경에서 찾은 한글 글꼴을 반환한다.
    """
    if value is not None:
        path = resolve_path(value)
        if not path.is_file():
            raise FileNotFoundError(f"글꼴 파일이 없습니다: {path}")
        return path
    for path in FONT_CANDIDATES:
        if path.is_file():
            return path
    raise FileNotFoundError("한글 오버레이 글꼴이 없습니다. --font-path로 TTF/TTC를 지정하세요.")


# 현재 프레임에서 표시할 번호 근거 선택
def select_evidence(event: dict, target_route: str) -> dict | None:
    """
    실시간 오버레이와 같은 우선순위로 목표 번호와 다른 노선의 표시 근거를 고른다.
    """
    evidence = [
        item for item in [*(event.get("recognized_routes") or []), *(event.get("matches") or [])]
        if item.get("route_number")
        and item.get("state") in {"recognized_single", "matched_candidate"}
    ]

    def ranking(item: dict) -> tuple[int, int, float]:
        """목표 여부, 반복 확정 여부, 토큰 점수 순으로 정렬 값을 만든다."""
        is_target = item.get("is_target") is not False and str(item["route_number"]) == target_route
        return (int(is_target), int(item.get("state") == "matched_candidate"),
                float(item.get("token_score") or 0.0))

    return max(evidence, key=ranking) if evidence else None


# 실시간 버스 카드와 탐지 박스 그리기
def draw_bus_overlay(frame, result: dict, target_route: str, age_ms: float,
                     shown: dict | None, font_path: Path):
    """
    실시간 Canvas의 색상·배치·문구를 녹화 프레임 위에 같은 비율로 그린다.
    """
    rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
    image = Image.fromarray(rgb).convert("RGBA")
    layer = Image.new("RGBA", image.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    width, height = image.size
    scale = width / 360.0
    event = result.get("event") or {}
    detections = result.get("detections") or []
    failed = bool(event.get("errors")) or result.get("status") in {"error", "unavailable", "disabled"}
    preparing = result.get("status") in {"loading", "idle"}
    fresh = math.isfinite(age_ms) and 0 <= age_ms <= 3000
    mint, blue, amber, muted = "#21d7bb", "#7cbdff", "#ffd166", "#b9c5d5"

    def font(size: float):
        """화면 폭 비율을 적용한 굵은 글꼴을 만든다."""
        return ImageFont.truetype(str(font_path), max(8, round(size * scale)))

    def fitted_font(text: str, size: float, maximum: float):
        """지정 폭 안에 들어갈 때까지 실시간 화면처럼 글꼴 크기를 줄인다."""
        current = size
        selected = font(current)
        while draw.textbbox((0, 0), text, font=selected)[2] > maximum and current > 8:
            current -= 1
            selected = font(current)
        return selected

    def is_target(item: dict) -> bool:
        """표시 근거가 사용자가 입력한 목표 노선인지 확인한다."""
        return item.get("is_target") is not False and str(item.get("route_number")) == target_route

    checking = (not failed and not preparing and fresh
                and (any(item.get("class_name") == "bus" or item.get("class_id") == 0
                         for item in detections) or bool(event.get("buses"))))
    if not failed and not preparing and 0 <= age_ms <= 400:
        evidence = [*(event.get("recognized_routes") or []), *(event.get("matches") or [])]
        for item in detections:
            box = item.get("box") or {}
            values = [box.get(key) for key in ("x1", "y1", "x2", "y2")]
            if not all(isinstance(value, (int, float)) and math.isfinite(value) for value in values):
                continue
            x1, y1, x2, y2 = [max(0.0, min(1.0, float(value))) for value in values]
            x, y = x1 * width, y1 * height
            right, bottom = x2 * width, y2 * height
            is_number = item.get("class_name") == "route_number" or item.get("class_id") == 1
            match = next((entry for entry in evidence if entry.get("track_id") is not None
                          and entry.get("track_id") == item.get("track_id")), None)
            color = mint if match and is_target(match) else amber if match else blue
            line = max(1, round((1.5 if is_number else 3) * scale))
            draw.rounded_rectangle((x, y, right, bottom), radius=(2 if is_number else 6) * scale,
                                   outline="white" if is_number else color, width=line)
            if is_number:
                continue
            label = str(match["route_number"]) if match else "버스 번호 확인 중"
            label_font = fitted_font(label, 32 if match else 15, width - 32 * scale)
            bounds = draw.textbbox((0, 0), label, font=label_font)
            label_width = min(width - 16 * scale, bounds[2] + 16 * scale)
            label_height = (44 if match else 27) * scale
            label_x = max(8 * scale, min(x, width - label_width - 8 * scale))
            label_y = min(height - label_height - 8 * scale,
                          max(130 * scale, y - label_height - 5 * scale))
            draw.rounded_rectangle((label_x, label_y, label_x + label_width, label_y + label_height),
                                   radius=6 * scale, fill=color)
            draw.text((label_x + 8 * scale, label_y + label_height - 8 * scale), label,
                      font=label_font, fill="#071e23", anchor="ls")

    x, y, card_width = 12 * scale, 12 * scale, width - 24 * scale
    card_height, padding = 108 * scale, 12 * scale
    accent = (mint if shown and is_target(shown) else amber if shown
              else blue if checking else muted)
    draw.rounded_rectangle((x, y, x + card_width, y + card_height), radius=12 * scale,
                           fill=(9, 20, 36, 240), outline=accent, width=max(1, round(1.5 * scale)))
    heading = ("인식한 버스 번호" if shown and is_target(shown) else "다른 버스 번호"
               if shown else "버스 번호 인식")
    draw.text((x + padding, y + 22 * scale), heading, font=font(11), fill="#edf3fa", anchor="ls")
    if target_route:
        target_label = f"찾는 번호 {target_route}"
        target_font = fitted_font(target_label, 11, card_width * 0.48)
        target_box = draw.textbbox((0, 0), target_label, font=target_font)
        draw.text((x + card_width - padding - target_box[2], y + 22 * scale), target_label,
                  font=target_font, fill=muted, anchor="ls")
    title = (str(shown["route_number"]) if shown else "번호 인식을 사용할 수 없어요"
             if failed else "번호 인식을 준비하고 있어요" if preparing
             else "버스 번호 확인 중" if checking
             else "번호를 다시 확인하고 있어요" if not fresh else "버스를 찾고 있어요")
    confirmed = bool(shown and is_target(shown) and shown.get("state") == "matched_candidate")
    detail = (None if confirmed else f"{shown['route_number']}번 버스 인식 중"
              if shown and is_target(shown) else f"다른 노선 · 목표 {target_route}번"
              if shown else "번호 인식 상태를 확인해 주세요" if failed
              else "잠시만 기다려 주세요" if preparing else "번호를 읽고 있어요"
              if checking else "버스 방향으로 유지해 주세요")
    title_size = 56 if confirmed else 52 if shown else 23
    title_y = 88 if confirmed else 78 if shown else 64
    title_font = fitted_font(title, title_size, card_width - padding * 2)
    draw.text((x + padding, y + title_y * scale), title, font=title_font, fill=accent, anchor="ls")
    if detail:
        detail_font = fitted_font(detail, 11, card_width - padding * 2)
        draw.text((x + padding, y + (99 if shown else 88) * scale), detail,
                  font=detail_font, fill="#edf3fa", anchor="ls")
    rendered = Image.alpha_composite(image, layer).convert("RGB")
    return cv2.cvtColor(np.asarray(rendered), cv2.COLOR_RGB2BGR)


# 입력 영상 타이밍 검사
def inspect_video_timing(video_path: Path) -> tuple[int, float, float]:
    """
    모든 프레임의 원본 PTS를 읽어 프레임 수, 시작 PTS, 실제 길이 기준 FPS를 계산한다.
    """
    capture = cv2.VideoCapture(str(video_path))
    timestamps = []
    try:
        if not capture.isOpened():
            raise RuntimeError(f"영상을 열 수 없습니다: {video_path}")
        while True:
            ok, _frame = capture.read()
            if not ok:
                break
            timestamp = capture.get(cv2.CAP_PROP_POS_MSEC)
            if math.isfinite(timestamp) and timestamp >= 0:
                timestamps.append(float(timestamp))
            elif timestamps:
                timestamps.append(timestamps[-1])
            else:
                timestamps.append(0.0)
    finally:
        capture.release()
    if not timestamps:
        raise ValueError(f"처리할 프레임이 없습니다: {video_path}")
    positive_gaps = [right - left for left, right in zip(timestamps, timestamps[1:])
                     if right > left]
    if not positive_gaps:
        raise ValueError(f"영상 프레임 타임스탬프가 올바르지 않습니다: {video_path}")
    typical_gap_ms = float(np.median(positive_gaps))
    duration_ms = timestamps[-1] - timestamps[0] + typical_gap_ms
    if not math.isfinite(duration_ms) or duration_ms <= 0:
        raise ValueError(f"영상 재생시간이 올바르지 않습니다: {video_path}")
    return len(timestamps), timestamps[0], len(timestamps) * 1000.0 / duration_ms


# 원본 오디오를 결과 영상에 결합
def mux_source_audio(video_path: Path, rendered_path: Path, output_path: Path) -> None:
    """
    원본 영상에 오디오가 있으면 재생 길이를 유지해 오버레이 영상에 다시 결합한다.
    """
    command = [
        ffmpeg_executable(), "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
        "-i", str(rendered_path), "-i", str(video_path),
        "-map", "0:v:0", "-map", "1:a:0?", "-c:v", "copy", "-c:a", "aac",
        "-shortest", "-movflags", "+faststart", str(output_path),
    ]
    result = subprocess.run(command, capture_output=True, text=True, timeout=300)
    if result.returncode:
        raise RuntimeError(result.stderr[-1000:] or "원본 오디오 결합에 실패했습니다.")


# 영상 한 개 버스 인식
def process_video(video_path: Path, output_path: Path, pipeline: BusPipeline,
                  target_route: str, interval_ms: int, font_path: Path) -> int:
    """
    영상 프레임을 실시간 주기로 추론하고 오버레이 MP4와 JSONL을 원자적으로 공개한다.
    """
    capture = cv2.VideoCapture(str(video_path))
    writer = None
    video_temporary = None
    mux_temporary = None
    log_temporary = None
    session_id = f"recorded-{uuid.uuid4().hex}"
    processed = 0
    last_inference_ms = -interval_ms
    latest = {"status": "searching", "event": None, "detections": []}
    latest_at_ms = None
    shown = None
    shown_at_ms = None
    log_path = bus_log_path(output_path)
    try:
        if not capture.isOpened():
            raise RuntimeError(f"영상을 열 수 없습니다: {video_path}")
        expected_frames, first_pts_ms, output_fps = inspect_video_timing(video_path)
        width = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH))
        height = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT))
        if width <= 0 or height <= 0 or not math.isfinite(output_fps) or output_fps <= 0:
            raise ValueError(f"영상 크기 또는 FPS가 올바르지 않습니다: {video_path}")
        output_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=output_path.parent, prefix=f".{output_path.stem}.",
                                         suffix=".partial.mp4", delete=False) as temporary:
            video_temporary = Path(temporary.name)
        with tempfile.NamedTemporaryFile(dir=output_path.parent, prefix=f".{output_path.stem}.mux.",
                                         suffix=".partial.mp4", delete=False) as temporary:
            mux_temporary = Path(temporary.name)
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=log_path.parent,
                                         prefix=f".{log_path.stem}.", suffix=".partial.jsonl",
                                         delete=False) as log_file:
            log_temporary = Path(log_file.name)
            writer = cv2.VideoWriter(str(video_temporary), cv2.VideoWriter_fourcc(*"mp4v"),
                                     output_fps, (width, height))
            if not writer.isOpened():
                raise RuntimeError(f"결과 영상을 생성할 수 없습니다: {output_path}")
            pipeline.configure_target_route(target_route)
            pipeline.reset_session(session_id)
            frame_id = 0
            while True:
                ok, frame = capture.read()
                if not ok:
                    break
                frame_id += 1
                source_pts_ms = capture.get(cv2.CAP_PROP_POS_MSEC)
                if math.isfinite(source_pts_ms) and source_pts_ms >= first_pts_ms:
                    captured_at_ms = round(source_pts_ms - first_pts_ms)
                else:
                    captured_at_ms = round((frame_id - 1) * 1000 / output_fps)
                if captured_at_ms - last_inference_ms >= interval_ms:
                    latest = pipeline.infer(
                        frame, InferenceContext(session_id, frame_id, captured_at_ms)
                    )
                    latest = {**latest, "status": "searching", "captured_at_ms": captured_at_ms,
                              "frame_id": frame_id}
                    latest_at_ms = captured_at_ms
                    last_inference_ms = captured_at_ms
                    selected = select_evidence(latest.get("event") or {}, target_route)
                    if selected is not None:
                        shown, shown_at_ms = selected, captured_at_ms
                if shown_at_ms is not None and captured_at_ms - shown_at_ms > 3000:
                    shown, shown_at_ms = None, None
                age_ms = captured_at_ms - latest_at_ms if latest_at_ms is not None else math.inf
                writer.write(draw_bus_overlay(frame, latest, target_route, age_ms, shown, font_path))
                log_file.write(json.dumps({
                    "source_video": str(video_path), "target_route": target_route,
                    "video_frame_id": frame_id, "video_time_ms": captured_at_ms,
                    "source_pts_ms": source_pts_ms if math.isfinite(source_pts_ms) else None,
                    "output_fps": output_fps,
                    "ocr_result_frame_id": latest.get("frame_id"),
                    "ocr_result_time_ms": latest.get("captured_at_ms"),
                    "ocr_result_age_ms": age_ms if math.isfinite(age_ms) else None,
                    "bus": latest,
                }, ensure_ascii=False, allow_nan=False) + "\n")
                processed += 1
        if processed == 0:
            raise ValueError(f"처리할 프레임이 없습니다: {video_path}")
        if processed != expected_frames:
            raise RuntimeError(f"입력 프레임 수가 처리 중 변경되었습니다: {expected_frames} -> {processed}")
        writer.release()
        writer = None
        mux_source_audio(video_path, video_temporary, mux_temporary)
        os.link(mux_temporary, output_path)
        os.link(log_temporary, log_path)
        mux_temporary.unlink()
        log_temporary.unlink()
        print(f"결과 영상 저장: {output_path}")
        print(f"버스 JSONL 저장: {log_path}")
        return processed
    finally:
        capture.release()
        if writer is not None:
            writer.release()
        pipeline.close_session(session_id)
        if video_temporary is not None:
            video_temporary.unlink(missing_ok=True)
        if mux_temporary is not None:
            mux_temporary.unlink(missing_ok=True)
        if log_temporary is not None:
            log_temporary.unlink(missing_ok=True)


# 명령행 입력과 출력 경로 준비
def main() -> None:
    """
    run_video_inference.py와 같은 입력·출력 옵션으로 버스 영상 추론을 실행한다.
    """
    parser = argparse.ArgumentParser(description="녹화영상 버스 번호 인식 및 실시간형 오버레이 생성")
    parser.add_argument("--config", type=Path, default=DEFAULT_BUS_CONFIG)
    inputs = parser.add_mutually_exclusive_group()
    inputs.add_argument("--video-path", type=Path, help="입력 영상 한 개")
    inputs.add_argument("--sample-dir", type=Path, help="모든 MP4·WebM을 처리할 샘플 폴더")
    outputs = parser.add_mutually_exclusive_group()
    outputs.add_argument("--output-path", type=Path, help="영상 한 개의 결과 MP4 경로")
    outputs.add_argument("--output-dir", type=Path, help="결과를 저장할 폴더")
    parser.add_argument("--target-route", required=True, help="찾을 버스 번호(예: 143, N26, 마포07)")
    parser.add_argument("--font-path", type=Path, help="오버레이용 한글 TTF/TTC 경로")
    args = parser.parse_args()

    target_route = normalize_route(args.target_route)
    config = load_bus_config(args.config)
    videos = ([resolve_path(args.video_path)] if args.video_path is not None
              else find_sample_videos(args.sample_dir or "data/samples/input"))
    if args.output_path is not None and len(videos) != 1:
        raise ValueError("--output-path는 영상 한 개 처리 시에만 지정할 수 있습니다.")
    destination = resolve_path(args.output_dir or "data/samples/output")
    requested = [
        resolve_path(args.output_path) if args.output_path is not None
        else destination / video.parent.name / f"result_bus_{video.stem}.mp4"
        for video in videos
    ]
    reserved: set[Path] = set()
    outputs_resolved = []
    for video, output in zip(videos, requested):
        if not video.is_file() or video.suffix.lower() not in VIDEO_SUFFIXES:
            raise FileNotFoundError(f"입력 MP4·WebM 영상이 없습니다: {video}")
        if output.suffix.lower() != ".mp4":
            raise ValueError(f"결과 영상 확장자는 .mp4여야 합니다: {output}")
        if video.resolve() == output.resolve():
            raise ValueError(f"입력 영상을 결과 경로로 덮어쓸 수 없습니다: {video}")
        selected = next_available_output(output, reserved)
        reserved.add(selected)
        outputs_resolved.append(selected)

    font_path = resolve_font_path(args.font_path)
    pipeline = BusPipeline(
        ModelSpec(id="bus-route-display-b", mode="bus", name="Bus B",
                  version="route-display-b", weights=config.route_display),
        {"bus_detector": config.bus_detector, "parseq_weights": config.parseq_weights,
         "parseq_source": config.parseq_source},
    )
    pipeline.load()
    for index, (video, output) in enumerate(zip(videos, outputs_resolved), 1):
        print(f"입력 영상 [{index}/{len(videos)}]: {video}")
        process_video(video, output, pipeline, target_route, config.interval_ms, font_path)


if __name__ == "__main__":
    main()

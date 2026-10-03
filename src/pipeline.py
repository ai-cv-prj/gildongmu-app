"""
file_path: src/pipeline.py

같은 원본 프레임의 도보·장애물·신호등 추론 결과를 MP4로 저장한다.
모델은 한 번만 로딩하며, 단독 실행과 통합 실행을 지원한다.
"""

import math
import os
import tempfile
import warnings
from pathlib import Path

import cv2

from src.settings import DEFAULT_PATHS_CONFIG, load_paths, read_yaml
from src.sidewalk import SidewalkSegmenter
from src.obstacle import ObstacleDetector, validate_yolo_config
from src.visualization import draw_detections, overlay_segmentation, draw_traffic
from src.traffic import TrafficSignalPipeline, validate_traffic_config
from src.risk import RiskEngine, VideoClock
from src.risk_config import risk_config as normalize_risk, tracking_config as normalize_tracking
from src.risk_visualization import draw_risk
from src.risk_log import RiskLog
from src.walking_voice import WalkingVoice, suppress_non_green_crosswalk_voice
from src.traffic_voice import TrafficVoice
from src.video_audio import render_voice_track, mux_voice
from src.crosswalk_safety import (
    CrosswalkSafetyEngine, crosswalk_camera_stable,
    crosswalk_safety_config as normalize_crosswalk,
)
from src.crosswalk_visualization import draw_crosswalk_safety
from src.walking_surface import WalkingSurfaceEngine, walking_surface_config
from src.walking_surface_visualization import draw_walking_surface
from src.voice_priority import CrosswalkVoice, prioritize_voice_events


PROJECT_DIR = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = PROJECT_DIR / "configs" / "inference.yaml"


# 프로젝트 기준 경로 해석
def resolve_path(value):
    """상대 경로는 통합 레포 루트, 절대 경로는 지정 위치로 해석한다."""
    path = Path(value).expanduser()
    return (PROJECT_DIR / path).resolve()


# YAML 설정 읽기
def load_config(config_path):
    """모델별 가중치 경로와 공통 추론 설정을 확인한다."""
    config = read_yaml(config_path)
    paths = load_paths(config.get("paths_config", DEFAULT_PATHS_CONFIG))
    for key in ("sample_dir", "output_dir", "session_dir"):
        config.setdefault(key, paths[key])
    config.setdefault("mask2former", {"weights": paths["mask2former_weights"]})
    for name in ("yolo", "traffic"):
        settings = config.setdefault(name, {})
        if isinstance(settings, dict):
            settings.setdefault("weights", paths[f"{name}_weights"])
    if isinstance(config["traffic"], dict):
        config["traffic"].setdefault("classifier_weights", paths["traffic_classifier_weights"])
    required = {"mask2former", "sample_dir", "output_dir", "device", "overlay_alpha"}
    if not isinstance(config, dict) or not required.issubset(config):
        raise ValueError(f"설정에 필요한 항목: {', '.join(sorted(required))}")
    mask2former_config = config["mask2former"]
    if not isinstance(mask2former_config, dict):
        raise ValueError("mask2former 설정에는 weights 항목이 필요합니다.")
    mask2former_weights = mask2former_config.get("weights")
    if not isinstance(mask2former_weights, str) or not mask2former_weights.strip():
        raise ValueError("mask2former.weights에는 비어 있지 않은 폴더 경로를 지정하세요.")
    for key in ("sample_dir", "output_dir"):
        if not isinstance(config[key], str) or not config[key].strip():
            raise ValueError(f"{key}에는 비어 있지 않은 경로를 지정하세요.")
    if config["device"] not in ("auto", "cpu", "cuda"):
        raise ValueError("device는 auto, cpu, cuda 중 하나여야 합니다.")
    alpha = config["overlay_alpha"]
    if isinstance(alpha, bool) or not isinstance(alpha, (int, float)) or not 0 <= alpha <= 1:
        raise ValueError("overlay_alpha는 0부터 1 사이의 숫자여야 합니다.")
    recorded_frame = config.get("recorded_frame")
    if not isinstance(recorded_frame, dict):
        raise ValueError("recorded_frame 설정에는 max_side와 jpeg_quality가 필요합니다.")
    max_side = recorded_frame.get("max_side")
    jpeg_quality = recorded_frame.get("jpeg_quality")
    if isinstance(max_side, bool) or not isinstance(max_side, int) or max_side < 1:
        raise ValueError("recorded_frame.max_side는 양의 정수여야 합니다.")
    if (isinstance(jpeg_quality, bool)
            or not isinstance(jpeg_quality, (int, float))
            or not 0 < jpeg_quality <= 1):
        raise ValueError("recorded_frame.jpeg_quality는 0보다 크고 1 이하여야 합니다.")
    return config


# 녹화 프레임을 실시간 전송 조건으로 변환
def prepare_recorded_frame(frame, settings):
    """비율을 유지해 최대 변을 줄이고 지정 품질의 JPEG를 거친 BGR 프레임을 반환한다."""
    if settings is None:
        return frame
    height, width = frame.shape[:2]
    scale = min(1.0, settings["max_side"] / max(width, height))
    target_width = max(1, round(width * scale))
    target_height = max(1, round(height * scale))
    resized = cv2.resize(frame, (target_width, target_height), interpolation=cv2.INTER_AREA)
    quality = round(settings["jpeg_quality"] * 100)
    encoded, payload = cv2.imencode(".jpg", resized, [cv2.IMWRITE_JPEG_QUALITY, quality])
    if not encoded:
        raise RuntimeError("녹화 프레임을 JPEG로 변환하지 못했습니다.")
    decoded = cv2.imdecode(payload, cv2.IMREAD_COLOR)
    if decoded is None:
        raise RuntimeError("JPEG 녹화 프레임을 다시 읽지 못했습니다.")
    return decoded


# 샘플 MP4 조회
def find_sample_videos(sample_dir):
    """지정 폴더 바로 아래의 모든 MP4를 파일명 순으로 조회한다."""
    sample_dir = resolve_path(sample_dir)
    if not sample_dir.is_dir():
        raise FileNotFoundError(f"샘플 폴더가 없습니다: {sample_dir}")
    videos = sorted(
        path for path in sample_dir.iterdir()
        if path.is_file() and path.suffix.lower() == ".mp4"
    )
    if not videos:
        raise FileNotFoundError(f"샘플 MP4 영상이 없습니다: {sample_dir}")
    return videos


# 완성된 결과 파일을 기존 결과와 교체
def publish_video_result(video_source, output_path, risk_log=None):
    """MP4와 JSONL을 교체하고 공개 오류가 나면 이전 결과를 복구한다."""
    risk_path = output_path.with_suffix(".risk.jsonl")
    targets = (output_path, risk_path)
    with tempfile.TemporaryDirectory(dir=output_path.parent, prefix=f".{output_path.stem}.backup.") as folder:
        backups = {}
        for target in targets:
            if target.exists():
                backup = Path(folder) / target.name
                os.link(target, backup)
                backups[target] = backup
        changed = []
        try:
            os.replace(video_source, output_path)
            changed.append(output_path)
            if risk_log is not None:
                os.replace(risk_log.temporary, risk_path)
                changed.append(risk_path)
            elif risk_path.exists():
                risk_path.unlink()
                changed.append(risk_path)
        except BaseException:
            for target in reversed(changed):
                backup = backups.get(target)
                if backup is None:
                    target.unlink(missing_ok=True)
                else:
                    os.replace(backup, target)
            raise


# 영상 한 개 처리
def process_video(video_path, output_path, segmenter=None, alpha=0.55, detector=None, traffic=None,
                  risk_config=None, tracking_config=None, crosswalk_config=None,
                  recorded_frame_config=None, walking_surface_config_value=None):
    """임시 MP4로 처리한 뒤 프레임 수 확인에 성공하면 이전 결과를 교체한다."""
    if segmenter is None and detector is None and traffic is None:
        raise ValueError("도보, 장애물 또는 신호등 모델이 하나 이상 필요합니다.")
    video_path, output_path = Path(video_path), Path(output_path)
    if video_path.resolve() == output_path.resolve():
        raise ValueError("입력 영상을 결과 경로로 덮어쓸 수 없습니다.")
    if not output_path.parent.is_dir():
        raise FileNotFoundError(f"출력 폴더가 없습니다: {output_path.parent}")
    if output_path.suffix.lower() != ".mp4":
        raise ValueError("결과 영상 확장자는 .mp4여야 합니다.")

    risk_settings = normalize_risk(risk_config)
    risk_enabled = risk_settings["enabled"] and detector is not None
    capture = cv2.VideoCapture(str(video_path))
    writer = None
    temporary_path = None
    processed_frames = 0
    risk_log = None
    voice = None
    signal_voice = None
    crosswalk_voice = None
    walking_surface_voice = None
    temporary_wav = None
    temporary_mux = None
    committed = False
    engine = None
    crosswalk_engine = None
    walking_surface_engine = None
    try:
        if not capture.isOpened():
            raise RuntimeError(f"영상을 열 수 없습니다: {video_path}")
        width = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH))
        height = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT))
        fps = capture.get(cv2.CAP_PROP_FPS)
        reported_frames = capture.get(cv2.CAP_PROP_FRAME_COUNT)
        total_frames = (
            int(reported_frames)
            if math.isfinite(reported_frames) and reported_frames >= 1 else None
        )
        if width <= 0 or height <= 0 or not math.isfinite(fps) or fps <= 0:
            raise ValueError(f"영상 크기 또는 FPS가 올바르지 않습니다: {video_path}")
        output_scale = (min(1.0, recorded_frame_config["max_side"] / max(width, height))
                        if recorded_frame_config is not None else 1.0)
        output_width = max(1, round(width * output_scale))
        output_height = max(1, round(height * output_scale))
        if total_frames is None:
            warnings.warn(
                f"전체 프레임 수를 알 수 없어 누락 여부를 검증할 수 없습니다: {video_path}",
                RuntimeWarning,
                stacklevel=2,
            )

        # 같은 출력 폴더에 이번 작업 전용 임시 파일 생성
        with tempfile.NamedTemporaryFile(
            dir=output_path.parent,
            prefix=f".{output_path.stem}.",
            suffix=".partial.mp4",
            delete=False,
        ) as temporary_file:
            temporary_path = Path(temporary_file.name)
        writer = cv2.VideoWriter(
            str(temporary_path), cv2.VideoWriter_fourcc(*"mp4v"), fps,
            (output_width, output_height),
        )
        if not writer.isOpened():
            raise RuntimeError(f"결과 영상을 생성할 수 없습니다: {output_path}")

        if traffic is not None:
            traffic.reset()
            signal_voice = TrafficVoice()

        crosswalk_settings = normalize_crosswalk(crosswalk_config)
        if crosswalk_settings["enabled"] and segmenter is not None and traffic is not None:
            crosswalk_engine = CrosswalkSafetyEngine(crosswalk_settings)
            crosswalk_voice = CrosswalkVoice()
        walking_surface_settings = walking_surface_config(walking_surface_config_value)
        if walking_surface_settings["enabled"] and segmenter is not None:
            walking_surface_engine = WalkingSurfaceEngine(walking_surface_settings)
            walking_surface_voice = CrosswalkVoice(source="walking_surface", priority=3)

        if risk_enabled:
            engine = RiskEngine(risk_settings, tracking_config)
            clock = VideoClock(fps)
            voice = WalkingVoice()
            if risk_settings["log_jsonl"]:
                risk_log = RiskLog(output_path, overwrite=True)

        while True:
            success, frame = capture.read()
            if not success:
                break
            frame = prepare_recorded_frame(frame, recorded_frame_config)
            # 모든 모델이 색칠 전의 같은 JPEG 변환 프레임 사용
            detections = detector.predict(frame) if detector is not None else []
            class_map = segmenter.predict(frame) if segmenter is not None else None
            risk_result = None
            if engine is not None:
                timestamp, valid_time, time_source = clock.read(
                    capture.get(cv2.CAP_PROP_POS_MSEC), processed_frames)
                risk_result = engine.update(frame, detections, timestamp, valid_time, class_map,
                    segmenter.label_ids if segmenter is not None else None)
                risk_result.update(frame_index=processed_frames, timestamp_source=time_source)
                if processed_frames == 0:
                    risk_result.update(risk_config=risk_settings,
                                       tracking_config=normalize_tracking(tracking_config))
            if risk_result is not None:
                engine.add_sidewalk_context(risk_result, class_map,
                    segmenter.label_ids if segmenter is not None else None, frame.shape)
            traffic_result = traffic.predict(
                frame, frame_id=processed_frames + 1,
                captured_at_ms=processed_frames * 1000 / fps,
            ) if traffic is not None else None
            if risk_result is not None:
                suppress_non_green_crosswalk_voice(
                    risk_result, traffic_result, class_map,
                    segmenter.label_ids if segmenter is not None else None,
                    frame.shape, crosswalk_settings,
                )
            if traffic_result is not None:
                signal_voice.observe(traffic_result, processed_frames + 1, processed_frames / fps)
            crosswalk_result = None
            if crosswalk_engine is not None:
                camera_stable = crosswalk_camera_stable(risk_result)
                crosswalk_result = crosswalk_engine.update(
                    class_map, segmenter.label_ids, frame.shape, traffic_result,
                    processed_frames / fps, camera_stable=camera_stable,
                    detections=(risk_result or {}).get("detections", []),
                )
                crosswalk_voice.observe(crosswalk_result, processed_frames / fps, 1 / fps)
                if risk_result is not None:
                    risk_result["crosswalk_safety"] = crosswalk_result
            walking_surface_result = None
            if walking_surface_engine is not None:
                walking_surface_result = walking_surface_engine.update(
                    class_map, segmenter.label_ids, frame.shape, processed_frames / fps,
                    camera_stable=crosswalk_camera_stable(risk_result),
                    crosswalk_status=(crosswalk_result or {}).get("status"),
                )
                walking_surface_voice.observe(
                    walking_surface_result, processed_frames / fps, 1 / fps)
                if risk_result is not None:
                    risk_result["walking_surface"] = walking_surface_result
            if risk_result is not None:
                voice.observe(
                    risk_result, output_width, processed_frames / fps,
                    crossing_active=bool(
                        crosswalk_result and crosswalk_result["crossing_active"]),
                )
            if traffic_result is not None:
                # 일반 장애물 모델의 traffic_light 박스와 대상 신호등 표시가 겹치지 않게 한다.
                detections = [item for item in detections if item["class_name"] != "traffic_light"]
            result = (
                overlay_segmentation(frame, class_map, segmenter.label_ids, alpha)
                if segmenter is not None else frame
            )
            if detector is not None and not risk_enabled:
                result = draw_detections(result, detections)
            if risk_result is not None:
                result = draw_risk(result, risk_result, risk_settings)
                if risk_log is not None:
                    risk_log.write(risk_result)
            if traffic_result is not None:
                result = draw_traffic(result, traffic_result)
            if crosswalk_result is not None:
                result = draw_crosswalk_safety(result, crosswalk_result)
            if walking_surface_result is not None:
                result = draw_walking_surface(result, walking_surface_result)
            writer.write(result)
            processed_frames += 1
            print(
                f"\r영상 처리: {processed_frames}/{total_frames or '?'}",
                end="", flush=True,
            )
        if processed_frames == 0:
            raise RuntimeError(f"읽을 수 있는 프레임이 없습니다: {video_path}")
        if total_frames is not None and processed_frames != total_frames:
            raise RuntimeError(
                f"영상 프레임 수 불일치: 예상={total_frames}, 처리={processed_frames}\n"
                f"읽기 오류 또는 영상 메타데이터 오류를 확인하세요: {video_path}\n"
                "최종 결과 파일은 저장하지 않습니다."
            )

        # 인코딩 종료 후 완성된 파일만 최종 이름으로 교체
        writer.release()
        writer = None

        video_to_publish = temporary_path
        walking_events = voice.events if voice is not None else []
        signal_events = signal_voice.events if signal_voice is not None else []
        crosswalk_events = (crosswalk_voice.events(processed_frames / fps)
                            if crosswalk_voice is not None else [])
        walking_surface_events = (
            walking_surface_voice.events(processed_frames / fps)
            if walking_surface_voice is not None else [])
        voice_events = prioritize_voice_events(
            walking_events, signal_events, crosswalk_events, walking_surface_events)
        if voice_events:
            with tempfile.NamedTemporaryFile(
                dir=output_path.parent, prefix=f".{output_path.stem}.",
                suffix=".partial.wav", delete=False,
            ) as temporary_file:
                temporary_wav = Path(temporary_file.name)
            render_voice_track(voice_events, processed_frames / fps, temporary_wav)
            with tempfile.NamedTemporaryFile(
                dir=output_path.parent, prefix=f".{output_path.stem}.",
                suffix=".voice.partial.mp4", delete=False,
            ) as temporary_file:
                temporary_mux = Path(temporary_file.name)
            mux_voice(temporary_path, temporary_wav, temporary_mux)
            video_to_publish = temporary_mux

        if risk_log is not None:
            risk_log.close()
        publish_video_result(video_to_publish, output_path, risk_log)
        committed = True
    finally:
        try:
            capture.release()
            if writer is not None:
                writer.release()
        finally:
            # 성공·오류·Ctrl+C 모두 이번 작업의 임시 파일만 정리
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
            if temporary_wav is not None:
                temporary_wav.unlink(missing_ok=True)
            if temporary_mux is not None:
                temporary_mux.unlink(missing_ok=True)
            if risk_log is not None:
                risk_log.finish(committed)
    print(f"\n결과 영상 저장: {output_path}")
    return processed_frames


# 기존 영상과 위험 로그를 보존할 결과 경로 선택
def next_available_output(output_path, reserved=()):
    """같은 이름의 결과가 있으면 (1), (2)를 붙인 새 MP4 경로를 반환한다."""
    base = Path(output_path)
    reserved = set(reserved)
    number = 0
    while True:
        candidate = base if number == 0 else base.with_name(f"{base.stem}({number}){base.suffix}")
        if (candidate not in reserved and not candidate.exists()
                and not candidate.with_suffix(".risk.jsonl").exists()):
            return candidate
        number += 1


# 설정 및 명령어 옵션으로 추론 실행
def run_video_inference(
    config_path=DEFAULT_CONFIG,
    *,
    mask2former_weights=None,
    video_path=None,
    sample_dir=None,
    output_path=None,
    output_dir=None,
    device=None,
    mode=None,
    yolo_weights=None,
    conf=None,
    imgsz=None,
    traffic_weights=None,
    traffic_classifier_weights=None,
    risk=None,
):
    """명령어 옵션을 설정에 우선 적용하고 모든 대상 영상을 처리한다."""
    if video_path is not None and sample_dir is not None:
        raise ValueError("--video-path와 --sample-dir은 동시에 지정할 수 없습니다.")
    if output_path is not None and output_dir is not None:
        raise ValueError("--output-path와 --output-dir은 동시에 지정할 수 없습니다.")
    config = load_config(config_path)
    mode = mode if mode is not None else config.get("mode", "all")
    if mode not in ("both", "sidewalk", "obstacle", "traffic", "all"):
        raise ValueError("mode는 both, sidewalk, obstacle, traffic, all 중 하나여야 합니다.")
    device = device if device is not None else config["device"]
    if device not in ("auto", "cpu", "cuda"):
        raise ValueError("device는 auto, cpu, cuda 중 하나여야 합니다.")
    yolo_config = {}
    if mode in ("both", "obstacle", "all"):
        if not isinstance(config.get("yolo", {}), dict):
            raise ValueError("yolo 설정은 weights, conf, imgsz, head 항목으로 작성하세요.")
        yolo_config = dict(config.get("yolo", {}))
        for key, value in (("weights", yolo_weights), ("conf", conf), ("imgsz", imgsz)):
            if value is not None:
                yolo_config[key] = str(value) if key == "weights" else value
        validate_yolo_config(yolo_config)
    risk_settings = normalize_risk({"enabled": False})
    tracking_settings = None
    if mode in ("both", "obstacle", "all"):
        risk_settings = normalize_risk(config.get("risk"))
        if risk is not None:
            risk_settings = normalize_risk({**risk_settings, "enabled": risk})
        tracking_settings = normalize_tracking(config.get("tracking"))
    traffic_config = {}
    if mode in ("traffic", "all"):
        if not isinstance(config.get("traffic", {}), dict):
            raise ValueError("traffic 설정은 사전이어야 합니다.")
        traffic_config = dict(config.get("traffic", {}))
        for key, value in (("weights", traffic_weights), ("classifier_weights", traffic_classifier_weights)):
            if value is not None:
                traffic_config[key] = str(value)
        validate_traffic_config(traffic_config)
    mask2former_weights = resolve_path(
        mask2former_weights if mask2former_weights is not None else config["mask2former"]["weights"]
    )
    videos = (
        [resolve_path(video_path)] if video_path is not None
        else find_sample_videos(sample_dir if sample_dir is not None else config["sample_dir"])
    )
    if output_path is not None and len(videos) != 1:
        raise ValueError("--output-path는 영상 한 개 처리 시에만 지정할 수 있습니다.")
    destination = resolve_path(output_dir if output_dir is not None else config["output_dir"])
    outputs = [
        resolve_path(output_path) if output_path is not None
        else destination / video.parent.name / f"result_{video.stem}.mp4"
        for video in videos
    ]

    # 전체 입출력 사전 검증
    for video, output in zip(videos, outputs):
        if not video.is_file():
            raise FileNotFoundError(f"입력 영상이 없습니다: {video}")
        if output.suffix.lower() != ".mp4":
            raise ValueError(f"결과 영상 확장자는 .mp4여야 합니다: {output}")
        if video.resolve() == output.resolve():
            raise ValueError(f"입력 영상을 결과 경로로 덮어쓸 수 없습니다: {video}")

    # 사전 검증 뒤 같은 이름의 기존 결과와 로그를 피해 새 경로를 배정한다.
    allocated = []
    for output in outputs:
        allocated.append(next_available_output(output, allocated))
    outputs = allocated

    # 사용할 모델만 로딩, 모든 영상에서 재사용
    detector = None
    if mode in ("both", "obstacle", "all"):
        yolo_weights = resolve_path(yolo_config["weights"])
        detector = ObstacleDetector(
            yolo_weights, device=device,
            conf=yolo_config["conf"], imgsz=yolo_config["imgsz"], head=yolo_config["head"],
            iou=yolo_config.get("iou", 0.7), max_det=yolo_config.get("max_det", 300),
            rect=yolo_config.get("rect", True),
        )
        device = detector.device
        print(
            f"YOLO: {yolo_weights}\n"
            f"YOLO 설정: conf={detector.conf}, imgsz={detector.imgsz}, head=nms"
        )
    segmenter = (
        SidewalkSegmenter(mask2former_weights, device=device)
        if mode in ("both", "sidewalk", "all") else None
    )
    if segmenter is not None:
        device = segmenter.device
        print(f"Mask2Former: {mask2former_weights}")
    traffic = None
    if mode in ("traffic", "all"):
        traffic_config["weights"] = resolve_path(traffic_config["weights"])
        traffic_config["classifier_weights"] = resolve_path(traffic_config["classifier_weights"])
        traffic = TrafficSignalPipeline(device=str(device), **traffic_config)
        device = traffic.device
        print(f"신호등 YOLO: {traffic_config['weights']}\nMobileNet: {traffic_config['classifier_weights']}")
    print(f"추론 모드: {mode} | 장치: {device}")
    for index, (video, output) in enumerate(zip(videos, outputs), start=1):
        print(f"입력 영상 [{index}/{len(videos)}]: {video}")
        output.parent.mkdir(parents=True, exist_ok=True)
        process_video(video, output, segmenter, config["overlay_alpha"], detector=detector,
                      traffic=traffic, risk_config=risk_settings, tracking_config=tracking_settings,
                      crosswalk_config=config.get("crosswalk_safety"),
                      recorded_frame_config=config["recorded_frame"],
                      walking_surface_config_value=config.get("walking_surface"))
    return outputs

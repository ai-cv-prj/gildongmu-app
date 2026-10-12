"""실시간 추론에 필요한 모델 경로와 공통 설정을 읽고 검증한다."""

from pathlib import Path

from src.settings import DEFAULT_PATHS_CONFIG, PROJECT_DIR, load_paths, read_yaml


def resolve_path(value):
    """상대 경로는 통합 레포 루트, 절대 경로는 지정 위치로 해석한다."""
    path = Path(value).expanduser()
    return (PROJECT_DIR / path).resolve()


def load_config(config_path):
    """모델별 가중치 경로와 공통 추론 설정을 확인한다."""
    config = read_yaml(config_path)
    paths = load_paths(config.get("paths_config", DEFAULT_PATHS_CONFIG))
    config.setdefault("session_dir", paths["session_dir"])
    config.setdefault("mask2former", {"weights": paths["mask2former_weights"]})
    for name in ("yolo", "traffic"):
        settings = config.setdefault(name, {})
        if isinstance(settings, dict):
            settings.setdefault("weights", paths[f"{name}_weights"])
    if isinstance(config["traffic"], dict):
        config["traffic"].setdefault("classifier_weights", paths["traffic_classifier_weights"])
    required = {"mask2former", "device"}
    if not isinstance(config, dict) or not required.issubset(config):
        raise ValueError(f"설정에 필요한 항목: {', '.join(sorted(required))}")
    mask2former_config = config["mask2former"]
    if not isinstance(mask2former_config, dict):
        raise ValueError("mask2former 설정에는 weights 항목이 필요합니다.")
    mask2former_weights = mask2former_config.get("weights")
    if not isinstance(mask2former_weights, str) or not mask2former_weights.strip():
        raise ValueError("mask2former.weights에는 비어 있지 않은 폴더 경로를 지정하세요.")
    if config["device"] not in ("auto", "cpu", "cuda"):
        raise ValueError("device는 auto, cpu, cuda 중 하나여야 합니다.")
    return config

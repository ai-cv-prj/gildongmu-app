"""
file_path: tests/test_video_output_naming.py

영상 추론 결과 이름이 겹칠 때 기존 MP4와 위험 로그를 보존하는지 확인한다.
"""

import io
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from src.pipeline import DEFAULT_CONFIG, load_config, next_available_output, run_video_inference


# 기존 결과와 위험 로그의 이름 충돌 확인
def test_next_available_output_keeps_video_and_risk_log(tmp_path):
    """MP4나 짝 위험 로그가 있으면 둘 다 비어 있는 다음 번호를 선택한다."""
    base = tmp_path / "result_test.mp4"
    base.write_bytes(b"original video")
    base.with_suffix(".risk.jsonl").write_text("original log", encoding="utf-8")
    first = tmp_path / "result_test(1).mp4"
    assert next_available_output(base) == first
    first.with_suffix(".risk.jsonl").write_text("orphan log", encoding="utf-8")
    assert next_available_output(base) == tmp_path / "result_test(2).mp4"
    assert base.read_bytes() == b"original video"
    assert base.with_suffix(".risk.jsonl").read_text(encoding="utf-8") == "original log"


# 명령행 영상 추론의 결과 경로 확인
def test_run_video_inference_adds_suffix_to_existing_result(tmp_path):
    """이미 생성된 영상 결과를 유지하고 새 결과 경로를 모델에 전달한다."""
    source = tmp_path / "sample" / "test.mp4"
    source.parent.mkdir()
    source.touch()
    base = tmp_path / "results" / "sample" / "result_test.mp4"
    base.parent.mkdir(parents=True)
    base.write_bytes(b"original video")
    base.with_suffix(".risk.jsonl").write_text("original log", encoding="utf-8")
    config = load_config(DEFAULT_CONFIG)
    config["output_dir"] = str(tmp_path / "results")
    with patch("src.pipeline.load_config", return_value=config), patch(
        "src.pipeline.SidewalkSegmenter"
    ), patch("src.pipeline.process_video") as process, redirect_stdout(io.StringIO()):
        outputs = run_video_inference(video_path=source, mode="sidewalk", device="cpu")
    expected = Path(str(base.with_suffix("")) + "(1).mp4")
    assert outputs == [expected]
    assert process.call_args.args[1] == expected
    assert base.read_bytes() == b"original video"

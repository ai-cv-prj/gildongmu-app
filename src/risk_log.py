"""
file_path: src/risk_log.py

영상의 프레임별 위험 판정을 임시 JSONL에 기록한다.
완료된 결과만 최종 영상과 함께 공개한다.
"""
import json
import os
from pathlib import Path
import tempfile


# 결과 영상에 대응하는 위험 로그 경로 계산
def risk_log_path(video_output):
    """
    결과 MP4와 같은 샘플 폴더의 jsonl 하위 폴더에 위험 로그 경로를 만든다.
    """
    video_output = Path(video_output)
    return video_output.parent / "jsonl" / f"{video_output.stem}.risk.jsonl"


class RiskLog:
    """완료 전까지 위험 로그를 임시 파일에 보관한다."""

    # jsonl 하위 폴더에 임시 위험 로그 만들기
    def __init__(self, video_output, overwrite=False):
        """덮어쓰기 여부에 따라 기존 로그를 확인하고 임시 파일을 연다."""
        self.path = risk_log_path(video_output)
        if self.path.exists() and not overwrite:
            raise FileExistsError(f"Risk log already exists: {self.path}")
        self.parent_created = not self.path.parent.exists()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.file = tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.path.parent,
                     prefix=f".{self.path.stem}.", suffix=".partial.jsonl", delete=False)
        self.temporary = Path(self.file.name)
        self.published = False

    # 한 프레임의 위험 결과 기록
    def write(self, prediction):
        """위험 판정 한 건을 JSONL 줄로 저장한다."""
        self.file.write(json.dumps(prediction, ensure_ascii=False, allow_nan=False)+"\n")

    # 영상 공개 전에 로그 쓰기 종료
    def close(self):
        """임시 로그를 닫아 최종 경로로 옮길 준비를 한다."""
        self.file.close()

    # 기존 비덮어쓰기 방식의 로그 공개
    def publish(self):
        """기존 로그를 보호하면서 임시 로그에 최종 이름을 붙인다."""
        self.file.close()
        os.link(self.temporary, self.path)
        self.published = True

    # 성공 또는 실패 후 임시 로그 정리
    def finish(self, committed):
        """실패한 비덮어쓰기 공개를 되돌리고 이번 임시 파일을 지운다."""
        self.file.close()
        # Only remove a link created by this operation, never a pre-existing/replaced result.
        if self.published and not committed and self.path.exists() and os.path.samefile(self.path,self.temporary):
            self.path.unlink()
        self.temporary.unlink(missing_ok=True)
        if self.parent_created:
            try:
                self.path.parent.rmdir()
            except OSError:
                pass

"""
file_path: scripts/run_realtime_app.py

PC에서 휴대폰 테스트용 FastAPI 서버를 실행한다.
"""

import os

import uvicorn


# 환경 변수에 지정된 주소와 포트로 서버 실행
def main():
    """run.sh와 같은 APP_HOST, APP_PORT 설정을 사용한다."""
    uvicorn.run("backend.app:app", host=os.environ.get("APP_HOST", "127.0.0.1"),
                port=int(os.environ.get("APP_PORT", "8001")), workers=1)


if __name__ == "__main__":
    main()

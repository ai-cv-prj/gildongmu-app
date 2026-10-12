"""서버와 터널 실행 스크립트에 YAML·환경변수 기반 주소·포트를 전달한다."""

from src.settings import server_address


def main():
    """호스트·포트·로컬 접속 URL을 셸이 해석하지 않는 줄 단위 값으로 출력한다."""
    address = server_address()
    host = address["host"]
    connect_host = "127.0.0.1" if host == "0.0.0.0" else "::1" if host == "::" else host
    if ":" in connect_host:
        connect_host = f"[{connect_host}]"
    print(host)
    print(address["port"])
    print(f"http://{connect_host}:{address['port']}")


if __name__ == "__main__":
    main()

"""위치 기반 정류장·경유 노선·버스 도착정보 API 테스트."""
from __future__ import annotations

import json
from collections import Counter
from io import BytesIO
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlparse

import pytest

from backend.bus.config import BusServiceSettings as Settings
from backend.bus.arrival import BusArrivalError, BusArrivalService
from backend.bus.speech import BusSpeechError, BusSpeechService


def _settings(tmp_path: Path, service_key: str = "test%2Bkey%3D") -> Settings:
    return Settings(
        seoul_bus_api_timeout_sec=8,
        seoul_bus_api_key=service_key,
        seoul_bus_station_radius_m=30,
        seoul_bus_max_station_distance_m=30,
    )


def _bus_response(items: str) -> BytesIO:
    return BytesIO((
        "<ServiceResult><msgHeader><headerCd>0</headerCd></msgHeader>"
        f"<msgBody>{items}</msgBody></ServiceResult>"
    ).encode())


@pytest.mark.parametrize("service_key", ["test%2Bkey%3D", "test+key="])
def test_route_arrival_lookup_filters_station_name(tmp_path: Path, service_key: str) -> None:
    requested_urls: list[str] = []

    def opener(url: str, timeout: float) -> BytesIO:
        requested_urls.append(url)
        assert timeout == 8
        return BytesIO("""<ServiceResult><msgHeader><headerCd>0</headerCd></msgHeader><msgBody>
          <itemList><busRouteId>100100118</busRouteId><stId>111000299</stId>
            <stNm>구산동사거리</stNm><arsId>12390</arsId><rtNm>753</rtNm>
            <dir>숭실대</dir><staOrd>1</staOrd><arrmsg1>3분 후 도착</arrmsg1>
            <exps1>180</exps1><arrmsg2>12분 후 도착</arrmsg2><exps2>720</exps2>
            <plainNo1>서울75사1234</plainNo1>
          </itemList>
          <itemList><busRouteId>100100118</busRouteId><stId>111000181</stId>
            <stNm>한솔아파트입구.선정중학교후문</stNm><rtNm>753</rtNm>
          </itemList>
        </msgBody></ServiceResult>""".encode())

    service = BusArrivalService(_settings(tmp_path, service_key=service_key), opener=opener)
    body = service.lookup("100100118", "구산동사거리")

    assert body == {
        "bus_route_id": "100100118",
        "bus_number": "753",
        "station_name": "구산동사거리",
        "serves_stop": True,
        "matches": [{
            "route_id": "100100118",
            "route_name": "753",
            "station_id": "111000299",
            "station_name": "구산동사거리",
            "ars_id": "12390",
            "direction": "숭실대",
            "direction_source": "api",
            "station_order": 1,
            "first_arrival": "3분 후 도착",
            "first_eta_seconds": 180,
            "first_vehicle_id": "",
            "first_arrival_state": "approaching",
            "second_arrival": "12분 후 도착",
            "second_eta_seconds": 720,
            "vehicle_number": "서울75사1234",
        }],
    }
    assert len(requested_urls) == 1
    assert "getArrInfoByRouteAll" in requested_urls[0]
    assert "busRouteId=100100118" in requested_urls[0]
    assert "serviceKey=test%2Bkey%3D" in requested_urls[0]
    assert "%252B" not in requested_urls[0]


@pytest.mark.parametrize(("vehicle_id", "expected_id"), [
    (None, ""), (" ", ""), ("0", ""), ("000", ""), (" 111033115 ", "111033115"),
])
def test_arrival_vehicle_id_normalizes_missing_vehicle(
    tmp_path: Path, vehicle_id: str | None, expected_id: str,
) -> None:
    vehicle_xml = f"<vehId1>{vehicle_id}</vehId1>" if vehicle_id is not None else ""

    def opener(url: str, timeout: float) -> BytesIO:
        return _bus_response(f"""
          <itemList><stNm>연세대앞</stNm><arrmsg1>3분 후 도착</arrmsg1>
            {vehicle_xml}</itemList>
        """)

    arrival = BusArrivalService(_settings(tmp_path), opener=opener).lookup("100100073", "연세대앞")["matches"][0]

    assert arrival["first_vehicle_id"] == expected_id
    assert arrival["first_arrival_state"] == "approaching"


@pytest.mark.parametrize(("message", "station_order", "section_order", "arrived", "vehicle_fields", "eta", "expected_state"), [
    pytest.param("진입중", "18", "18", "1", "<vehId1>111033115</vehId1>", "0", "arrived", id="target-stop-arrived"),
    pytest.param("진입중", "18", "18", "1", "<vehId1>0</vehId1><plainNo1>서울74사2576</plainNo1>", "0", "arrived", id="plate-identifies-arrived-bus"),
    pytest.param("3분 후 도착", "18", "16", "1", "<vehId1>111033115</vehId1>", "180", "approaching", id="upstream-arrival-is-not-target-arrival"),
    pytest.param("진입중", "18", "17", "1", "<vehId1>111033115</vehId1>", "0", "arriving", id="upstream-nearby-bus"),
    pytest.param("진입중", "18", "18", "0", "<vehId1>111033115</vehId1>", "0", "arriving", id="section-match-without-arrival-flag"),
    pytest.param("곧 도착", "18", "17", "0", "<vehId1>111033115</vehId1>", "0", "arriving", id="arrival-message-is-approach-only"),
    pytest.param("진입중", "0", "0", "1", "<vehId1>111033115</vehId1>", "0", "arriving", id="zero-orders-cannot-confirm-arrival"),
    pytest.param("진입중", "", "", "1", "<vehId1>111033115</vehId1>", "0", "arriving", id="missing-orders-cannot-confirm-arrival"),
    pytest.param("진입중", "18", "18", "1", "<vehId1>0</vehId1><plainNo1>0</plainNo1>", "0", "arriving", id="missing-vehicle-cannot-confirm-arrival"),
    pytest.param("도착 예정", "18", "17", "0", "<vehId1>111033115</vehId1>", "1", "approaching", id="positive-eta-does-not-confirm-arrival"),
    pytest.param("도착 예정", "18", "17", "0", "<vehId1>111033115</vehId1>", "0", "unavailable", id="zero-eta-does-not-confirm-arrival"),
    pytest.param("", "18", "18", "1", "<vehId1>111033115</vehId1>", "0", "unavailable", id="missing-message"),
])
def test_arrival_state_only_confirms_vehicle_at_requested_stop(
    tmp_path: Path, message: str, station_order: str, section_order: str,
    arrived: str, vehicle_fields: str, eta: str, expected_state: str,
) -> None:
    def opener(url: str, timeout: float) -> BytesIO:
        return _bus_response(f"""
          <itemList><stNm>연세대앞</stNm><rtNm>470</rtNm><dir>세브란스병원앞</dir>
            <staOrd>{station_order}</staOrd><sectOrd1>{section_order}</sectOrd1>
            <isArrive1>{arrived}</isArrive1><arrmsg1>{message}</arrmsg1>
            <exps1>{eta}</exps1>{vehicle_fields}</itemList>
        """)

    service = BusArrivalService(_settings(tmp_path), opener=opener)
    arrival = service.lookup("100100073", "연세대앞")["matches"][0]
    announcement = service._arrival_announcement("470", "연세대앞", arrival)

    assert arrival["first_arrival_state"] == expected_state
    if expected_state == "arrived":
        assert announcement == "연세대앞 정류장, 세브란스병원앞 방향, 470번 버스가 도착했습니다."
    else:
        assert "도착했습니다" not in announcement
    if expected_state == "arriving":
        assert announcement.endswith("버스가 곧 도착합니다.")


@pytest.mark.parametrize("message", [
    "출발대기", "운행 종료", "도착 정보 없음", "도착 정보가 없습니다",
    "정류소 출발", "정류장 통과",
])
def test_nonactive_arrival_messages_override_stale_vehicle_fields(tmp_path: Path, message: str) -> None:
    def opener(url: str, timeout: float) -> BytesIO:
        return _bus_response(f"""
          <itemList><stNm>연세대앞</stNm><staOrd>18</staOrd><sectOrd1>18</sectOrd1>
            <isArrive1>1</isArrive1><vehId1>111033115</vehId1><exps1>180</exps1>
            <arrmsg1>{message}</arrmsg1></itemList>
        """)

    service = BusArrivalService(_settings(tmp_path), opener=opener)
    arrival = service.lookup("100100073", "연세대앞")["matches"][0]

    assert arrival["first_arrival_state"] == "unavailable"
    assert "도착했습니다" not in service._arrival_announcement("470", "연세대앞", arrival)


@pytest.fixture
def route_470_arrival_items() -> str:
    # 같은 이름의 양방향 정류장을 포함하고, 응답 순서는 정차 순서와 다르다.
    return """
      <itemList><busRouteId>100100073</busRouteId><stId>112000011</stId>
        <stNm>서대문우체국</stNm><rtNm>470</rtNm><staOrd>75</staOrd></itemList>
      <itemList><busRouteId>100100073</busRouteId><stId>112000013</stId>
        <stNm>연세대앞</stNm><arsId>13013</arsId><rtNm>470</rtNm>
        <dir></dir><adirection></adirection><staOrd>74</staOrd>
        <arrmsg1>3분 후 도착</arrmsg1></itemList>
      <itemList><busRouteId>100100073</busRouteId><stId>112000012</stId>
        <stNm>연세대앞</stNm><arsId>13012</arsId><rtNm>470</rtNm>
        <dir> </dir><staOrd>18</staOrd><arrmsg1>8분 후 도착</arrmsg1></itemList>
      <itemList><busRouteId>100100073</busRouteId><stId>112000014</stId>
        <stNm>세브란스병원앞</stNm><rtNm>470</rtNm><staOrd>19</staOrd></itemList>
    """


def test_nearby_lookup_uses_next_station_for_both_directions(
    tmp_path: Path, route_470_arrival_items: str,
) -> None:
    requested_paths: list[str] = []
    checked_stations: list[str] = []

    def opener(url: str, timeout: float) -> BytesIO:
        parsed = urlparse(url)
        requested_paths.append(parsed.path.rsplit("/", 1)[-1])
        params = parse_qs(parsed.query)
        if "getStationByPos" in url:
            return _bus_response("""
              <itemList><stationId>112000013</stationId><stationNm>연세대앞</stationNm>
                <arsId>13013</arsId><dist>21</dist></itemList>
              <itemList><stationId>112000012</stationId><stationNm>연세대앞</stationNm>
                <arsId>13012</arsId><dist>7</dist></itemList>
            """)
        if "getRouteByStation" in url:
            ars_id = params["arsId"][0]
            checked_stations.append(ars_id)
            order = {"13012": 18, "13013": 74}[ars_id]
            return _bus_response(
                "<itemList><busRouteId>100100073</busRouteId><busRouteNm>470</busRouteNm>"
                f"<staOrd>{order}</staOrd></itemList>"
            )
        assert "getArrInfoByRouteAll" in url
        assert params["busRouteId"] == ["100100073"]
        return _bus_response(route_470_arrival_items)

    body = BusArrivalService(_settings(tmp_path), opener=opener).lookup_nearby("470", 37.5, 127.0)

    assert checked_stations == ["13012", "13013"]
    assert requested_paths.count("getStationByPos") == 1
    assert requested_paths.count("getRouteByStation") == 2
    assert requested_paths.count("getArrInfoByRouteAll") == 1
    assert len(requested_paths) == 4
    assert body["matched_station_count"] == 2
    assert [match["station"]["ars_id"] for match in body["matches"]] == ["13012", "13013"]
    assert [match["arrival"]["station_order"] for match in body["matches"]] == [18, 74]
    assert [match["arrival"]["direction"] for match in body["matches"]] == ["세브란스병원앞", "서대문우체국"]
    assert [match["arrival"]["direction_source"] for match in body["matches"]] == ["next_station", "next_station"]
    announcements = [
        "연세대앞 정류장, 세브란스병원앞 방향, 470번 버스가 8분 후 도착합니다.",
        "연세대앞 정류장, 서대문우체국 방향, 470번 버스가 3분 후 도착합니다.",
    ]
    assert [match["announcement"] for match in body["matches"]] == announcements
    assert body["announcement"] == " ".join(announcements)
    assert body["station"] == body["matches"][0]["station"]
    assert body["arrival"] == body["matches"][0]["arrival"]
    assert body["arrival"]["direction"] == "세브란스병원앞"


def test_nearby_lookup_reuses_static_stops_but_refreshes_arrival_and_distance(tmp_path: Path) -> None:
    calls: list[str] = []
    now = [0.0]
    arrival_number = [0]

    def opener(url: str, timeout: float) -> BytesIO:
        path = urlparse(url).path.rsplit("/", 1)[-1]
        calls.append(path)
        if path == "getStationByPos":
            assert parse_qs(urlparse(url).query)["radius"] == ["60"]
            return _bus_response("""
              <itemList><stationId>A</stationId><stationNm>동쪽</stationNm><arsId>10001</arsId>
                <gpsY>37.5</gpsY><gpsX>127.0001</gpsX></itemList>
              <itemList><stationId>B</stationId><stationNm>서쪽</stationNm><arsId>10002</arsId>
                <gpsY>37.5</gpsY><gpsX>126.9999</gpsX></itemList>
              <itemList><stationId>C</stationId><stationNm>새정류장</stationNm><arsId>10003</arsId>
                <gpsY>37.5</gpsY><gpsX>127.0005</gpsX></itemList>
            """)
        if path == "getRouteByStation":
            ars_id = parse_qs(urlparse(url).query)["arsId"][0]
            return _bus_response(f"""
              <itemList><busRouteId>route-105</busRouteId><busRouteNm>105</busRouteNm>
                <staOrd>{int(ars_id) - 9980}</staOrd></itemList>
            """)
        assert path == "getArrInfoByRouteAll"
        arrival_number[0] += 1
        return _bus_response("".join(f"""
          <itemList><busRouteId>route-105</busRouteId><stId>{station_id}</stId>
            <arsId>{ars_id}</arsId><stNm>{name}</stNm><staOrd>{int(ars_id) - 9980}</staOrd>
            <arrmsg1>{arrival_number[0]}분 후 도착</arrmsg1></itemList>
        """ for station_id, ars_id, name in [("A", "10001", "동쪽"), ("B", "10002", "서쪽"), ("C", "10003", "새정류장")]))

    service = BusArrivalService(_settings(tmp_path), opener=opener, clock=lambda: now[0])
    first = service.lookup_nearby("105", 37.5, 127.0)
    assert first["matched_station_count"] == 2
    assert calls[0] == "getStationByPos"
    assert Counter(calls) == {"getStationByPos": 1, "getRouteByStation": 2, "getArrInfoByRouteAll": 1}

    # 처음에는 30m 밖이던 C가 이동 후 범위에 들어와도 확장 조회 캐시에서 찾는다.
    second = service.lookup_nearby("105", 37.5, 127.0002)
    assert second["matched_station_count"] == 3
    assert len(calls) == 6
    assert Counter(calls[-2:]) == {"getRouteByStation": 1, "getArrInfoByRouteAll": 1}
    assert second["matches"][0]["arrival"]["first_arrival"] == "2분 후 도착"
    before = {match["station"]["station_id"]: match["station"]["distance_m"] for match in first["matches"]}
    after = {match["station"]["station_id"]: match["station"]["distance_m"] for match in second["matches"]}
    assert after["B"] > before["B"]

    service.lookup_nearby("105", 37.5, 127.0002)
    assert len(calls) == 7
    assert calls[-1] == "getArrInfoByRouteAll"
    now[0] = 301
    service.lookup_nearby("105", 37.5, 127.0002)
    assert calls[-5] == "getStationByPos"
    assert Counter(calls[-5:]) == {"getStationByPos": 1, "getRouteByStation": 3, "getArrInfoByRouteAll": 1}


@pytest.mark.parametrize("duplicate_next_stop", [False, True])
def test_route_lookup_uses_full_unsorted_route_for_same_name_stops(
    tmp_path: Path, route_470_arrival_items: str, duplicate_next_stop: bool,
) -> None:
    requested_urls: list[str] = []
    if duplicate_next_stop:
        route_470_arrival_items += """
          <itemList><busRouteId>100100073</busRouteId><stId>112000014</stId>
            <stNm>세브란스병원앞</stNm><rtNm>470</rtNm><staOrd>19</staOrd></itemList>
        """

    def opener(url: str, timeout: float) -> BytesIO:
        requested_urls.append(url)
        return _bus_response(route_470_arrival_items)

    body = BusArrivalService(_settings(tmp_path), opener=opener).lookup("100100073", "연세대앞")

    assert body["serves_stop"] is True
    assert body["bus_number"] == "470"
    assert [match["ars_id"] for match in body["matches"]] == ["13013", "13012"]
    assert [match["direction"] for match in body["matches"]] == ["서대문우체국", "세브란스병원앞"]
    assert all(match["direction_source"] == "next_station" for match in body["matches"])
    assert len(requested_urls) == 1
    assert "getArrInfoByRouteAll" in requested_urls[0]


@pytest.mark.parametrize(("direction_fields", "expected_direction"), [
    ("<dir>양재</dir>", "양재"),
    ("<dir> </dir><adirection>수색</adirection>", "수색"),
    ("<dir>양재</dir><adirection>수색</adirection>", "양재"),
])
def test_route_lookup_preserves_api_direction_over_next_station(
    tmp_path: Path, direction_fields: str, expected_direction: str,
) -> None:
    def opener(url: str, timeout: float) -> BytesIO:
        return _bus_response(f"""
          <itemList><stId>112000012</stId><stNm>연세대앞</stNm><staOrd>18</staOrd>
            {direction_fields}</itemList>
          <itemList><stId>112000014</stId><stNm>세브란스병원앞</stNm><staOrd>19</staOrd></itemList>
        """)

    body = BusArrivalService(_settings(tmp_path), opener=opener).lookup("100100073", "연세대앞")

    assert body["matches"][0]["direction"] == expected_direction
    assert body["matches"][0]["direction_source"] == "api"


@pytest.mark.parametrize(("current_order", "other_stops"), [
    pytest.param("18", "<itemList><stNm>기점</stNm><staOrd>1</staOrd></itemList>", id="terminal-no-wrap"),
    pytest.param("18", "<itemList><stNm>다다음정류장</stNm><staOrd>20</staOrd></itemList>", id="sequence-gap"),
    pytest.param(None, "<itemList><stNm>다음정류장</stNm><staOrd>19</staOrd></itemList>", id="missing-order"),
    pytest.param("invalid", "<itemList><stNm>다음정류장</stNm><staOrd>19</staOrd></itemList>", id="invalid-order"),
    pytest.param("18.5", "<itemList><stNm>다음정류장</stNm><staOrd>19</staOrd></itemList>", id="fractional-order"),
    pytest.param("0", "<itemList><stNm>다음정류장</stNm><staOrd>1</staOrd></itemList>", id="zero-order"),
    pytest.param("-1", "<itemList><stNm>다음정류장</stNm><staOrd>0</staOrd></itemList>", id="negative-order"),
    pytest.param("18", "<itemList><stId>112000014</stId><staOrd>19</staOrd></itemList>", id="missing-next-name"),
    pytest.param("18", "<itemList><stId>112000014</stId><stNm>연세대앞</stNm><staOrd>19</staOrd></itemList>", id="same-next-name"),
    pytest.param("18", "<itemList><stId>112000012</stId><stNm>다른이름</stNm><staOrd>19</staOrd></itemList>", id="same-next-id"),
    pytest.param("18", """
      <itemList><stNm>첫번째후보</stNm><staOrd>19</staOrd></itemList>
      <itemList><stNm>두번째후보</stNm><staOrd>19</staOrd></itemList>
    """, id="ambiguous-next-order"),
    pytest.param("18", """
      <itemList><busRouteId>999999999</busRouteId><stNm>다른노선정류장</stNm><staOrd>19</staOrd></itemList>
    """, id="different-route"),
])
def test_route_lookup_keeps_unknown_direction_without_unique_next_stop(
    tmp_path: Path, current_order: str | None, other_stops: str,
) -> None:
    order_xml = f"<staOrd>{current_order}</staOrd>" if current_order is not None else ""

    def opener(url: str, timeout: float) -> BytesIO:
        return _bus_response(f"""
          <itemList><busRouteId>100100073</busRouteId><stId>112000012</stId>
            <stNm>연세대앞</stNm><dir></dir><adirection> </adirection>{order_xml}</itemList>
          {other_stops}
        """)

    body = BusArrivalService(_settings(tmp_path), opener=opener).lookup("100100073", "연세대앞")

    assert body["matches"][0]["station_id"] == "112000012"
    assert body["matches"][0]["direction"] == ""
    assert body["matches"][0]["direction_source"] == "unknown"


def test_unknown_station_returns_no_match(tmp_path: Path) -> None:
    def opener(url: str, timeout: float) -> BytesIO:
        return BytesIO("""<ServiceResult><msgHeader><headerCd>0</headerCd></msgHeader><msgBody>
          <itemList><stId>111000299</stId><stNm>구산동사거리</stNm><rtNm>753</rtNm></itemList>
        </msgBody></ServiceResult>""".encode())

    body = BusArrivalService(_settings(tmp_path), opener=opener).lookup("100100118", "없는정류장")

    assert body["bus_number"] == "753"
    assert body["serves_stop"] is False
    assert body["matches"] == []


def test_nearby_lookup_finds_route_and_arrival(tmp_path: Path) -> None:
    requested_urls: list[str] = []

    def opener(url: str, timeout: float) -> BytesIO:
        requested_urls.append(url)
        if "getStationByPos" in url:
            return BytesIO("""<ServiceResult><msgHeader><headerCd>0</headerCd></msgHeader><msgBody>
              <itemList><stationId>111000181</stationId><stationNm>다른정류장</stationNm>
                <arsId>12271</arsId><dist>2.5</dist><gpsX>127.0</gpsX><gpsY>37.5</gpsY>
              </itemList>
              <itemList><stationId>111000299</stationId><stationNm>구산동사거리</stationNm>
                <arsId>12390</arsId><dist>9.2</dist><gpsX>127.0</gpsX><gpsY>37.5</gpsY>
              </itemList>
            </msgBody></ServiceResult>""".encode())
        if "getRouteByStation" in url and "arsId=12271" in url:
            return BytesIO("""<ServiceResult><msgHeader><headerCd>0</headerCd></msgHeader><msgBody>
              <itemList><busRouteId>100100001</busRouteId><busRouteNm>105</busRouteNm></itemList>
            </msgBody></ServiceResult>""".encode())
        if "getRouteByStation" in url:
            return BytesIO("""<ServiceResult><msgHeader><headerCd>0</headerCd></msgHeader><msgBody>
              <itemList><busRouteId>100100118</busRouteId><busRouteNm>753</busRouteNm></itemList>
            </msgBody></ServiceResult>""".encode())
        return BytesIO("""<ServiceResult><msgHeader><headerCd>0</headerCd></msgHeader><msgBody>
          <itemList><stId>111000299</stId><stNm>구산동사거리</stNm><arsId>12390</arsId>
            <rtNm>753</rtNm><dir>숭실대</dir><arrmsg1>8분 후 도착</arrmsg1><exps1>480</exps1>
          </itemList>
        </msgBody></ServiceResult>""".encode())

    service = BusArrivalService(_settings(tmp_path), opener=opener)
    body = service.lookup_nearby("753", 37.5, 127.0, 25)

    assert body["route_found"] is True
    assert body["bus_route_id"] == "100100118"
    assert body["station"] == {
        "station_id": "111000299",
        "station_name": "구산동사거리",
        "ars_id": "12390",
        "distance_m": 9.2,
        "latitude": 37.5,
        "longitude": 127.0,
    }
    assert body["arrival"]["first_eta_seconds"] == 480
    assert body["announcement"] == "구산동사거리 정류장, 숭실대 방향, 753번 버스가 8분 후 도착합니다."
    assert body["search_radius_m"] == 30
    assert body["matched_station_count"] == 1
    assert body["matches"] == [{
        "station": body["station"],
        "bus_route_id": body["bus_route_id"],
        "route_found": True,
        "arrival": body["arrival"],
        "announcement": body["announcement"],
    }]
    assert len(requested_urls) == 4
    assert "tmX=127.0000000" in requested_urls[0]
    assert "tmY=37.5000000" in requested_urls[0]
    assert "radius=60" in requested_urls[0]
    assert "arsId=12271" in requested_urls[1]
    assert "arsId=12390" in requested_urls[2]
    assert "busRouteId=100100118" in requested_urls[3]


@pytest.mark.parametrize("opposite_route_id", ["100100118", "100100119"])
def test_nearby_lookup_checks_all_distinct_stops_in_distance_order(
    tmp_path: Path, opposite_route_id: str,
) -> None:
    checked_stations: list[str] = []
    requested_routes: list[str] = []

    def opener(url: str, timeout: float) -> BytesIO:
        params = parse_qs(urlparse(url).query)
        if "getStationByPos" in url:
            return _bus_response("""
              <itemList><stationId>111000300</stationId><stationNm>구산동사거리</stationNm>
                <arsId>12391</arsId><dist>30</dist></itemList>
              <itemList><stationId>111000299</stationId><stationNm>구산동사거리</stationNm>
                <arsId>12390</arsId><dist>9</dist></itemList>
              <itemList><stationId>111000181</stationId><stationNm>가까운다른노선</stationNm>
                <arsId>12271</arsId><dist>2</dist></itemList>
              <itemList><stationId>111000182</stationId><stationNm>먼다른노선</stationNm>
                <arsId>12272</arsId><dist>15</dist></itemList>
              <itemList><stationId>111000299</stationId><stationNm>구산동사거리</stationNm>
                <arsId>12390</arsId><dist>8</dist></itemList>
              <itemList><stationId>111000301</stationId><stationNm>반경밖정류장</stationNm>
                <arsId>12392</arsId><dist>30.1</dist></itemList>
              <itemList><arsId>12390</arsId><dist>1</dist></itemList>
              <itemList><stationNm>번호없는정류장</stationNm><dist>1</dist></itemList>
            """)
        if "getRouteByStation" in url:
            ars_id = params["arsId"][0]
            checked_stations.append(ars_id)
            if ars_id in {"12271", "12272"}:
                return _bus_response("<itemList><busRouteId>100100001</busRouteId><busRouteNm>105</busRouteNm></itemList>")
            route_id = opposite_route_id if ars_id == "12391" else "100100118"
            return _bus_response(f"<itemList><busRouteId>{route_id}</busRouteId><busRouteNm>753</busRouteNm></itemList>")
        requested_routes.append(params["busRouteId"][0])
        return _bus_response("""
          <itemList><stId>111000299</stId><stNm>구산동사거리</stNm><arsId>12390</arsId>
            <rtNm>753</rtNm><dir>숭실대</dir><staOrd>9</staOrd><arrmsg1>8분 후 도착</arrmsg1></itemList>
          <itemList><stId>111000300</stId><stNm>구산동사거리</stNm><arsId>12391</arsId>
            <rtNm>753</rtNm><dir>구산동</dir><staOrd>40</staOrd><arrmsg1>3분 후 도착</arrmsg1></itemList>
        """)

    body = BusArrivalService(_settings(tmp_path), opener=opener).lookup_nearby("753", 37.5, 127.0)

    assert checked_stations == ["12271", "12390", "12272", "12391"]
    assert requested_routes == list(dict.fromkeys(["100100118", opposite_route_id]))
    assert body["matched_station_count"] == 2
    assert [match["station"]["distance_m"] for match in body["matches"]] == [8, 30]
    assert [match["station"]["ars_id"] for match in body["matches"]] == ["12390", "12391"]
    assert [match["arrival"]["direction"] for match in body["matches"]] == ["숭실대", "구산동"]
    assert all(match["route_found"] for match in body["matches"])
    assert body["station"] == body["matches"][0]["station"]
    assert body["arrival"] == body["matches"][0]["arrival"]
    assert body["bus_route_id"] == body["matches"][0]["bus_route_id"]
    assert body["announcement"] == (
        "구산동사거리 정류장, 숭실대 방향, 753번 버스가 8분 후 도착합니다. "
        "구산동사거리 정류장, 구산동 방향, 753번 버스가 3분 후 도착합니다."
    )


@pytest.mark.parametrize(("route_orders", "expected_orders"), [
    ([40], [40]),
    ([9, 40, 40], [9, 40]),
    ([None], [9, 40]),
])
def test_nearby_lookup_matches_repeated_stop_visits_by_station_order(
    tmp_path: Path, route_orders: list[int | None], expected_orders: list[int],
) -> None:
    arrival_requests = 0

    def opener(url: str, timeout: float) -> BytesIO:
        nonlocal arrival_requests
        if "getStationByPos" in url:
            return _bus_response("""
              <itemList><stationId>111000299</stationId><stationNm>구산동사거리</stationNm>
                <arsId>12390</arsId><dist>9</dist></itemList>
            """)
        if "getRouteByStation" in url:
            return _bus_response("".join(
                "<itemList><busRouteId>100100118</busRouteId><busRouteNm>753</busRouteNm>"
                + (f"<staOrd>{order}</staOrd>" if order is not None else "")
                + "</itemList>"
                for order in route_orders
            ))
        arrival_requests += 1
        return _bus_response("""
          <itemList><stId>111000299</stId><arsId>12390</arsId><stNm>구산동사거리</stNm>
            <dir>숭실대</dir><staOrd>9</staOrd><arrmsg1>8분 후 도착</arrmsg1></itemList>
          <itemList><stId>111000299</stId><arsId>12390</arsId><stNm>구산동사거리</stNm>
            <dir>구산동</dir><staOrd>40</staOrd><arrmsg1>3분 후 도착</arrmsg1></itemList>
        """)

    body = BusArrivalService(_settings(tmp_path), opener=opener).lookup_nearby("753", 37.5, 127.0)

    assert arrival_requests == 1
    assert body["matched_station_count"] == 1
    assert [match["arrival"]["station_order"] for match in body["matches"]] == expected_orders
    directions = {9: "숭실대", 40: "구산동"}
    for match, order in zip(body["matches"], expected_orders):
        assert match["arrival"]["direction"] == directions[order]
        assert match["announcement"].startswith(f"구산동사거리 정류장, {directions[order]} 방향, 753번 버스")


def test_nearby_lookup_keeps_served_stop_without_arrival_information(tmp_path: Path) -> None:
    def opener(url: str, timeout: float) -> BytesIO:
        if "getStationByPos" in url:
            return _bus_response("""
              <itemList><stationId>111000299</stationId><stationNm>구산동사거리</stationNm>
                <arsId>12390</arsId><dist>9</dist></itemList>
            """)
        if "getRouteByStation" in url:
            return _bus_response("<itemList><busRouteId>100100118</busRouteId><busRouteNm>753</busRouteNm></itemList>")
        return _bus_response("")

    body = BusArrivalService(_settings(tmp_path), opener=opener).lookup_nearby("753", 37.5, 127.0)

    assert body["matched_station_count"] == 1
    assert body["matches"][0]["route_found"] is True
    assert body["matches"][0]["arrival"] is None
    assert body["matches"][0]["announcement"] == (
        "구산동사거리 정류장, 방향 정보 없음, 753번 버스는 운행하지만 도착 예정 정보가 없습니다."
    )


def test_nearby_lookup_preserves_error_from_later_station(tmp_path: Path) -> None:
    def opener(url: str, timeout: float) -> BytesIO:
        if "getStationByPos" in url:
            return _bus_response("""
              <itemList><stationId>111000299</stationId><stationNm>구산동사거리</stationNm>
                <arsId>12390</arsId><dist>9</dist></itemList>
              <itemList><stationId>111000300</stationId><stationNm>다음정류장</stationNm>
                <arsId>12391</arsId><dist>20</dist></itemList>
            """)
        if "getRouteByStation" in url:
            if "arsId=12391" in url:
                raise HTTPError(url, 503, "Unavailable", {}, BytesIO(b""))
            return _bus_response("<itemList><busRouteId>100100118</busRouteId><busRouteNm>753</busRouteNm></itemList>")
        return _bus_response("<itemList><stId>111000299</stId><dir>숭실대</dir><arrmsg1>3분 후 도착</arrmsg1></itemList>")

    with pytest.raises(BusArrivalError) as caught:
        BusArrivalService(_settings(tmp_path), opener=opener).lookup_nearby("753", 37.5, 127.0)

    assert caught.value.status_code == 502
    assert caught.value.code == "bus_api_http_error"


def test_nearby_lookup_reports_route_not_found_within_30m(tmp_path: Path) -> None:
    requested_urls: list[str] = []

    def opener(url: str, timeout: float) -> BytesIO:
        requested_urls.append(url)
        if "getStationByPos" in url:
            return BytesIO("""<ServiceResult><msgHeader><headerCd>0</headerCd></msgHeader><msgBody>
              <itemList><stationId>111000299</stationId><stationNm>구산동사거리</stationNm>
                <arsId>12390</arsId><dist>9</dist>
              </itemList>
            </msgBody></ServiceResult>""".encode())
        return BytesIO("""<ServiceResult><msgHeader><headerCd>0</headerCd></msgHeader><msgBody>
          <itemList><busRouteId>100100118</busRouteId><busRouteNm>753</busRouteNm></itemList>
        </msgBody></ServiceResult>""".encode())

    with pytest.raises(BusArrivalError) as caught:
        BusArrivalService(_settings(tmp_path), opener=opener).lookup_nearby("105", 37.5, 127.0)

    assert caught.value.code == "bus_route_not_nearby"
    assert caught.value.message == "현재 위치 30m 안에 105번 노선 정류장이 없습니다."
    assert len(requested_urls) == 2


def test_nearby_lookup_rejects_station_farther_than_limit(tmp_path: Path) -> None:
    requested_urls: list[str] = []

    def opener(url: str, timeout: float) -> BytesIO:
        requested_urls.append(url)
        return BytesIO("""<ServiceResult><msgHeader><headerCd>0</headerCd></msgHeader><msgBody>
          <itemList><stationId>111000299</stationId><stationNm>구산동사거리</stationNm>
            <arsId>12390</arsId><dist>30.1</dist>
          </itemList>
        </msgBody></ServiceResult>""".encode())

    service = BusArrivalService(_settings(tmp_path), opener=opener)

    with pytest.raises(BusArrivalError) as caught:
        service.lookup_nearby("753", 37.5, 127.0, 10)

    assert caught.value.status_code == 404
    assert caught.value.code == "nearby_station_not_found"
    assert caught.value.message == "현재 위치 30m 안에 정류장이 없습니다."
    assert len(requested_urls) == 1


def test_nearby_lookup_ignores_route_station_outside_30m(tmp_path: Path) -> None:
    requested_urls: list[str] = []

    def opener(url: str, timeout: float) -> BytesIO:
        requested_urls.append(url)
        return BytesIO("""<ServiceResult><msgHeader><headerCd>0</headerCd></msgHeader><msgBody>
          <itemList><stationId>111000181</stationId><stationNm>다른정류장</stationNm>
            <arsId>12271</arsId><dist>31</dist>
          </itemList>
        </msgBody></ServiceResult>""".encode())

    service = BusArrivalService(_settings(tmp_path), opener=opener)

    with pytest.raises(BusArrivalError) as caught:
        service.lookup_nearby("753", 37.5, 127.0, 10)

    assert caught.value.status_code == 404
    assert caught.value.code == "nearby_station_not_found"
    assert caught.value.message == "현재 위치 30m 안에 정류장이 없습니다."
    assert len(requested_urls) == 1


def test_missing_service_key_returns_503(tmp_path: Path) -> None:
    service = BusArrivalService(_settings(tmp_path, service_key=""))

    with pytest.raises(BusArrivalError) as caught:
        service.lookup("100100118", "구산동사거리")

    assert caught.value.status_code == 503
    assert caught.value.code == "bus_api_unconfigured"


def test_rejected_service_key_preserves_upstream_message(tmp_path: Path) -> None:
    def rejected(url: str, timeout: float) -> BytesIO:
        body = BytesIO('{"message":"유효하지 않은 서비스키입니다: 등록되지 않은 서비스키"}'.encode())
        raise HTTPError(url, 401, "Unauthorized", {}, body)

    service = BusArrivalService(_settings(tmp_path), opener=rejected)

    with pytest.raises(BusArrivalError) as caught:
        service.lookup("100100118", "구산동사거리")

    assert caught.value.status_code == 502
    assert caught.value.code == "bus_api_auth_failed"
    assert "버스도착정보조회" in caught.value.message
    assert "등록되지 않은 서비스키" in caught.value.message


def test_bus_speech_returns_and_caches_korean_mp3() -> None:
    requested_urls: list[str] = []
    mp3 = b"\xff\xf3\x84" + (b"\x00" * 32)

    def opener(request, timeout: float) -> BytesIO:
        requested_urls.append(request.full_url)
        assert timeout == 10
        assert request.headers["User-agent"] == "Mozilla/5.0"
        return BytesIO(mp3)

    service = BusSpeechService(opener=opener)
    first = service.synthesize("마포07번 버스가 3분 후 도착합니다.")
    second = service.synthesize("  마포07번  버스가 3분 후 도착합니다. ")

    assert first == mp3
    assert second == mp3
    assert len(requested_urls) == 1
    assert "translate_tts" in requested_urls[0]
    assert "tl=ko" in requested_urls[0]
    assert "%EB%A7%88%ED%8F%AC07" in requested_urls[0]


def test_bus_speech_rejects_invalid_audio() -> None:
    service = BusSpeechService(opener=lambda request, timeout: BytesIO(b"not-mp3"))

    with pytest.raises(BusSpeechError) as caught:
        service.synthesize("버스 안내")

    assert caught.value.status_code == 502
    assert caught.value.code == "bus_speech_invalid_response"

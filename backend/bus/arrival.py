"""현재 위치 주변에서 입력한 노선이 정차하는 정류장과 도착정보를 조회한다."""
from __future__ import annotations

import json
import math
import re
from threading import Lock
from time import monotonic
from collections.abc import Callable
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urlencode
from urllib.request import urlopen
from xml.etree import ElementTree

from .config import BusServiceSettings as Settings


class BusArrivalError(RuntimeError):
    def __init__(self, status_code: int, code: str, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


class BusArrivalService:
    nearby_stations_url = "http://ws.bus.go.kr/api/rest/stationinfo/getStationByPos"
    station_routes_url = "http://ws.bus.go.kr/api/rest/stationinfo/getRouteByStation"
    route_arrivals_url = "http://ws.bus.go.kr/api/rest/arrive/getArrInfoByRouteAll"
    static_cache_ttl_sec = 300
    station_cache_coverage_m = 30

    def __init__(self, settings: Settings, opener: Callable[..., object] = urlopen,
                 clock: Callable[[], float] = monotonic):
        self.service_key = settings.seoul_bus_api_key.strip()
        self.timeout = settings.seoul_bus_api_timeout_sec
        self.station_radius_m = settings.seoul_bus_station_radius_m
        self.max_station_distance_m = settings.seoul_bus_max_station_distance_m
        self.opener = opener
        self.clock = clock
        self._cache_lock = Lock()
        self._nearby_cache: list[tuple[float, float, float, float, list[ElementTree.Element]]] = []
        self._routes_cache: dict[str, tuple[float, list[ElementTree.Element]]] = {}

    def _nearby_stations(
        self, latitude: float, longitude: float, radius: int,
    ) -> tuple[list[ElementTree.Element], bool]:
        now = self.clock()
        with self._cache_lock:
            self._nearby_cache = [entry for entry in self._nearby_cache if entry[0] > now]
            for expires, center_lat, center_lon, coverage, items in reversed(self._nearby_cache):
                if self._coordinate_distance(latitude, longitude, center_lat, center_lon) <= coverage:
                    return items, True
        # 캐시 중심에서 조금 움직여도 원래 30m 탐색 영역을 모두 포함한다.
        request_radius = min(1000, radius + self.station_cache_coverage_m)
        items = self._request(
            "정류소정보조회", self.nearby_stations_url,
            tmX=f"{longitude:.7f}", tmY=f"{latitude:.7f}", radius=str(request_radius),
        )
        if all(self._station_coordinates(item) is not None for item in items):
            with self._cache_lock:
                self._nearby_cache.append((now + self.static_cache_ttl_sec, latitude, longitude,
                                           request_radius - radius, items))
                self._nearby_cache = self._nearby_cache[-32:]
        return items, False

    def _station_routes(self, ars_id: str) -> list[ElementTree.Element]:
        now = self.clock()
        with self._cache_lock:
            cached = self._routes_cache.get(ars_id)
            if cached and cached[0] > now:
                return cached[1]
        items = self._request("정류소정보조회", self.station_routes_url, arsId=ars_id)
        with self._cache_lock:
            self._routes_cache[ars_id] = (now + self.static_cache_ttl_sec, items)
            if len(self._routes_cache) > 256:
                self._routes_cache = {key: value for key, value in self._routes_cache.items() if value[0] > now}
                while len(self._routes_cache) > 256:
                    self._routes_cache.pop(next(iter(self._routes_cache)))
        return items

    def lookup_nearby(
        self,
        bus_number: str,
        latitude: float,
        longitude: float,
        accuracy_m: float | None = None,
    ) -> dict:
        """설정된 거리 안에서 입력한 노선이 정차하는 모든 정류장을 찾는다."""
        bus_number = bus_number.strip()
        if not bus_number:
            raise BusArrivalError(400, "invalid_bus_query", "버스 번호를 입력하세요")
        if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
            raise BusArrivalError(400, "invalid_location", "위도 또는 경도 범위가 올바르지 않습니다")
        if not self.service_key:
            raise BusArrivalError(503, "bus_api_unconfigured", "서울시 버스 API 인증키가 설정되지 않았습니다")

        radius = min(max(self.station_radius_m, 1), 1000)
        stations, cached_stations = self._nearby_stations(latitude, longitude, radius)
        nearby = sorted(
            (
                (item, self._station_distance(item, latitude, longitude, use_reported=not cached_stations))
                for item in stations
            ),
            key=lambda entry: entry[1],
        )
        nearby = [entry for entry in nearby if math.isfinite(entry[1]) and entry[1] <= self.max_station_distance_m]
        if not nearby:
            raise BusArrivalError(
                404,
                "nearby_station_not_found",
                f"현재 위치 {self.max_station_distance_m:g}m 안에 정류장이 없습니다.",
            )

        matches = []
        seen_stations: set[str] = set()
        matched_stations: set[str] = set()
        arrivals_by_route: dict[str, list[ElementTree.Element]] = {}
        for candidate, candidate_distance in nearby:
            ars_id = self._text(candidate, "arsId")
            station_name = self._text(candidate, "stationNm") or self._text(candidate, "stNm")
            if not ars_id or not station_name or ars_id in seen_stations:
                continue
            seen_stations.add(ars_id)
            routes = self._station_routes(ars_id)
            candidate_routes = [
                item
                for item in routes
                if self._same_name(
                    self._text(item, "busRouteNm") or self._text(item, "rtNm"),
                    bus_number,
                )
            ]
            if not candidate_routes:
                continue
            station_id = self._text(candidate, "stationId") or self._text(candidate, "stId")
            station_result = {
                "station_id": station_id,
                "station_name": station_name,
                "ars_id": ars_id,
                "distance_m": round(candidate_distance, 1),
                "latitude": self._float(candidate, "gpsY") or self._float(candidate, "posY"),
                "longitude": self._float(candidate, "gpsX") or self._float(candidate, "posX"),
            }
            seen_arrivals: set[tuple[str, int | None, str]] = set()
            for route in candidate_routes:
                route_id = self._text(route, "busRouteId")
                if not route_id:
                    raise BusArrivalError(502, "bus_api_invalid_response", "경유 노선의 서울시 노선 ID가 없습니다")
                if route_id not in arrivals_by_route:
                    arrivals_by_route[route_id] = self._request(
                        "버스도착정보조회", self.route_arrivals_url, busRouteId=route_id,
                    )
                station_order = self._integer(route, "staOrd")
                # 순환 노선은 같은 정류장을 여러 번 지나므로 정차 순서도 확인한다.
                arrival_items = [
                    item
                    for item in arrivals_by_route[route_id]
                    if (
                        (station_id and self._text(item, "stId") == station_id)
                        or self._text(item, "arsId") == ars_id
                    )
                    and (station_order is None or self._integer(item, "staOrd") == station_order)
                ]
                for arrival_item in arrival_items or [None]:
                    arrival = self._arrival_result(
                        arrival_item, route_id, arrivals_by_route[route_id],
                    ) if arrival_item is not None else None
                    arrival_key = (
                        route_id,
                        arrival["station_order"] if arrival else station_order,
                        arrival["direction"] if arrival else "",
                    )
                    if arrival_key in seen_arrivals:
                        continue
                    seen_arrivals.add(arrival_key)
                    matches.append({
                        "station": station_result,
                        "bus_route_id": route_id,
                        "route_found": True,
                        "arrival": arrival,
                        "announcement": self._arrival_announcement(bus_number, station_name, arrival),
                    })
                    matched_stations.add(ars_id)

        if not matches:
            raise BusArrivalError(
                404,
                "bus_route_not_nearby",
                f"현재 위치 {self.max_station_distance_m:g}m 안에 {bus_number}번 노선 정류장이 없습니다.",
            )

        first_match = matches[0]
        return {
            "bus_number": bus_number,
            "bus_route_id": first_match["bus_route_id"],
            "station": first_match["station"],
            "search_radius_m": self.max_station_distance_m,
            "route_found": True,
            "arrival": first_match["arrival"],
            "announcement": " ".join(match["announcement"] for match in matches),
            "matches": matches,
            "matched_station_count": len(matched_stations),
        }

    def lookup(self, bus_route_id: str, station_name: str) -> dict:
        bus_route_id = bus_route_id.strip()
        station_name = station_name.strip()
        if not bus_route_id.isdigit() or not station_name:
            raise BusArrivalError(400, "invalid_bus_query", "숫자 노선 ID와 정류장 이름을 입력하세요")
        if not self.service_key:
            raise BusArrivalError(503, "bus_api_unconfigured", "서울시 버스 API 인증키가 설정되지 않았습니다")

        arrivals = self._request("버스도착정보조회", self.route_arrivals_url, busRouteId=bus_route_id)
        if not arrivals:
            raise BusArrivalError(404, "bus_route_not_found", f"노선 ID {bus_route_id}의 도착정보를 찾지 못했습니다")

        route_name = self._text(arrivals[0], "rtNm") or self._text(arrivals[0], "busRouteAbrv")
        matches = [
            self._arrival_result(item, bus_route_id, arrivals)
            for item in arrivals
            if self._same_name(self._text(item, "stNm"), station_name)
        ]

        return {
            "bus_route_id": bus_route_id,
            "bus_number": route_name,
            "station_name": station_name,
            "serves_stop": bool(matches),
            "matches": matches,
        }

    def _arrival_result(
        self,
        item: ElementTree.Element,
        route_id: str,
        route_arrivals: list[ElementTree.Element] | None = None,
    ) -> dict:
        direction = self._text(item, "dir") or self._text(item, "adirection")
        direction_source = "api" if direction else "unknown"
        if not direction:
            direction = self._next_station_name(item, route_id, route_arrivals or [])
            if direction:
                direction_source = "next_station"
        vehicle_id = self._text(item, "vehId1")
        if not vehicle_id or re.fullmatch(r"0+", vehicle_id):
            vehicle_id = ""
        return {
            "route_id": route_id,
            "route_name": self._text(item, "rtNm") or self._text(item, "busRouteAbrv"),
            "station_id": self._text(item, "stId"),
            "station_name": self._text(item, "stNm"),
            "ars_id": self._text(item, "arsId"),
            "direction": direction,
            "direction_source": direction_source,
            "station_order": self._integer(item, "staOrd"),
            "first_arrival": self._text(item, "arrmsg1"),
            "first_eta_seconds": self._integer(item, "exps1"),
            "first_vehicle_id": vehicle_id,
            "first_arrival_state": self._first_arrival_state(item, vehicle_id),
            "second_arrival": self._text(item, "arrmsg2"),
            "second_eta_seconds": self._integer(item, "exps2"),
            "vehicle_number": self._text(item, "plainNo1"),
        }

    def _first_arrival_state(self, item: ElementTree.Element, vehicle_id: str) -> str:
        message = re.sub(r"\s+", "", self._text(item, "arrmsg1"))
        if not message or any(word in message for word in (
            "출발대기", "운행종료", "운행대기", "회차대기", "미운행", "정보없", "정보가없",
            "정류소출발", "정류장출발", "정류소통과", "정류장통과",
        )):
            return "unavailable"
        station_order = self._integer(item, "staOrd")
        section_order = self._integer(item, "sectOrd1")
        vehicle_number = self._text(item, "plainNo1")
        identified_vehicle = bool(vehicle_id or (vehicle_number and not re.fullmatch(r"0+", vehicle_number)))
        # isArrive1은 버스가 마지막으로 보고한 정류장의 상태다. 앞 정류장 도착을
        # 사용자가 기다리는 정류장 도착으로 잘못 안내하지 않도록 순번도 확인한다.
        if (
            identified_vehicle
            and station_order is not None and station_order > 0
            and section_order == station_order
            and self._text(item, "isArrive1") == "1"
        ):
            return "arrived"
        if "곧도착" in message or "진입중" in message:
            return "arriving"
        eta_seconds = self._integer(item, "exps1")
        if (eta_seconds is not None and eta_seconds > 0) or re.search(r"\d+(?:분|초)", message):
            return "approaching"
        return "unavailable"

    def _next_station_name(
        self, item: ElementTree.Element, route_id: str, route_arrivals: list[ElementTree.Element],
    ) -> str:
        """이미 받은 노선 응답에서 바로 다음 정차 순서가 명확할 때만 방향을 보완한다."""
        station_order = self._integer(item, "staOrd")
        if station_order is None or station_order < 1:
            return ""
        # 응답 배열 순서나 같은 정류장 이름으로 짝지으면 왕복 방향이 섞일 수 있다.
        next_stops = {
            (self._text(candidate, "stId"), self._text(candidate, "arsId"), self._text(candidate, "stNm"))
            for candidate in route_arrivals
            if self._integer(candidate, "staOrd") == station_order + 1
            and self._text(candidate, "busRouteId") in {"", route_id}
        }
        if len(next_stops) != 1:
            return ""
        station_id, ars_id, station_name = next_stops.pop()
        if (
            not station_name
            or self._same_name(station_name, self._text(item, "stNm"))
            or (station_id and station_id == self._text(item, "stId"))
            or (ars_id and ars_id == self._text(item, "arsId"))
        ):
            return ""
        return station_name

    @staticmethod
    def _arrival_announcement(bus_number: str, station_name: str, arrival: dict | None) -> str:
        direction = (arrival or {}).get("direction") or ""
        direction_label = direction if direction.endswith("방향") else f"{direction} 방향" if direction else "방향 정보 없음"
        prefix = f"{station_name} 정류장, {direction_label}, {bus_number}번 버스"
        if arrival is None or not arrival["first_arrival"]:
            return f"{prefix}는 운행하지만 도착 예정 정보가 없습니다."
        if arrival.get("first_arrival_state") == "arrived":
            return f"{prefix}가 도착했습니다."
        message = arrival["first_arrival"]
        if message == "출발대기":
            return f"{prefix}는 현재 출발 대기 중입니다."
        minute_second = re.search(r"(\d+)분\s*(\d+)초\s*후", message)
        if minute_second:
            return f"{prefix}가 {minute_second.group(1)}분 {minute_second.group(2)}초 후 도착합니다."
        minute = re.search(r"(\d+)분\s*후", message)
        if minute:
            return f"{prefix}가 {minute.group(1)}분 후 도착합니다."
        if "곧 도착" in message or "진입중" in message:
            return f"{prefix}가 곧 도착합니다."
        if "운행종료" in message:
            return f"{prefix}는 운행이 종료됐습니다."
        return f"{prefix} 도착 안내는 {message}입니다."

    @classmethod
    def _station_coordinates(cls, item: ElementTree.Element) -> tuple[float, float] | None:
        latitude = cls._float(item, "gpsY")
        longitude = cls._float(item, "gpsX")
        if latitude is None:
            latitude = cls._float(item, "posY")
        if longitude is None:
            longitude = cls._float(item, "posX")
        if latitude is None or longitude is None:
            return None
        return latitude, longitude

    @staticmethod
    def _coordinate_distance(latitude: float, longitude: float,
                             station_lat: float, station_lon: float) -> float:
        radius = 6_371_000
        lat1, lat2 = math.radians(latitude), math.radians(station_lat)
        delta_lat = math.radians(station_lat - latitude)
        delta_lon = math.radians(station_lon - longitude)
        value = math.sin(delta_lat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(delta_lon / 2) ** 2
        return radius * 2 * math.atan2(math.sqrt(value), math.sqrt(1 - value))

    @classmethod
    def _station_distance(cls, item: ElementTree.Element, latitude: float, longitude: float,
                          use_reported: bool = True) -> float:
        if use_reported:
            distance = cls._float(item, "dist")
            if distance is not None:
                return distance
        coordinates = cls._station_coordinates(item)
        if coordinates is None:
            return float("inf")
        return cls._coordinate_distance(latitude, longitude, *coordinates)

    def _request(self, service_name: str, url: str, **params: str) -> list[ElementTree.Element]:
        # 포털의 Encoding/Decoding 인증키 어느 쪽을 넣어도 URL에는 한 번만 인코딩한다.
        query = urlencode({"serviceKey": unquote(self.service_key), **params})
        try:
            with self.opener(f"{url}?{query}", timeout=self.timeout) as response:
                payload = response.read()
        except HTTPError as error:
            message = self._http_error_message(service_name, error)
            if error.code in {401, 403}:
                raise BusArrivalError(502, "bus_api_auth_failed", message) from error
            raise BusArrivalError(502, "bus_api_http_error", message) from error
        except (URLError, TimeoutError, OSError) as error:
            raise BusArrivalError(
                502,
                "bus_api_unreachable",
                f"서울시 {service_name} 서비스 연결에 실패했습니다: {error}",
            ) from error

        try:
            root = ElementTree.fromstring(payload)
        except ElementTree.ParseError as error:
            raise BusArrivalError(
                502,
                "bus_api_invalid_response",
                f"서울시 {service_name} 서비스 응답 형식이 올바르지 않습니다",
            ) from error

        header = root.find(".//msgHeader")
        if header is None:
            header = root.find(".//header")
        result_code = self._text(header, "headerCd")
        if result_code and result_code != "0":
            message = self._text(header, "headerMsg") or "알 수 없는 오류"
            raise BusArrivalError(502, "bus_api_error", f"서울시 {service_name} 서비스 오류({result_code}): {message}")
        return list(root.findall(".//itemList"))

    @staticmethod
    def _http_error_message(service_name: str, error: HTTPError) -> str:
        try:
            body = error.read().decode("utf-8", "replace")
            data = json.loads(body)
            detail = str(data.get("message") or data.get("error") or "").strip()
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, AttributeError):
            detail = ""
        if detail:
            return f"서울시 {service_name} 서비스 인증 또는 요청이 거부되었습니다: {detail}"
        return f"서울시 {service_name} 서비스가 HTTP {error.code} 오류를 반환했습니다"

    @staticmethod
    def _same_name(actual: str, expected: str) -> bool:
        return "".join(actual.split()).casefold() == "".join(expected.split()).casefold()

    @staticmethod
    def _text(item: ElementTree.Element | None, field: str) -> str:
        if item is None:
            return ""
        return (item.findtext(field) or "").strip()

    @classmethod
    def _integer(cls, item: ElementTree.Element, field: str) -> int | None:
        value = cls._text(item, field)
        try:
            return int(value) if value else None
        except ValueError:
            return None

    @classmethod
    def _float(cls, item: ElementTree.Element, field: str) -> float | None:
        value = cls._text(item, field)
        try:
            return float(value) if value else None
        except ValueError:
            return None

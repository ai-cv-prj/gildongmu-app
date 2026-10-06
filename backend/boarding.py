"""Session-scoped bus input, armed only after the arrival stop clip completes."""


class BoardingError(ValueError):
    """An input action does not belong to the current arrival."""


class Boarding:
    def __init__(self):
        self.status = "searching"
        self.arrival_event_id = None
        self.arrival_source = None
        self._next_arrival_event_id = 0
        self.bus_number = None
        self.revision = 0

    @property
    def stationary(self):
        return self.status == "pending"

    @property
    def at_stop(self):
        return self.status in ("awaiting_stop", "pending", "submitted")

    def snapshot(self):
        return {"status": self.status, "arrival_event_id": self.arrival_event_id,
                "arrival_source": self.arrival_source,
                "bus_number": self.bus_number, "revision": self.revision,
                "assumed_stationary": self.stationary,
                "stop_hazard_suppressed": self.at_stop,
                "obstacle_detection_enabled": not self.at_stop}

    def observe(self, proximity, *, crossing_active=False):
        # A historical arrival alone must not open the form after leaving a stop.
        if (self.status == "searching" and proximity and proximity.get("nearby")
                and proximity.get("arrival_event_id") and not crossing_active):
            self._next_arrival_event_id += 1
            self.arrival_event_id = self._next_arrival_event_id
            self.arrival_source = "visual_proximity"
            self.status = "awaiting_stop"
            self.revision += 1
        return self.snapshot()

    def act(self, action, arrival_event_id=None, bus_number=None, *, crossing_active=False):
        if action == "arrive":
            if crossing_active:
                raise BoardingError("횡단 중에는 버스 탑승 입력을 시작할 수 없습니다.")
            if self.status in ("awaiting_stop", "pending"):
                return self.snapshot()
            self._next_arrival_event_id += 1
            self.arrival_event_id = self._next_arrival_event_id
            self.arrival_source = "user_confirmed"
            self.bus_number = None
            self.status = "awaiting_stop"
            self.revision += 1
            return self.snapshot()
        if self.arrival_event_id is None or arrival_event_id != self.arrival_event_id:
            raise BoardingError("현재 정류장 도착에 해당하는 요청이 아닙니다.")
        before = (self.status, self.bus_number)
        if action == "stop_announced":
            # A delayed acknowledgement must never reopen a cancelled/submitted form.
            if self.status == "awaiting_stop":
                self.status = "pending"
        elif action == "submit":
            if not isinstance(bus_number, str):
                raise BoardingError("탑승할 버스 번호를 입력해 주세요.")
            number = " ".join(bus_number.split())
            if (not number or len(number) > 30 or
                    any(ord(char) < 32 or ord(char) == 127 for char in bus_number)):
                raise BoardingError("버스 번호는 1~30자의 문자로 입력해 주세요.")
            if self.status == "submitted" and self.bus_number == number:
                return self.snapshot()
            if self.status != "pending":
                raise BoardingError("버스 번호 입력 화면을 먼저 열어 주세요.")
            self.bus_number = number
            self.status = "submitted"
        elif action == "cancel":
            if self.status not in ("awaiting_stop", "pending", "cancelled"):
                raise BoardingError("현재 취소할 버스 번호 입력이 없습니다.")
            self.bus_number = None
            self.status = "cancelled"
        elif action == "reopen":
            if crossing_active:
                raise BoardingError("횡단 중에는 버스 탑승 입력을 시작할 수 없습니다.")
            if self.status in ("submitted", "cancelled"):
                # Ask for another stop clip before assuming the user stopped again.
                # Editing is an explicit user action, so the old stop box need
                # not stay visible when the camera is now pointed at a bus.
                self.arrival_source = "user_confirmed"
                self.status = "awaiting_stop"
            elif self.status not in ("awaiting_stop", "pending"):
                raise BoardingError("확인된 정류장 도착이 없습니다.")
        else:
            raise BoardingError("지원하지 않는 탑승 입력 요청입니다.")
        if before != (self.status, self.bus_number):
            self.revision += 1
        return self.snapshot()

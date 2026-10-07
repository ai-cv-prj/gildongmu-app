"""
file_path: src/risk_visualization.py

Additional risk overlay; existing class colors and traffic drawing are untouched.
"""
import cv2
import numpy as np
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

# 실시간 UI의 RGB 색상을 OpenCV BGR 순서로 변환한 값
COLORS = {"monitor": (180,180,180), "caution": (102,183,255), "danger": (113,101,255)}
PATH_ROI_COLOR = (250, 227, 76)
NEAR_ROI_COLOR = (186, 136, 255)
DARK_TEXT_COLOR = (31, 19, 8)
KOREAN_FONT_PATHS = (
    Path("/mnt/c/Windows/Fonts/malgunbd.ttf"),
    Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc"),
    Path("/usr/share/fonts/truetype/nanum/NanumGothicBold.ttf"),
)


# 장애물 행동과 음성·움직임 상태 문구 생성
def action_status_text(prediction):
    """화면 행동과 음성 선택, 카메라 움직임 상태를 짧은 문구로 반환한다."""
    action = prediction.get("last_action") or "none"
    voice = prediction.get("voice_playback_action") or "none"
    motion = (prediction.get("stationarity") or {}).get("status") or "unavailable"
    return f"ACTION: {action}", f"VOICE: {voice}", f"MOTION: {motion}"


# 현재 장애물 이동 행동 배지 표시
def draw_action_status(frame, prediction):
    """영상 왼쪽 위에 현재 장애물 이동 행동과 음성 상태를 검은 배지로 표시한다."""
    lines = action_status_text(prediction)
    height, width = frame.shape[:2]
    font = cv2.FONT_HERSHEY_SIMPLEX
    scale, thickness, padding = max(.45, width / 760), 1, 8
    sizes = [cv2.getTextSize(text, font, scale, thickness) for text in lines]
    text_width = max(size[0][0] for size in sizes)
    text_height = max(size[0][1] for size in sizes)
    baseline = max(size[1] for size in sizes)
    line_height = text_height + baseline + padding
    left, top = 8, 8
    right = min(width - 1, left + text_width + padding * 2)
    bottom = min(height - 1, top + line_height * len(lines) + padding)
    cv2.rectangle(frame, (left, top), (right, bottom), (24, 24, 24), -1)
    for index, text in enumerate(lines):
        y = top + padding + text_height + index * line_height
        cv2.putText(frame, text, (left + padding, y), font, scale,
                    (255, 255, 255), thickness, cv2.LINE_AA)
    return frame


# 추적과 위험 이벤트 식별자 표시
def risk_identity(item):
    """표시 가능한 추적 ID와 이벤트 ID를 짧은 문자열로 묶는다."""
    values = []
    if item.get("track_id") is not None:
        values.append(f'T{item["track_id"]}')
    if item.get("event_id") is not None:
        values.append(f'E{item["event_id"]}')
    return " · ".join(values) if values else "no-ID"


# 정지 중 음성 안내 범위 경계 표시
def draw_stationary_voice_boundary(frame, polygon):
    """분홍색 ROI의 아래쪽 절반이 시작되는 위치를 점선으로 표시한다."""
    if not isinstance(polygon, (list, tuple)) or len(polygon) < 3:
        return frame
    try:
        points = [(float(point[0]), float(point[1])) for point in polygon]
    except (TypeError, ValueError, IndexError):
        return frame
    if not np.isfinite(points).all():
        return frame
    middle_y = (min(point[1] for point in points) + max(point[1] for point in points)) / 2
    intersections = []
    for index, (x1, y1) in enumerate(points):
        x2, y2 = points[(index + 1) % len(points)]
        if abs(y2 - y1) < 1e-9:
            if abs(middle_y - y1) < 1e-9:
                intersections.extend((x1, x2))
            continue
        if min(y1, y2) <= middle_y <= max(y1, y2):
            intersections.append(x1 + (middle_y - y1) * (x2 - x1) / (y2 - y1))
    if len(intersections) < 2:
        return frame
    height, width = frame.shape[:2]
    left = round(min(intersections) * width)
    right = round(max(intersections) * width)
    y = round(middle_y * height)
    dash, gap = max(8, round(width / 45)), max(6, round(width / 60))
    for start in range(left, right, dash + gap):
        cv2.line(frame, (start, y), (min(start + dash, right), y), NEAR_ROI_COLOR,
                 max(2, round(width / 320)), cv2.LINE_AA)
    return frame


# 정류장 근접 판정 문구 생성
def stop_proximity_text(event):
    """실시간 오버레이와 같은 정류장 판정 문구를 반환한다."""
    event = event or {}
    state = event.get("status", "unavailable")
    recorded_undetected = state == "not_detected" and event.get("arrival_recorded")
    position = {"left": "좌측 ", "right": "우측 ", "bottom": "하단 "}.get(
        event.get("basis"), "")
    if state == "nearby":
        return f"{position}정류장 {'근접 유지 · 재확인 중' if event.get('held') else '근접 추정'}"
    if state == "candidate":
        if event.get("arrival_recorded"):
            return "정류장 후보 재확인 중"
        if event.get("held"):
            return f"{position}정류장 후보 유지"
        return (f"{position}정류장 후보 {event.get('observations', 0)}/"
                f"{event.get('required_observations', 0)}")
    if recorded_undetected:
        return "정류장 도착 기록 있음 · 현재 화면에서 미검출"
    if state == "not_detected":
        return "정류장 미검출"
    return "정류장 판정 보류"


# 저장 영상용 한글 글꼴 조회
def korean_font(size):
    """현재 실행 환경에서 사용할 수 있는 굵은 한글 글꼴을 반환한다."""
    for path in KOREAN_FONT_PATHS:
        if path.is_file():
            return ImageFont.truetype(str(path), size)
    return None


# OpenCV 프레임에 한글 문구 표시
def draw_korean_lines(frame, lines, position, size, color):
    """Pillow 한글 글꼴이 있으면 BGR 프레임 위에 여러 줄 문구를 표시한다."""
    font = korean_font(size)
    if font is None:
        return False
    image = Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
    draw = ImageDraw.Draw(image)
    x, y = position
    line_height = size + 7
    rgb = (color[2], color[1], color[0])
    for index, line in enumerate(lines):
        draw.text((x, y + index * line_height), line, font=font, fill=rgb)
    frame[:] = cv2.cvtColor(np.asarray(image), cv2.COLOR_RGB2BGR)
    return True


# 정류장 근접 판정 표시
def draw_stop_proximity(frame, event, crosswalk=None, walking_surface=None):
    """실시간 화면과 같은 정류장 후보 박스와 우측 배너를 표시한다."""
    event = event or {}
    result = frame.copy()
    height, width = result.shape[:2]
    state = event.get("status", "unavailable")
    color = ((171, 215, 87) if state == "nearby"
             else (115, 206, 255) if state == "candidate" else (218, 206, 194))
    box = event.get("xyxy")
    if (not event.get("held") and state in ("nearby", "candidate")
            and isinstance(box, (list, tuple)) and len(box) == 4
            and np.isfinite(box).all()):
        x1, y1, x2, y2 = [round(value * scale)
                          for value, scale in zip(box, (width, height, width, height))]
        cv2.rectangle(result, (x1, y1), (x2, y2), color, max(4, round(width / 120)))
    text = stop_proximity_text(event)
    lines = (["정류장 도착 기록 있음 ·", "현재 화면에서 미검출"]
             if state == "not_detected" and event.get("arrival_recorded") else [text])
    font_size = round(max(14, min(20, width / 32)))
    font = korean_font(font_size)
    if font is not None:
        probe = ImageDraw.Draw(Image.new("RGB", (1, 1)))
        text_width = max(round(probe.textbbox((0, 0), line, font=font)[2]) for line in lines)
    else:
        text_width = max(len(line) for line in lines) * font_size
    banner_width = min(width - 16, text_width + 22)
    banner_height = 54 if len(lines) == 2 else 36
    status_height = max(26, height / 24)
    right_badges = sum(
        bool(item and item.get("status") and item.get("status") != "disabled")
        for item in (crosswalk, walking_surface)
    )
    action_height = max(26, height / 24) * 2
    left = width - banner_width - 8
    top = round(8 + max(action_height, right_badges * (status_height + 8)) + 8)
    overlay = result.copy()
    cv2.rectangle(overlay, (left, top), (left + banner_width, top + banner_height),
                  DARK_TEXT_COLOR, -1)
    cv2.addWeighted(overlay, 235 / 255, result, 20 / 255, 0, result)
    cv2.rectangle(result, (left, top), (left + banner_width, top + banner_height), color, 2)
    if not draw_korean_lines(result, lines, (left + 11, top + 7), font_size, color):
        fallback = "STOP STATUS"
        cv2.putText(result, fallback, (left + 11, top + 24), cv2.FONT_HERSHEY_SIMPLEX,
                    .5, color, 1, cv2.LINE_AA)
    return result

# 영상에 위험 판정 표시
def draw_risk(frame, prediction, config):
    """현재 ROI와 장애물 위험도를 영상 프레임에 표시한다."""
    config = dict(config)
    for key in ("corridor_polygon", "immediate_polygon"):
        if prediction.get("roi"):
            config[key] = prediction["roi"][key]
    if config.get("review_overlay", False):
        return draw_review(frame, prediction, config)
    result = draw_scene_regions(frame, prediction)
    h, w = frame.shape[:2]
    if config["draw_roi"] and (prediction.get("camera_view") or {}).get("status") != "unavailable":
        for key, color, alpha in (("corridor_polygon", PATH_ROI_COLOR, 0.125),
                                  ("immediate_polygon", NEAR_ROI_COLOR, 0.14)):
            points = np.rint(np.asarray(config[key])*[w-1,h-1]).astype(np.int32)
            tint = result.copy()
            cv2.fillPoly(tint, [points], color)
            result = cv2.addWeighted(tint, alpha, result, 1-alpha, 0)
            cv2.polylines(result, [points], True, color, max(2, round(w/320)), cv2.LINE_AA)
        result = draw_stationary_voice_boundary(result, config["immediate_polygon"])
    counts = {"monitor":0,"caution":0,"danger":0}
    labels = []
    for item in prediction["detections"]:
        level = item.get("alert_level", item["risk_level"])
        if level is None:
            continue
        if level == "monitor" or item.get("warning_primary", True):
            counts[level] += 1
        x1,y1,x2,y2 = item["xyxy"]
        if not np.isfinite([x1,y1,x2,y2]).all():
            continue
        color = COLORS.get(level, COLORS["monitor"])
        x1, y1 = max(0, min(w-1, round(x1))), max(0, min(h-1, round(y1)))
        x2, y2 = max(0, min(w-1, round(x2))), max(0, min(h-1, round(y2)))
        if x2 <= x1 or y2 <= y1:
            continue
        cv2.rectangle(result, (x1, y1), (x2, y2), color,
                      4 if level in ("caution", "danger") else 2)
        identity = risk_identity(item)
        name = item.get("display_label") or item.get("class_name") or "obstacle"
        text = f"{name} | {identity}" if identity != "no-ID" else name
        scale = max(.45, w/1440)
        (tw, th), baseline = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, scale, 1)
        label_y = max(0, y1-th-baseline-10)
        labels.append((text, color, x1, label_y, tw+12, th+baseline+8, scale, baseline))
    for text, color, x, y, width, height, scale, baseline in labels:
        cv2.rectangle(result, (x, y), (min(w-1, x+width), min(h-1, y+height)), color, -1)
        cv2.putText(result, text, (x+6, y+height-baseline-3), cv2.FONT_HERSHEY_SIMPLEX,
                    scale, DARK_TEXT_COLOR, 1, cv2.LINE_AA)
    return draw_action_status(result, prediction)


# 검토용 위험 화면 생성
def draw_review(frame, prediction, config):
    """검토용 ROI와 위험 카드 및 상태 요약을 그린다."""
    """Large, temporary inspection overlay. Red/amber outlines encode risk, not class."""
    result = draw_scene_regions(frame, prediction)
    h, w = frame.shape[:2]
    scale = max(.45, w/1080)
    thick = max(3, round(7*scale))
    font = cv2.FONT_HERSHEY_SIMPLEX
    # 검토용 글자 표시
    def text(message, position, size, color, weight=2):
        """검은 외곽선을 포함한 문구를 프레임에 그린다."""
        cv2.putText(result, message, position, font, size, (0,0,0), weight+3, cv2.LINE_AA)
        cv2.putText(result, message, position, font, size, color, weight, cv2.LINE_AA)

    if config["draw_roi"] and (prediction.get("camera_view") or {}).get("status") != "unavailable":
        for key, color, alpha, label in (
            ("corridor_polygon",(255,220,20),.09,"PATH ROI"),
            ("immediate_polygon",(220,40,250),.14,"NEAR ROI")):
            points = np.rint(np.asarray(config[key])*[w-1,h-1]).astype(np.int32)
            overlay = result.copy()
            cv2.fillPoly(overlay,[points],color)
            result = cv2.addWeighted(overlay,alpha,result,1-alpha,0)
            cv2.polylines(result,[points],True,(0,0,0),thick+4,cv2.LINE_AA)
            cv2.polylines(result,[points],True,color,thick,cv2.LINE_AA)
            left, top = points[0]
            text(label,(max(8,int(left)),max(30,int(top)-14)),.9*scale,color,max(1,round(2*scale)))
    counts = {"monitor":0,"caution":0,"danger":0}
    occupied = []
    cards = []
    levels = {"monitor":0,"caution":1,"danger":2}
    items = sorted(prediction["detections"],key=lambda x:levels.get(x.get("alert_level",x["risk_level"]),-1),reverse=True)
    for item in items:
        level = item.get("alert_level",item["risk_level"])
        if level is None:
            continue
        if level == "monitor" or item.get("warning_primary", True):
            counts[level] += 1
        box = item["xyxy"]
        if not np.isfinite(box).all():
            continue
        x1,y1,x2,y2 = map(int,np.rint(box))
        x1,x2 = max(0,min(w-1,x1)),max(0,min(w-1,x2))
        y1,y2 = max(0,min(h-1,y1)),max(0,min(h-1,y2))
        if x2 <= x1 or y2 <= y1:
            continue
        color = COLORS[level] if level != "monitor" else (200,200,200)
        stroke = thick if level != "monitor" else max(1,thick//3)
        if level != "monitor":
            cv2.rectangle(result,(x1,y1),(x2,y2),(0,0,0),stroke+4)
        cv2.rectangle(result,(x1,y1),(x2,y2),color,stroke)
        if level != "monitor" and not item.get("warning_primary", True):
            continue
        identity = risk_identity(item)
        display_level = "UNKNOWN" if item.get("alert_status") == "uncertain" and level == "monitor" else level.upper()
        grouped = f' x{item.get("warning_group_size",1)}' if item.get("warning_group_size",1)>1 else ""
        first = f'{display_level} | {item.get("display_label", "obstacle")} {identity}{grouped}'
        motion = item.get("motion") or {}
        detail = ""
        if level != "monitor":
            ttc = motion.get("ttc_scale_s")
            expansion = motion.get("relative_expansion_per_s")
            prox=(item.get("proximity") or {}).get("band","unknown").upper()
            reasons=item.get("reasons",[])
            prox = ("SIDE CLOSE" if "side_close_candidate" in reasons else
                    "CLOSE CANDIDATE" if "large_static_candidate" in reasons else
                    "NEAR CONTACT" if "static_near_contact" in reasons else
                    "NEAR PATH" if "near_path_occupied" in reasons else "PATH")
            detail = prox + " | " + (f"TTC~{ttc:.1f}s" if ttc is not None else "TTC --")
            if expansion is not None:
                detail += f" | size {expansion*100:+.0f}%/s"
            if item.get("hold_reason"):
                detail += " | HOLD"
            if motion.get("time_to_corridor_s") is not None:
                detail += f' | entry~{motion["time_to_corridor_s"]:.1f}s'
        size = (.88 if level != "monitor" else .58)*scale
        weight = max(1,round((2 if level != "monitor" else 1)*scale))
        lines = [first] + ([detail] if detail else [])
        # Fit every line to the image width; do not truncate the risk word.
        while max(cv2.getTextSize(line,font,size,weight)[0][0] for line in lines) > w-20 and size > .3:
            size *= .9
        line_height = max(18,round(34*scale))
        label_height = line_height*len(lines)+10
        label_width = min(w-12,max(cv2.getTextSize(line,font,size,weight)[0][0] for line in lines)+14)
        left = min(x1,w-label_width-4)
        top = max(4,y1-label_height-8)
        # Avoid stacking labels over already annotated high-priority objects.
        for candidate in (top,y1+8,y2+8):
            candidate=max(4,min(h-label_height-155,candidate))
            rect=(left,candidate,left+label_width,candidate+label_height)
            if not any(rect[0]<r[2] and rect[2]>r[0] and rect[1]<r[3] and rect[3]>r[1] for r in occupied):
                top=candidate
                break
        occupied.append((left,top,left+label_width,top+label_height))
        cards.append((left,top,label_width,label_height,color,lines,line_height,size,weight))
    # Draw labels after all boxes, with the highest-risk cards last.
    for left,top,label_width,label_height,color,lines,line_height,size,weight in reversed(cards):
        cv2.rectangle(result,(left,top),(left+label_width,top+label_height),(15,15,15),-1)
        cv2.rectangle(result,(left,top),(left+label_width,top+label_height),color,max(1,round(2*scale)))
        for i,line in enumerate(lines):
            cv2.putText(result,line,(left+7,top+(i+1)*line_height),font,size,color,weight,cv2.LINE_AA)
    panel_height=max(110,round(202*scale))
    cv2.rectangle(result,(0,h-panel_height),(w-1,h-1),(16,16,16),-1)
    scene_warnings=(int(bool((prediction.get("surface") or {}).get("alert_level")))
                    + int(bool(prediction.get("advisories")))
                    + int((prediction.get("camera_view") or {}).get("status") in ("uncertain", "unavailable")))
    title=f'DANGER {counts["danger"]}   CAUTION {counts["caution"]+scene_warnings}   MONITOR/UNK {counts["monitor"]}'
    text(title,(16,h-panel_height+round(34*scale)),.95*scale,(255,255,255),max(1,round(2*scale)))
    mode="ON (experimental)" if config["ttc_alerts"] else "LOG ONLY"
    roi=prediction.get("roi") or {}
    text(f'ROI: {roi.get("source","fixed")} | direction confidence: {roi.get("direction_confidence",0):.2f}',
         (16,h-panel_height+round(66*scale)),.66*scale,(255,255,255),max(1,round(scale)))
    text("TTC risk: "+mode+" | metric distance: unavailable",
         (16,h-panel_height+round(98*scale)),.66*scale,(255,255,255),max(1,round(scale)))
    stationarity=(prediction.get("stationarity") or {}).get("status","disabled")
    text("Motion: "+stationarity+" | CYAN: path   MAGENTA: near   AMBER: path check / side watch",
         (16,h-panel_height+round(130*scale)),.60*scale,(255,255,255),max(1,round(scale)))
    surface=prediction.get("surface") or {}
    state=("CAUTION" if surface.get("alert_level") else surface.get("status","off")).upper()
    if surface.get("status")=="uncertain":
        state="CAUTION / UNCERTAIN" if surface.get("alert_level") else "UNCERTAIN"
    fraction=surface.get("path_nonwalkable_fraction")
    line=f'PATH CHECK: {state}' + (f' | non-walkable {fraction:.0%}' if fraction is not None else "")
    text(line,(16,h-panel_height+round(160*scale)),.65*scale,(0,210,255),max(1,round(scale)))
    draw_scene_status(result,prediction,(16,h-panel_height+round(190*scale)),.62*scale)
    return draw_action_status(result, prediction)


# 표면 위험 영역 표시
def draw_scene_regions(frame, prediction):
    """보행불가 영역 위험 다각형을 영상에 표시한다."""
    """Draw observed semantic regions; never invent a detection box for lost tracks."""
    result=frame.copy()
    h,w=result.shape[:2]
    surface=prediction.get("surface") or {}
    if surface.get("alert_level"):
        for region in surface.get("regions",[]):
            points=np.rint(np.asarray(region["polygon"])*[w-1,h-1]).astype(np.int32)
            tint=result.copy()
            cv2.fillPoly(tint,[points],(0,190,255))
            result=cv2.addWeighted(tint,.20,result,.80,0)
            cv2.polylines(result,[points],True,(0,210,255),max(2,round(w/270)),cv2.LINE_AA)
    return result


# 장면 전체 경고 표시
def draw_scene_status(result, prediction, position, scale):
    """촬영 상태와 보행 영역 경고를 영상에 표시한다."""
    messages=[]
    camera_view=prediction.get("camera_view") or {}
    if camera_view.get("status") == "unavailable":
        messages.append("CAMERA: point forward")
    elif camera_view.get("status") == "uncertain":
        messages.append("CAMERA: steady view")
    surface=prediction.get("surface") or {}
    if surface.get("alert_level"):
        messages.append("PATH: check surroundings" if surface.get("status")=="uncertain" else "PATH: non-walkable area")
    directions=sorted({a["direction"].upper() for a in prediction.get("advisories",[])})
    if directions:
        messages.append("/".join(directions)+": nearby object out of view")
    if messages:
        text=" | ".join(messages)
        while cv2.getTextSize(text,cv2.FONT_HERSHEY_SIMPLEX,scale,1)[0][0]>result.shape[1]-24 and scale>.3:
            scale*=.9
        cv2.putText(result,text,position,cv2.FONT_HERSHEY_SIMPLEX,scale,(0,0,0),4,cv2.LINE_AA)
        cv2.putText(result,text,position,cv2.FONT_HERSHEY_SIMPLEX,scale,(0,215,255),1,cv2.LINE_AA)

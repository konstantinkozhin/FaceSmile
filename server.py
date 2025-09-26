import json, random, uuid, cv2, time as _time
import numpy as np
from dataclasses import dataclass, field
from typing import Dict, Optional, Tuple, List

from fastapi import FastAPI, WebSocket
from fastapi.responses import FileResponse
from starlette.websockets import WebSocketDisconnect
from fer import FER

# ---------------- CONFIG ----------------
PORT = 8000
THRESHOLD = 0.5            # порог зачёта эмоции
CONSECUTIVE_NEEDED = 3     # подряд кадров для зачёта
FRAME_DOWNSCALE = 0.75     # уменьшаем кадр для скорости
GAME_DURATION = 60.0       # секунд

def now(): return _time.monotonic()

# --------- Эмоции/смайлы ----------
EMOJI_MAP = {
    "angry":    {"emoji": "😡", "title": "Гнев"},
    "happy":    {"emoji": "😀", "title": "Счастье"},
    "sad":      {"emoji": "😢", "title": "Грусть"},
    "surprise": {"emoji": "😮", "title": "Удивление"},
    "neutral":  {"emoji": "😐", "title": "Нейтрально"},
}
EMO_KEYS = list(EMOJI_MAP.keys())

# --------- состояние игры ----------
@dataclass
class RoundResult:
    target: str
    seconds: float
    ts: float

@dataclass
class GameState:
    started: bool = False
    finished: bool = False
    target: Optional[str] = None
    start_ts: Optional[float] = None
    game_start_ts: Optional[float] = None
    game_end_ts: Optional[float] = None
    history: List[RoundResult] = field(default_factory=list)
    consecutive_hits: int = 0
    last_pred: Optional[str] = None
    last_conf: float = 0.0
    progress: float = 0.0     # 0..1 — уверенность по ТЕКУЩЕЙ цели
    best_snaps: Dict[str, Tuple[float, np.ndarray]] = field(default_factory=dict)

    def pick_next(self):
        keys = EMO_KEYS[:]
        if self.target in keys and len(keys) > 1:
            keys.remove(self.target)
        self.target = random.choice(keys)
        self.start_ts = now()
        self.consecutive_hits = 0
        self.progress = 0.0

# сессии по устойчивому sid
SESSIONS: Dict[str, GameState] = {}
detector = FER(mtcnn=False)

# -------------- FASTAPI --------------
app = FastAPI()

@app.get("/")
def root():
    return FileResponse("static/index.html")

# ----- утилиты -----
def detect(frame_bgr):
    """
    Возвращает:
      best_label, best_conf, has_face, raw_results(list of detections)
    """
    try:
        results = detector.detect_emotions(frame_bgr)
    except Exception:
        results = []
    has_face = len(results) > 0
    best_label, best_conf = None, 0.0
    for det in results:
        emotions = det.get("emotions", {}) or {}
        for label, conf in emotions.items():
            if label in EMOJI_MAP and conf > best_conf:
                best_label, best_conf = label, float(conf)
    return best_label, best_conf, has_face, results

def encode_jpeg_base64(img_bgr, max_h=240):
    h, w = img_bgr.shape[:2]
    scale = min(1.0, max_h/float(h))
    if scale < 0.999:
        img_bgr = cv2.resize(img_bgr, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
    ok, buf = cv2.imencode(".jpg", img_bgr, [int(cv2.IMWRITE_JPEG_QUALITY), 85])
    if not ok: return None
    import base64
    return "data:image/jpeg;base64," + base64.b64encode(buf).decode()

# -------------- WebSocket --------------
@app.websocket("/ws")
async def ws_handler(ws: WebSocket):
    await ws.accept()
    session_id: Optional[str] = None

    def get_state() -> GameState:
        nonlocal session_id
        if not session_id:
            session_id = str(uuid.uuid4())
        st = SESSIONS.get(session_id)
        if st is None:
            st = GameState()
            SESSIONS[session_id] = st
        return st

    async def send_state(st: GameState):
        remain_ms = max(0, int((st.game_end_ts - now())*1000)) if st.game_end_ts else None
        await ws.send_text(json.dumps({
            "type": "state",
            "started": st.started,
            "finished": st.finished,
            "target": st.target,
            "target_emoji": EMOJI_MAP[st.target]["emoji"] if st.target else None,
            "target_title": EMOJI_MAP[st.target]["title"] if st.target else None,
            "remain_ms": remain_ms,
            "last_pred": st.last_pred,
            "last_conf": st.last_conf,
            "progress": st.progress,  # уверенность именно по целевой эмоции
        }, ensure_ascii=False))

    async def send_final(st: GameState):
        st.finished = True
        gallery = []
        for key, (conf, frame) in st.best_snaps.items():
            data = encode_jpeg_base64(frame)
            if data:
                gallery.append({"key": key, "title": EMOJI_MAP[key]["title"],
                                "emoji": EMOJI_MAP[key]["emoji"], "data_url": data})
        await ws.send_text(json.dumps({
            "type": "final", "duration": int(GAME_DURATION),
            "total": len(st.history), "gallery": gallery
        }, ensure_ascii=False))

    try:
        while True:
            data = await ws.receive()

            # ---- текст ----
            if "text" in data and data["text"]:
                obj = json.loads(data["text"])
                if obj.get("sid"):
                    session_id = obj["sid"]
                    if session_id not in SESSIONS:
                        SESSIONS[session_id] = GameState()
                st = get_state()

                t = obj.get("type")
                if t == "hello":
                    await send_state(st)
                if t == "start":
                    st.started = True; st.finished = False
                    st.history.clear(); st.best_snaps.clear()
                    st.pick_next()
                    st.game_start_ts = now()
                    st.game_end_ts = st.game_start_ts + GAME_DURATION
                    await send_state(st)
                if t == "hb":     # heartbeat на всякий случай
                    await ws.send_text(json.dumps({"type":"hb"}))
                continue

            # ---- бинарные кадры ----
            st = get_state()
            if not st.started or st.finished:
                continue
            if st.game_end_ts and now() >= st.game_end_ts:
                await send_final(st)
                continue

            frame = cv2.imdecode(np.frombuffer(data["bytes"], np.uint8), cv2.IMREAD_COLOR)
            if frame is None:
                continue
            if FRAME_DOWNSCALE:
                frame = cv2.resize(frame, None, fx=FRAME_DOWNSCALE, fy=FRAME_DOWNSCALE)

            best_label, best_conf, has_face, results = detect(frame)
            st.last_pred, st.last_conf = best_label, best_conf

            # прогресс по ТЕКУЩЕЙ цели (даже если она не «лучшая» на кадре)
            target_conf = 0.0
            if has_face and st.target and results:
                for det in results:
                    em = det.get("emotions", {}) or {}
                    target_conf = max(target_conf, float(em.get(st.target, 0.0)))
            st.progress = target_conf if has_face else 0.0

            # зачёт раунда — строго по лучшей эмоции
            if st.target and best_label == st.target and best_conf >= THRESHOLD:
                st.consecutive_hits += 1
                if st.consecutive_hits >= CONSECUTIVE_NEEDED:
                    st.history.append(RoundResult(st.target, now()-(st.start_ts or now()), _time.time()))
                    if best_label not in st.best_snaps or best_conf > st.best_snaps[best_label][0]:
                        st.best_snaps[best_label] = (best_conf, frame.copy())
                    st.pick_next()
            else:
                st.consecutive_hits = 0

            await send_state(st)

    except WebSocketDisconnect:
        pass  # сессию не удаляем — можно переподключиться

# локальный запуск python server.py
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=7778, log_level="info")
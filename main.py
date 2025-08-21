import os
import time
import logging
import requests
import threading
from fastapi import FastAPI
from datetime import datetime, timedelta

# ================== CONFIG ==================
TB_DEMO_TOKEN = "sgkxcrqntuki8gu1oj8u"
TB_DEVICE_URL = f"https://thingsboard.cloud/api/v1/{TB_DEMO_TOKEN}/telemetry"

LAT = float(os.getenv("LAT", "10.79"))
LON = float(os.getenv("LON", "106.70"))

AI_API_URL = os.getenv("AI_API_URL", "https://api-inference.huggingface.co/models/gpt2")
HF_TOKEN = os.getenv("HF_TOKEN", "")

LOOP_INTERVAL = 300  # 5 phút

# ================== LOGGING ==================
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ================== FASTAPI ==================
app = FastAPI()

_last_payload = []

# ================== WEATHER ==================
WEATHER_CODE_MAP = {
    0: "Trời quang", 1: "Trời quang nhẹ", 2: "Có mây", 3: "Nhiều mây",
    45: "Sương mù", 48: "Sương mù đóng băng",
    51: "Mưa phùn nhẹ", 53: "Mưa phùn vừa", 55: "Mưa phùn dày",
    61: "Mưa nhẹ", 63: "Mưa vừa", 65: "Mưa to",
    71: "Tuyết nhẹ", 73: "Tuyết vừa", 75: "Tuyết dày",
    80: "Mưa rào nhẹ", 81: "Mưa rào vừa", 82: "Mưa rào mạnh",
    95: "Giông nhẹ hoặc vừa", 96: "Giông kèm mưa đá nhẹ", 99: "Giông kèm mưa đá mạnh"
}

def get_hourly_forecast_hours(n_hours=7):
    """Trả về n_hours từ giờ hiện tại"""
    now = datetime.now()
    return [now + timedelta(hours=i) for i in range(n_hours)]

def fetch_weather_hour(hour_dt):
    try:
        url = "https://api.open-meteo.com/v1/forecast"
        params = {
            "latitude": LAT,
            "longitude": LON,
            "hourly": "temperature_2m,relativehumidity_2m,weathercode",
            "timezone": "Asia/Ho_Chi_Minh",
            "start": hour_dt.strftime("%Y-%m-%dT%H:00"),
            "end": (hour_dt + timedelta(hours=1)).strftime("%Y-%m-%dT%H:00")
        }
        r = requests.get(url, params=params, timeout=10)
        r.raise_for_status()
        data = r.json()
        temp = data.get("hourly", {}).get("temperature_2m", [None])[0] or 0
        hum = data.get("hourly", {}).get("relativehumidity_2m", [None])[0] or 0
        code = data.get("hourly", {}).get("weathercode", [0])[0]
        desc = WEATHER_CODE_MAP.get(code, "?")
        return {"temperature": temp, "humidity": hum, "weather_desc": desc}
    except Exception as e:
        logger.warning(f"Weather fetch failed: {e}")
        return {"temperature": 0, "humidity": 0, "weather_desc": "?"}

# ================== AI ==================
def call_ai_api(temp, humi):
    """Trả về advice"""
    nutrition = ["Ưu tiên Kali (K)", "Cân bằng NPK", "Bón phân hữu cơ"]
    care = []
    if temp >= 35: care.append("Tránh nắng gắt, tưới sáng sớm/chiều mát")
    elif temp >= 30: care.append("Tưới đủ nước, theo dõi thường xuyên")
    elif temp <= 15: care.append("Giữ ấm, tránh sương muối")
    else: care.append("Nhiệt độ bình thường")
    if humi <= 40: care.append("Độ ẩm thấp: tăng tưới")
    elif humi <= 60: care.append("Độ ẩm hơi thấp: theo dõi, tưới khi cần")
    elif humi >= 85: care.append("Độ ẩm cao: tránh úng, kiểm tra thoát nước")
    else: care.append("Độ ẩm ổn định cho rau muống")
    advice = " | ".join(nutrition + care + ["Quan sát cây trồng và điều chỉnh thực tế"])
    return {"advice": advice}

# ================== THINGSBOARD ==================
def send_point(hour_dt, temp, hum, weather_desc, advice):
    ts = int(hour_dt.timestamp() * 1000)
    payload = [
        {"ts": ts, "key": "temperature", "value": temp},
        {"ts": ts, "key": "humidity", "value": hum},
        {"ts": ts, "key": "weather_desc", "value": weather_desc},
        {"ts": ts, "key": "advice", "value": advice},
        {"ts": ts, "key": "location", "value": "An Phú, Hồ Chí Minh"},
        {"ts": ts, "key": "crop", "value": "Rau muống"}
    ]
    try:
        r = requests.post(TB_DEVICE_URL, json=payload, timeout=10)
        logger.info(f"TB push ts={ts} status={r.status_code}")
    except Exception as e:
        logger.error(f"TB push failed: {e}")

# ================== BACKGROUND LOOP ==================
def background_loop():
    global _last_payload
    while True:
        try:
            hours = get_hourly_forecast_hours()
            points = []
            for i,h in enumerate(hours):
                w = fetch_weather_hour(h)
                advice = call_ai_api(w["temperature"], w["humidity"])["advice"]
                send_point(h, w["temperature"], w["humidity"], w["weather_desc"], advice)
                points.append({"ts": int(h.timestamp()*1000), **w, "advice": advice,
                               "location":"An Phú, Hồ Chí Minh","crop":"Rau muống"})
            _last_payload = points
        except Exception as e:
            logger.error(f"Background loop error: {e}")
        time.sleep(LOOP_INTERVAL)

threading.Thread(target=background_loop, daemon=True).start()

# ================== ROUTES ==================
@app.get("/")
def root():
    return {"status": "running"}

@app.get("/last")
def last_payload_endpoint():
    return _last_payload or {"message": "No telemetry yet"}

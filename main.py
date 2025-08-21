import os
import time
import logging
import requests
import threading
from fastapi import FastAPI
from datetime import datetime, timedelta

# ================== CONFIG ==================
TB_DEMO_TOKEN = os.getenv("TB_DEMO_TOKEN", "sgkxcrqntuki8gu1oj8u")
TB_DEVICE_ID = os.getenv("TB_DEVICE_ID", "023a1b70-7e63-11f0-a413-b38ec94d1465")
TB_DEVICE_PUSH_URL = f"https://thingsboard.cloud/api/v1/{TB_DEMO_TOKEN}/telemetry"

LAT = float(os.getenv("LAT", "10.79"))
LON = float(os.getenv("LON", "106.70"))
LOOP_INTERVAL = 300  # 5 phút

AI_API_URL = os.getenv("AI_API_URL", "https://api-inference.huggingface.co/models/gpt2")
HF_TOKEN = os.getenv("HF_TOKEN", "")

# ================== LOGGING ==================
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ================== FASTAPI ==================
app = FastAPI()
_last_payload = None

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

def get_hourly_forecast():
    try:
        now = datetime.now()
        end = now + timedelta(hours=6)
        url = "https://api.open-meteo.com/v1/forecast"
        params = {
            "latitude": LAT,
            "longitude": LON,
            "hourly": "temperature_2m,relativehumidity_2m,weathercode",
            "timezone": "Asia/Ho_Chi_Minh",
            "start": now.strftime("%Y-%m-%dT%H:%M"),
            "end": end.strftime("%Y-%m-%dT%H:%M")
        }
        r = requests.get(url, params=params, timeout=10)
        r.raise_for_status()
        data = r.json()
        hourly = data.get("hourly", {})
        times = hourly.get("time", [])
        temps = hourly.get("temperature_2m", [])
        hums = hourly.get("relativehumidity_2m", [])
        codes = hourly.get("weathercode", [])

        forecast = []
        for i in range(len(times)):
            ts = int(datetime.fromisoformat(times[i]).timestamp() * 1000)
            forecast.append({
                "ts": ts,
                "temperature": temps[i],
                "humidity": hums[i],
                "weather_desc": WEATHER_CODE_MAP.get(codes[i], "?")
            })
        return forecast[:7]  # present + 6 next hours
    except Exception as e:
        logger.warning(f"Weather API error: {e}")
        now_ts = int(datetime.now().timestamp() * 1000)
        return [{"ts": now_ts + 3600*1000*i, "temperature":0, "humidity":0, "weather_desc":"?"} for i in range(7)]

# ================== AI HELPER ==================
def get_advice(temp, hum):
    nutrition = ["Ưu tiên Kali (K)", "Cân bằng NPK", "Bón phân hữu cơ"]
    care = []
    if temp >= 35: care.append("Tránh nắng gắt, tưới sáng sớm/chiều mát")
    elif temp >= 30: care.append("Tưới đủ nước, theo dõi thường xuyên")
    elif temp <= 15: care.append("Giữ ấm, tránh sương muối")
    else: care.append("Nhiệt độ bình thường")
    if hum <= 40: care.append("Độ ẩm thấp: tăng tưới")
    elif hum <= 60: care.append("Độ ẩm hơi thấp: theo dõi, tưới khi cần")
    elif hum >= 85: care.append("Độ ẩm cao: tránh úng, kiểm tra thoát nước")
    else: care.append("Độ ẩm ổn định cho rau muống")
    return " | ".join(nutrition + care + ["Quan sát cây trồng và điều chỉnh thực tế"])

# ================== THINGSBOARD ==================
def send_hourly_to_tb(forecast):
    payload = []
    now = int(time.time() * 1000)
    for item in forecast:
        payload.append({"ts": item["ts"], "temperature": item["temperature"], "humidity": item["humidity"],
                        "weather_desc": item["weather_desc"], "advice": get_advice(item["temperature"], item["humidity"])})
    try:
        r = requests.post(TB_DEVICE_PUSH_URL, json=payload, timeout=10)
        logger.info(f"TB push {len(payload)} points, status: {r.status_code}")
    except Exception as e:
        logger.error(f"ThingsBoard push error: {e}")
    return payload

# ================== ROUTES ==================
@app.get("/")
def root():
    return {"status":"running","device":TB_DEMO_TOKEN[:4]+"***"}

@app.get("/last")
def last_payload_endpoint():
    global _last_payload
    return _last_payload or {"message":"Chưa có dữ liệu"}

# ================== BACKGROUND LOOP ==================
def background_loop():
    global _last_payload
    while True:
        try:
            forecast = get_hourly_forecast()
            _last_payload = send_hourly_to_tb(forecast)
        except Exception as e:
            logger.error(f"Background loop error: {e}")
        time.sleep(LOOP_INTERVAL)

@app.on_event("startup")
def start_loop():
    threading.Thread(target=background_loop, daemon=True).start()

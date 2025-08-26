import os
import random
import logging
import requests
from datetime import datetime
from fastapi import FastAPI
from apscheduler.schedulers.background import BackgroundScheduler

# ================== CONFIG ==================
logger = logging.getLogger("uvicorn")
logger.setLevel(logging.INFO)

TB_TOKEN = os.getenv("TB_TOKEN", "demo_tb_token")
WEATHER_KEY = os.getenv("WEATHER_API_KEY")  # Đọc đúng tên biến từ Render
LOCATION = os.getenv("LOCATION", "Ho Chi Minh,VN")

PUSH_INTERVAL = 300  # 5 phút
CROP_NAME = "Rau muống"

app = FastAPI()
last_payload = {}

# ================== WEATHER FETCH ==================
def fetch_weather():
    if WEATHER_KEY:
        try:
            resp = requests.get(
                "https://api.openweathermap.org/data/2.5/weather",
                params={"q": LOCATION, "appid": WEATHER_KEY, "units": "metric"}
            )
            resp.raise_for_status()
            data = resp.json()
            telemetry = {
                "time": datetime.utcnow().isoformat(),
                "location": LOCATION,
                "temperature": data["main"]["temp"],
                "humidity": data["main"]["humidity"],
                "weather_desc": data["weather"][0]["description"],
                "crop": CROP_NAME,
                "advice_text": random.choice([
                    "Tưới nước đều đặn cho rau muống.",
                    "Bón phân hữu cơ để rau phát triển tốt.",
                    "Theo dõi sâu bệnh, kịp thời xử lý.",
                    "Chọn thời điểm thu hoạch vào buổi sáng để rau tươi ngon."
                ])
            }
            return telemetry
        except Exception as e:
            logger.error(f"Lỗi fetch weather: {e}")
    # Fallback nếu WEATHER_KEY thiếu hoặc lỗi
    telemetry = {
        "time": datetime.utcnow().isoformat(),
        "location": LOCATION,
        "temperature": round(random.uniform(24, 32), 1),
        "humidity": random.randint(60, 95),
        "weather_desc": "Trời quang (test)",
        "crop": CROP_NAME,
        "advice_text": random.choice([
            "Tưới nước đều đặn cho rau muống.",
            "Bón phân hữu cơ để rau phát triển tốt.",
            "Theo dõi sâu bệnh, kịp thời xử lý.",
            "Chọn thời điểm thu hoạch vào buổi sáng để rau tươi ngon."
        ])
    }
    logger.warning("⚠️ WEATHER_API_KEY not found → dùng dữ liệu giả định")
    return telemetry

# ================== THINGSBOARD PUSH ==================
def push_to_thingsboard():
    global last_payload
    payload = fetch_weather()
    last_payload = payload
    if not TB_TOKEN:
        logger.warning("⚠️ TB_TOKEN chưa cấu hình → chỉ log payload")
        logger.info(payload)
        return
    try:
        url = f"https://thingsboard.cloud/api/v1/{TB_TOKEN}/telemetry"
        resp = requests.post(url, json=payload, timeout=10)
        resp.raise_for_status()
        logger.info(f"✅ Sent to ThingsBoard: {payload}")
    except Exception as e:
        logger.error(f"Lỗi push ThingsBoard: {e}")

# ================== BACKGROUND SCHEDULER ==================
scheduler = BackgroundScheduler()
scheduler.add_job(push_to_thingsboard, 'interval', seconds=PUSH_INTERVAL)
scheduler.start()

# ================== API ENDPOINT ==================
@app.get("/last-push")
def last_push():
    return last_payload or fetch_weather()

# ================== STARTUP LOG ==================
@app.on_event("startup")
def startup_event():
    logger.info("🚀 Service started, first push in 5s")
    push_to_thingsboard()

import os
import time
import json
import logging
import requests
import asyncio
from fastapi import FastAPI
from pydantic import BaseModel
from datetime import datetime, timedelta

# ================== CONFIG ==================
TB_DEMO_TOKEN = os.getenv("TB_DEMO_TOKEN", "sgkxcrqntuki8gu1oj8u")
TB_DEVICE_URL = f"https://thingsboard.cloud/api/v1/{TB_DEMO_TOKEN}/telemetry"

LAT = float(os.getenv("LAT", "10.79"))
LON = float(os.getenv("LON", "106.70"))
AUTO_LOOP_INTERVAL = int(os.getenv("AUTO_LOOP_INTERVAL", 300))  # giây

# ================== LOGGING ==================
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# ================== FASTAPI ==================
app = FastAPI()

class SensorData(BaseModel):
    temperature: float
    humidity: float
    battery: float | None = None

# ================== AI HELPER ==================
def get_advice(temp, humi):
    nutrition = ["Ưu tiên Kali (K)","Cân bằng NPK","Bón phân hữu cơ"]
    care = []
    if temp >=35: care.append("Tránh nắng gắt, tưới sáng sớm/chiều mát")
    elif temp >=30: care.append("Tưới đủ nước, theo dõi thường xuyên")
    elif temp <=15: care.append("Giữ ấm, tránh sương muối")
    else: care.append("Nhiệt độ bình thường")
    if humi <=40: care.append("Độ ẩm thấp: tăng tưới")
    elif humi <=60: care.append("Độ ẩm hơi thấp: theo dõi, tưới khi cần")
    elif humi >=85: care.append("Độ ẩm cao: tránh úng, kiểm tra thoát nước")
    else: care.append("Độ ẩm ổn định cho rau muống")
    return {
        "advice": " | ".join(nutrition + care + ["Quan sát cây trồng và điều chỉnh thực tế"]),
        "advice_nutrition": " | ".join(nutrition),
        "advice_care": " | ".join(care),
        "advice_note": "Quan sát cây trồng và điều chỉnh thực tế",
        "prediction": f"Nhiệt độ {temp}°C, độ ẩm {humi}%"
    }

# ================== WEATHER ==================
def get_weather_forecast():
    now = datetime.now()
    result = {}
    # Giờ hiện tại
    result["current_hour"] = now.strftime("%H:%M")
    # 6 giờ tiếp theo
    for i in range(1, 7):
        next_hour = now + timedelta(hours=i)
        result[f"{i}_gio_tiep_theo"] = next_hour.strftime("%H:%M")
    return result

# ================== THINGSBOARD ==================
def send_to_thingsboard(data: dict):
    try:
        logger.info(f"TB ▶ {json.dumps(data, ensure_ascii=False)}")
        r = requests.post(TB_DEVICE_URL, json=data, timeout=10)
        logger.info(f"TB ◀ {r.status_code}")
    except Exception as e:
        logger.error(f"ThingsBoard push error: {e}")

# ================== ROUTES ==================
@app.get("/")
def root():
    return {"status":"running","demo_token":TB_DEMO_TOKEN[:4]+"***"}

@app.post("/esp32-data")
def receive_data(data: SensorData):
    logger.info(f"ESP32 ▶ {data.dict()}")
    advice_data = get_advice(data.temperature,data.humidity)
    weather_data = get_weather_forecast()
    merged = {
        **data.dict(),
        **advice_data,
        **weather_data,
        "location":"An Phú, Hồ Chí Minh",
        "crop":"Rau muống"
    }
    send_to_thingsboard(merged)
    return {"received": data.dict(), "pushed": merged}

# ================== AUTO LOOP ==================
async def auto_loop():
    while True:
        try:
            sample = {"temperature":30.1,"humidity":69.2}
            advice_data = get_advice(sample["temperature"],sample["humidity"])
            weather_data = get_weather_forecast()
            merged = {
                **sample,
                **advice_data,
                **weather_data,
                "location":"An Phú, Hồ Chí Minh",
                "crop":"Rau muống"
            }
            send_to_thingsboard(merged)
        except Exception as e:
            logger.error(f"AUTO loop error: {e}")
        await asyncio.sleep(AUTO_LOOP_INTERVAL)

@app.on_event("startup")
async def start_auto_loop():
    asyncio.create_task(auto_loop())

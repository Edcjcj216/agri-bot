import os
import time
import json
import logging
import requests
from fastapi import FastAPI
from pydantic import BaseModel
import threading
from datetime import datetime, timedelta

# ================== CONFIG ==================
TB_DEMO_TOKEN = "sgkxcrqntuki8gu1oj8u"
TB_DEVICE_URL = f"https://thingsboard.cloud/api/v1/{TB_DEMO_TOKEN}/telemetry"

AI_API_URL = os.getenv("AI_API_URL", "https://api-inference.huggingface.co/models/gpt2")
HF_TOKEN = os.getenv("HF_TOKEN", "")

LAT = float(os.getenv("LAT", 10.79))
LON = float(os.getenv("LON", 106.70))
CROP_NAME = "Rau muống"
LOCATION_NAME = "An Phú, Hồ Chí Minh"

# ================== LOGGING ==================
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# ================== FASTAPI ==================
app = FastAPI()

class SensorData(BaseModel):
    temperature: float
    humidity: float
    battery: float | None = None

# ================== WEATHER ==================
WEATHER_CODE_MAP = {
    0: "Trời quang",
    1: "Trời quang nhẹ",
    2: "Có mây",
    3: "Nhiều mây",
    45: "Sương mù",
    48: "Sương mù đóng băng",
    51: "Mưa phùn nhẹ",
    53: "Mưa phùn vừa",
    55: "Mưa phùn dày",
    61: "Mưa nhẹ",
    63: "Mưa vừa",
    65: "Mưa to",
    71: "Tuyết nhẹ",
    73: "Tuyết vừa",
    75: "Tuyết dày",
    80: "Mưa rào nhẹ",
    81: "Mưa rào vừa",
    82: "Mưa rào mạnh",
    95: "Giông nhẹ hoặc vừa",
    96: "Giông kèm mưa đá nhẹ",
    99: "Giông kèm mưa đá mạnh"
}

def get_weather_forecast() -> dict:
    """Lấy weather và humidity trung bình hôm qua / hôm nay / ngày mai"""
    try:
        start_date = (datetime.utcnow() - timedelta(days=1)).strftime("%Y-%m-%d")
        end_date = (datetime.utcnow() + timedelta(days=1)).strftime("%Y-%m-%d")
        url = "https://api.open-meteo.com/v1/forecast"
        params = {
            "latitude": LAT,
            "longitude": LON,
            "daily": "weathercode,temperature_2m_max,temperature_2m_min",
            "hourly": "humidity_2m",
            "start_date": start_date,
            "end_date": end_date,
            "timezone": "Asia/Ho_Chi_Minh"
        }
        r = requests.get(url, params=params, timeout=10)
        r.raise_for_status()
        data = r.json()

        # Hàng ngày
        daily = data.get("daily", {})
        hourly = data.get("hourly", {})

        def avg_humidity(date_obj):
            date_str = date_obj.strftime("%Y-%m-%d")
            times = hourly.get("time", [])
            hums = hourly.get("humidity_2m", [])
            values = [h for t, h in zip(times, hums) if t.startswith(date_str)]
            return round(sum(values)/len(values), 1) if values else 0

        today_dt = datetime.utcnow()
        yesterday_dt = today_dt - timedelta(days=1)
        tomorrow_dt = today_dt + timedelta(days=1)

        weather_yesterday = {
            "weather_yesterday_desc": WEATHER_CODE_MAP.get(daily["weathercode"][0], "?") if daily else "?",
            "weather_yesterday_max": daily["temperature_2m_max"][0] if daily else 0,
            "weather_yesterday_min": daily["temperature_2m_min"][0] if daily else 0,
            "humidity_yesterday": avg_humidity(yesterday_dt)
        }
        weather_today = {
            "weather_today_desc": WEATHER_CODE_MAP.get(daily["weathercode"][1], "?") if daily else "?",
            "weather_today_max": daily["temperature_2m_max"][1] if daily else 0,
            "weather_today_min": daily["temperature_2m_min"][1] if daily else 0,
            "humidity_today": avg_humidity(today_dt)
        }
        weather_tomorrow = {
            "weather_tomorrow_desc": WEATHER_CODE_MAP.get(daily["weathercode"][2], "?") if daily and len(daily["weathercode"])>2 else "?",
            "weather_tomorrow_max": daily["temperature_2m_max"][2] if daily and len(daily["temperature_2m_max"])>2 else 0,
            "weather_tomorrow_min": daily["temperature_2m_min"][2] if daily and len(daily["temperature_2m_min"])>2 else 0,
            "humidity_tomorrow": avg_humidity(tomorrow_dt)
        }

        return {**weather_yesterday, **weather_today, **weather_tomorrow}

    except Exception as e:
        logger.warning(f"Weather API error: {e}")
        # Fallback
        return {
            "weather_yesterday_desc":"?", "weather_yesterday_max":0, "weather_yesterday_min":0, "humidity_yesterday":0,
            "weather_today_desc":"?", "weather_today_max":0, "weather_today_min":0, "humidity_today":0,
            "weather_tomorrow_desc":"?", "weather_tomorrow_max":0, "weather_tomorrow_min":0, "humidity_tomorrow":0
        }

# ================== AI HELPER ==================
def call_ai_api(data: dict) -> dict:
    model_url = AI_API_URL
    hf_token = HF_TOKEN
    headers = {"Authorization": f"Bearer {hf_token}"} if hf_token else {}

    weather_info = get_weather_forecast()
    weather_text = f"Hôm nay: {weather_info['weather_today_desc']}, {weather_info['weather_today_min']}–{weather_info['weather_today_max']}°C. Ngày mai: {weather_info['weather_tomorrow_desc']}, {weather_info['weather_tomorrow_min']}–{weather_info['weather_tomorrow_max']}°C."

    prompt = (
        f"Dữ liệu cảm biến: Nhiệt độ {data['temperature']}°C, độ ẩm {data['humidity']}%. "
        f"Cây: {CROP_NAME} tại {LOCATION_NAME}. {weather_text} "
        "Viết 1 câu dự báo và 1 câu gợi ý chăm sóc ngắn gọn."
    )
    body = {"inputs": prompt, "options": {"wait_for_model": True}}

    def local_sections(temp: float, humi: float) -> dict:
        nutrition = ["Ưu tiên Kali (K)", "Cân bằng NPK", "Bón phân hữu cơ"]
        care = []
        if temp >= 35:
            care.append("Tránh nắng gắt, tưới sáng sớm/chiều mát")
        elif temp >= 30:
            care.append("Tưới đủ nước, theo dõi thường xuyên")
        elif temp <= 15:
            care.append("Giữ ấm, tránh sương muối")
        else:
            care.append("Nhiệt độ bình thường")
        if humi <= 40:
            care.append("Độ ẩm thấp: tăng tưới")
        elif humi <= 60:
            care.append("Độ ẩm hơi thấp: theo dõi, tưới khi cần")
        elif humi >= 85:
            care.append("Độ ẩm cao: tránh úng, kiểm tra thoát nước")
        else:
            care.append("Độ ẩm ổn định cho rau muống")
        return {
            "prediction": f"Nhiệt độ {temp}°C, độ ẩm {humi}%",
            "advice_nutrition": " | ".join(nutrition),
            "advice_care": " | ".join(care),
            "advice_note": "Quan sát cây trồng và điều chỉnh thực tế",
            "advice": " | ".join(nutrition + care + ["Quan sát cây trồng và điều chỉnh thực tế"])
        }

    try:
        logger.info(f"AI ▶ {prompt[:150]}...")
        r = requests.post(model_url, headers=headers, json=body, timeout=30)
        if r.status_code == 200:
            out = r.json()
            text = ""
            if isinstance(out, list) and out:
                first = out[0]
                if isinstance(first, dict):
                    text = first.get("generated_text") or first.get("text") or str(first)
                else:
                    text = str(first)
            else:
                text = str(out)
            sections = local_sections(data['temperature'], data['humidity'])
            sections['advice'] = text.strip()
            return sections
    except Exception as e:
        logger.warning(f"AI API failed: {e}, fallback local")

    return local_sections(data['temperature'], data['humidity'])

# ================== THINGSBOARD ==================
def send_to_thingsboard(data: dict):
    try:
        logger.info(f"TB ▶ {data}")
        r = requests.post(TB_DEVICE_URL, json=data, timeout=10)
        logger.info(f"TB ◀ {r.status_code}")
    except Exception as e:
        logger.error(f"ThingsBoard push error: {e}")

# ================== ROUTES ==================
@app.get("/")
def root():
    return {"status": "running", "demo_token": TB_DEMO_TOKEN[:4] + "***"}

@app.post("/esp32-data")
def receive_data(data: SensorData):
    logger.info(f"ESP32 ▶ {data.dict()}")
    ai_result = call_ai_api(data.dict())
    weather_info = get_weather_forecast()
    merged = data.dict() | {"location": LOCATION_NAME, "crop": CROP_NAME} | ai_result | weather_info
    send_to_thingsboard(merged)
    return {"received": data.dict(), "pushed": merged}

# ================== AUTO LOOP ==================
def auto_loop():
    while True:
        try:
            sample = {"temperature": 30.1, "humidity": 69.2}
            ai_result = call_ai_api(sample)
            weather_info = get_weather_forecast()
            merged = sample | {"location": LOCATION_NAME, "crop": CROP_NAME} | ai_result | weather_info
            send_to_thingsboard(merged)
            logger.info(f"[AUTO LOOP] Pushed telemetry.")
        except Exception as e:
            logger.error(f"AUTO loop error: {e}")
        time.sleep(300)

threading.Thread(target=auto_loop, daemon=True).start()

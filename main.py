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

# ================== WEATHER ==================
WEATHER_CODE_MAP = {
    0: "Trời quang", 1: "Trời quang nhẹ", 2: "Có mây", 3: "Nhiều mây",
    45: "Sương mù", 48: "Sương mù đóng băng", 51: "Mưa phùn nhẹ", 53: "Mưa phùn vừa",
    55: "Mưa phùn dày", 61: "Mưa nhẹ", 63: "Mưa vừa", 65: "Mưa to",
    71: "Tuyết nhẹ", 73: "Tuyết vừa", 75: "Tuyết dày", 80: "Mưa rào nhẹ",
    81: "Mưa rào vừa", 82: "Mưa rào mạnh", 95: "Giông nhẹ hoặc vừa",
    96: "Giông kèm mưa đá nhẹ", 99: "Giông kèm mưa đá mạnh"
}

weather_cache = {"ts": 0, "data": {}}

def get_weather_forecast():
    now = datetime.now()
    if time.time() - weather_cache["ts"] < 900:  # cache 15 phút
        return weather_cache["data"]
    try:
        start_date = (now - timedelta(days=1)).strftime("%Y-%m-%d")
        end_date = (now + timedelta(days=1)).strftime("%Y-%m-%d")
        url = "https://api.open-meteo.com/v1/forecast"
        params = {
            "latitude": LAT,
            "longitude": LON,
            "daily": "weathercode,temperature_2m_max,temperature_2m_min",
            "hourly": "temperature_2m,relativehumidity_2m,weathercode",
            "timezone": "Asia/Ho_Chi_Minh",
            "start_date": start_date,
            "end_date": end_date
        }
        r = requests.get(url, params=params, timeout=10)
        r.raise_for_status()
        data = r.json()
        daily = data.get("daily", {})
        hourly = data.get("hourly", {})

        def mean(lst):
            return round(sum(lst)/len(lst),1) if lst else 0

        # Hôm qua
        weather_yesterday = {
            "weather_yesterday_desc": WEATHER_CODE_MAP.get(daily.get("weathercode",[0])[0], "?"),
            "weather_yesterday_max": daily.get("temperature_2m_max",[0])[0],
            "weather_yesterday_min": daily.get("temperature_2m_min",[0])[0],
            "humidity_yesterday": mean(hourly.get("relativehumidity_2m", [])[:24])
        }
        # Hôm nay
        weather_today = {
            "weather_today_desc": WEATHER_CODE_MAP.get(daily.get("weathercode",[0])[1], "?"),
            "weather_today_max": daily.get("temperature_2m_max",[0])[1],
            "weather_today_min": daily.get("temperature_2m_min",[0])[1],
            "humidity_today": mean(hourly.get("relativehumidity_2m", [])[24:48])
        }
        # Ngày mai
        weather_tomorrow = {
            "weather_tomorrow_desc": WEATHER_CODE_MAP.get(daily.get("weathercode",[0])[2], "?"),
            "weather_tomorrow_max": daily.get("temperature_2m_max",[0])[2],
            "weather_tomorrow_min": daily.get("temperature_2m_min",[0])[2],
            "humidity_tomorrow": mean(hourly.get("relativehumidity_2m", [])[48:72])
        }

        # 6 giờ tiếp theo theo giờ thực
        now_hour = now.hour
        times = hourly.get("time", [])
        temps = hourly.get("temperature_2m", [])
        hums = hourly.get("relativehumidity_2m", [])
        codes = hourly.get("weathercode", [])
        next_6h = []
        for i in range(1,7):
            idx = now_hour + i
            if idx < len(temps):
                next_6h.append(f"{i} giờ tiếp theo: {times[idx]} — {round(temps[idx],1)}°C — {WEATHER_CODE_MAP.get(codes[idx],'?')}")
            else:
                next_6h.append(f"{i} giờ tiếp theo: - — 0°C — ?")

        result = {
            **weather_yesterday, **weather_today, **weather_tomorrow
        }
        # Thêm 6 giờ tiếp theo
        for i, val in enumerate(next_6h,1):
            result[f"{i}_gio_tiep_theo"] = val

        weather_cache["data"] = result
        weather_cache["ts"] = time.time()
        return result
    except Exception as e:
        logger.warning(f"Weather API error: {e}")
        fallback = {f"{i}_gio_tiep_theo": f"{i} giờ tiếp theo: - — 0°C — ?" for i in range(1,7)}
        return fallback

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

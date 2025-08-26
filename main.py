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
TB_TOKEN = os.getenv("TB_TOKEN")
if not TB_TOKEN:
    raise ValueError("TB_TOKEN not set in environment!")
TB_DEVICE_URL = f"https://thingsboard.cloud/api/v1/{TB_TOKEN}/telemetry"

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
    0: "Trời quang",
    1: "Trời quang nhẹ",
    2: "Có mây",
    3: "Nhiều mây",
    45: "Sương mù",
    51: "Mưa phùn nhẹ / Lất phất",
    53: "Mưa phùn vừa",
    55: "Mưa phùn dày",
    61: "Mưa nhẹ",
    63: "Mưa vừa",
    65: "Mưa to",
    80: "Mưa rào nhẹ",
    81: "Mưa rào vừa",
    82: "Mưa rào mạnh",
    95: "Giông nhẹ hoặc vừa"
}

weather_cache = {"ts": 0, "data": {}}

def weather_alert(temp, weather_desc):
    """Cảnh báo nông nghiệp cơ bản dựa trên nhiệt độ và mô tả thời tiết"""
    alerts = []
    if temp >= 35: alerts.append("Nắng gắt — cần che phủ/tưới nhiều")
    elif temp <= 15: alerts.append("Trời lạnh — theo dõi cây trồng")
    if "Mưa" in weather_desc: alerts.append("Mưa — kiểm tra thoát nước")
    if "Sương" in weather_desc: alerts.append("Sương mù — theo dõi độ ẩm")
    if "Giông" in weather_desc: alerts.append("Giông — bảo vệ cây và vật nuôi")
    return " | ".join(alerts) if alerts else "Bình thường"

def get_weather_forecast():
    now = datetime.now()
    if time.time() - weather_cache["ts"] < 900:
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

        def mean(lst): return round(sum(lst)/len(lst),1) if lst else 0

        weather_yesterday = {
            "weather_yesterday_desc": WEATHER_CODE_MAP.get(daily.get("weathercode",[0])[0], "Không rõ"),
            "weather_yesterday_max": daily.get("temperature_2m_max",[0])[0],
            "weather_yesterday_min": daily.get("temperature_2m_min",[0])[0],
            "humidity_yesterday": mean(hourly.get("relativehumidity_2m", [])[:24]),
        }
        weather_yesterday["alert"] = weather_alert(weather_yesterday["weather_yesterday_max"], weather_yesterday["weather_yesterday_desc"])

        weather_today = {
            "weather_today_desc": WEATHER_CODE_MAP.get(daily.get("weathercode",[0])[1], "Không rõ"),
            "weather_today_max": daily.get("temperature_2m_max",[0])[1],
            "weather_today_min": daily.get("temperature_2m_min",[0])[1],
            "humidity_today": mean(hourly.get("relativehumidity_2m", [])[24:48]),
        }
        weather_today["alert"] = weather_alert(weather_today["weather_today_max"], weather_today["weather_today_desc"])

        weather_tomorrow = {
            "weather_tomorrow_desc": WEATHER_CODE_MAP.get(daily.get("weathercode",[0])[2], "Không rõ"),
            "weather_tomorrow_max": daily.get("temperature_2m_max",[0])[2],
            "weather_tomorrow_min": daily.get("temperature_2m_min",[0])[2],
            "humidity_tomorrow": mean(hourly.get("relativehumidity_2m", [])[48:72]),
        }
        weather_tomorrow["alert"] = weather_alert(weather_tomorrow["weather_tomorrow_max"], weather_tomorrow["weather_tomorrow_desc"])

        temps = hourly.get("temperature_2m", [])
        hums = hourly.get("relativehumidity_2m", [])
        codes = hourly.get("weathercode", [])
        hours_data = {}
        for i in range(4):
            hours_data[f"hour_{i}_temperature"] = round(temps[i],1) if i < len(temps) else 0
            hours_data[f"hour_{i}_humidity"] = round(hums[i],1) if i < len(hums) else 0
            desc = WEATHER_CODE_MAP.get(codes[i], "Không rõ") if i < len(codes) else "Không rõ"
            hours_data[f"hour_{i}_weather_desc"] = desc
            hours_data[f"hour_{i}_alert"] = weather_alert(hours_data[f"hour_{i}_temperature"], desc)

        result = {**weather_yesterday, **weather_today, **weather_tomorrow, **hours_data}
        weather_cache["data"] = result
        weather_cache["ts"] = time.time()
        return result
    except Exception as e:
        logger.warning(f"Weather API error: {e}")
        fallback = {f"hour_{i}_temperature":0, f"hour_{i}_humidity":0,
                    f"hour_{i}_weather_desc":"Không rõ", f"hour_{i}_alert":"Không rõ" for i in range(4)}
        fallback.update({
            "weather_yesterday_desc":"Không rõ", "weather_yesterday_max":0, "weather_yesterday_min":0,
            "humidity_yesterday":0, "weather_yesterday_alert":"Không rõ",
            "weather_today_desc":"Không rõ", "weather_today_max":0, "weather_today_min":0,
            "humidity_today":0, "weather_today_alert":"Không rõ",
            "weather_tomorrow_desc":"Không rõ", "weather_tomorrow_max":0, "weather_tomorrow_min":0,
            "humidity_tomorrow":0, "weather_tomorrow_alert":"Không rõ",
        })
        return fallback

# ================== ADVICE ==================
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
    return {"status":"running","tb_token":TB_TOKEN[:4]+"***"}

@app.post("/esp32-data")
def receive_data(data: SensorData):
    logger.info(f"ESP32 ▶ {data.dict()}")
    advice_data = get_advice(data.temperature, data.humidity)
    merged = {
        **data.dict(),
        **advice_data,
        "location": "An Phú, Hồ Chí Minh",
        "crop": "Rau muống"
    }
    merged.update(get_weather_forecast())
    send_to_thingsboard(merged)
    return {"received": data.dict(), "pushed": merged}

# ================== AUTO LOOP ==================
async def auto_loop():
    while True:
        try:
            sample = {"temperature":30.1,"humidity":69.2}
            advice_data = get_advice(sample["temperature"], sample["humidity"])
            merged = {
                **sample,
                **advice_data,
                "location": "An Phú, Hồ Chí Minh",
                "crop": "Rau muống"
            }
            merged.update(get_weather_forecast())
            send_to_thingsboard(merged)
        except Exception as e:
            logger.error(f"AUTO loop error: {e}")
        await asyncio.sleep(AUTO_LOOP_INTERVAL)

@app.on_event("startup")
async def start_auto_loop():
    asyncio.create_task(auto_loop())

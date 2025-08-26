import os
import time
import logging
import requests
from fastapi import FastAPI
from apscheduler.schedulers.background import BackgroundScheduler

# ================== CONFIG ==================
TB_URL = "https://thingsboard.cloud/api/v1"
TB_TOKEN = os.getenv("TB_DEMO_TOKEN", "your_tb_token_here")
LAT = 10.762622   # Hồ Chí Minh
LON = 106.660172
OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"

# Sai số hiệu chỉnh nhiệt độ dự báo
FORECAST_BIAS = -0.5

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("main")

# ================== WEATHER CODE MAP ==================
WEATHER_CODE_MAP = {
    800: "Có nắng",
    801: "Nắng nhẹ (ít mây)",
    802: "Có mây",
    803: "Nhiều mây",
    804: "U ám",

    500: "Mưa nhẹ",
    501: "Mưa vừa",
    502: "Mưa to",
    503: "Mưa rất to",
    504: "Mưa cực lớn",
    520: "Mưa rào nhẹ",
    521: "Mưa rào",
    522: "Mưa rào to",
    531: "Mưa rào bất thường",

    200: "Có giông",
    201: "Giông vừa",
    202: "Giông mạnh",
    210: "Giông",
    211: "Giông",
    212: "Giông mạnh",
    221: "Giông bất thường",
    230: "Giông kèm mưa phùn",
    231: "Giông kèm mưa nhỏ",
    232: "Giông kèm mưa lớn",

    701: "Sương mù nhẹ",
    741: "Sương mù",
    771: "Gió giật"
}
DEFAULT_DESC = "Không xác định"

# ================== FASTAPI APP ==================
app = FastAPI()
last_push = {}

# ================== FETCH WEATHER ==================
def fetch_weather():
    try:
        params = {
            "latitude": LAT,
            "longitude": LON,
            "hourly": ["temperature_2m", "relative_humidity_2m", "weathercode"],
            "daily": ["temperature_2m_max", "temperature_2m_min"],
            "timezone": "Asia/Ho_Chi_Minh"
        }
        r = requests.get(OPEN_METEO_URL, params=params, timeout=10)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        logger.error(f"[ERROR] Error fetching Open-Meteo: {e}")
        return None

# ================== PUSH TO THINGSBOARD ==================
def push_to_tb():
    global last_push
    data = fetch_weather()
    if not data:
        return

    try:
        hourly = data["hourly"]
        daily = data["daily"]

        telemetry = {}
        # chỉ lấy 5 giờ tới (hour_0 -> hour_4)
        for i in range(0, 5):
            temp = hourly["temperature_2m"][i] + FORECAST_BIAS
            hum = hourly["relative_humidity_2m"][i]
            code = hourly["weathercode"][i]
            desc = WEATHER_CODE_MAP.get(code, DEFAULT_DESC)
            telemetry[f"hour_{i}temperature"] = round(temp, 1)
            telemetry[f"hour_{i}humidity"] = hum
            telemetry[f"hour_{i}weather_desc"] = desc

        # daily max/min
        daily_max = daily["temperature_2m_max"][0] + FORECAST_BIAS
        daily_min = daily["temperature_2m_min"][0] + FORECAST_BIAS
        telemetry["daily_max"] = round(daily_max, 1)
        telemetry["daily_min"] = round(daily_min, 1)

        # override nếu nắng nóng
        if daily_max >= 35:
            telemetry["daily_weather_desc"] = "Nắng nóng"
        else:
            first_code = hourly["weathercode"][0]
            telemetry["daily_weather_desc"] = WEATHER_CODE_MAP.get(first_code, DEFAULT_DESC)

        # timestamp
        telemetry["_ts"] = int(time.time() * 1000)

        # gửi lên ThingsBoard
        url = f"{TB_URL}/{TB_TOKEN}/telemetry"
        r = requests.post(url, json=telemetry, timeout=10)
        r.raise_for_status()
        last_push = telemetry
        logger.info(f"✅ Sent to ThingsBoard: {telemetry}")
    except Exception as e:
        logger.error(f"[ERROR] push_to_tb: {e}")

# ================== SCHEDULER ==================
scheduler = BackgroundScheduler()
scheduler.add_job(push_to_tb, "interval", minutes=5)
scheduler.start()

# ================== FASTAPI ENDPOINTS ==================
@app.get("/")
def root():
    return {"status": "ok", "last_push_keys": list(last_push.keys())}

@app.get("/last-push")
def get_last_push():
    return last_push

# ================== STARTUP EVENT ==================
@app.on_event("startup")
def startup_event():
    logger.info("🚀 Service started, sending first telemetry...")
    push_to_tb()

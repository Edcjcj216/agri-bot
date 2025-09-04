# main.py
# ================================================
# 🌱 Agri-Bot — FastAPI service
# Nhiệm vụ:
#   - Lấy dữ liệu thời tiết từ Open-Meteo API
#   - Chuẩn hoá dữ liệu thành telemetry cho ThingsBoard
#   - Gửi telemetry định kỳ (10 phút 1 lần)
#
# Lưu ý:
#   - Không thêm key ngoài dashboard quy định
#   - Chỉ giữ lại các key cần thiết
#   - Tất cả mô tả thời tiết đều dịch sang tiếng Việt
# ================================================

import os
import asyncio
import logging
import requests
from fastapi import FastAPI
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

# ------------------------------------------------
# Cấu hình logging
# ------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

# ------------------------------------------------
# Load biến môi trường từ Render (hoặc local)
# ------------------------------------------------
THINGSBOARD_URL = os.getenv("THINGSBOARD_URL", "https://thingsboard.cloud")
DEVICE_TOKEN = os.getenv("THINGSBOARD_DEVICE_TOKEN", "")
LATITUDE = float(os.getenv("LATITUDE", "10.762622"))      # Hồ Chí Minh mặc định
LONGITUDE = float(os.getenv("LONGITUDE", "106.660172"))
LOCATION_NAME = os.getenv("LOCATION_NAME", "An Phú, Hồ Chí Minh")
TIMEZONE = ZoneInfo("Asia/Ho_Chi_Minh")

# ------------------------------------------------
# Mapping Open-Meteo weather codes sang tiếng Việt
# ------------------------------------------------
WEATHER_CODE_MAP = {
    0: "Nắng", 1: "Nắng nhẹ", 2: "Ít mây", 3: "Nhiều mây",
    45: "Sương mù", 48: "Sương mù",
    51: "Mưa phùn nhẹ", 53: "Mưa phùn vừa", 55: "Mưa phùn dày",
    56: "Mưa phùn lạnh", 57: "Mưa phùn lạnh dày",
    61: "Mưa nhẹ", 63: "Mưa vừa", 65: "Mưa to",
    66: "Mưa lạnh nhẹ", 67: "Mưa lạnh to",
    80: "Mưa rào nhẹ", 81: "Mưa rào vừa", 82: "Mưa rào mạnh",
    95: "Có giông", 96: "Có giông", 99: "Có giông",
}

# ------------------------------------------------
# Hàm gọi API Open-Meteo
# ------------------------------------------------
def fetch_weather():
    """
    Lấy dữ liệu dự báo từ Open-Meteo:
      - hourly: nhiệt độ, độ ẩm, weathercode
      - daily: min/max nhiệt độ, độ ẩm TB, weathercode
    """
    url = (
        f"https://api.open-meteo.com/v1/forecast?"
        f"latitude={LATITUDE}&longitude={LONGITUDE}"
        f"&hourly=temperature_2m,relative_humidity_2m,weathercode"
        f"&daily=temperature_2m_max,temperature_2m_min,relative_humidity_2m_mean,weathercode"
        f"&timezone=Asia%2FBangkok"
    )

    logging.info("[FETCH] Open-Meteo URL: %s", url)
    r = requests.get(url, timeout=10)
    r.raise_for_status()
    return r.json()

# ------------------------------------------------
# Xử lý dữ liệu thành telemetry ThingsBoard
# ------------------------------------------------
def process_weather(data):
    """
    Trích xuất dữ liệu từ Open-Meteo và chuẩn hoá thành telemetry.
    Dashboard yêu cầu:
      - Top-level:
          + latitude, longitude, location
          + temperature_h (tại giờ kế tiếp)
          + humidity (tại giờ kế tiếp)
      - Dự báo 4 giờ tới:
          + hour_1 ... hour_4
          + hour_X_temperature, hour_X_humidity, hour_X_weather_desc
      - Ngày mai:
          + weather_tomorrow_min
          + weather_tomorrow_max
          + humidity_tomorrow
          + weather_tomorrow_desc
    """
    now = datetime.now(TIMEZONE)

    hourly = data["hourly"]
    daily = data["daily"]

    # Làm tròn giờ hiện tại lên giờ kế tiếp
    current_hour = now.replace(minute=0, second=0, microsecond=0)
    if now.minute > 0:
        current_hour += timedelta(hours=1)

    telemetry = {
        "latitude": LATITUDE,
        "longitude": LONGITUDE,
        "location": LOCATION_NAME,
    }

    # Lấy 4 giờ kế tiếp
    for i in range(4):
        ts = current_hour + timedelta(hours=i)
        ts_str = ts.strftime("%H:00")

        # Open-Meteo trả ISO datetime => check index
        if ts.isoformat() in hourly["time"]:
            idx = hourly["time"].index(ts.isoformat())

            telemetry[f"hour_{i+1}"] = ts_str
            telemetry[f"hour_{i+1}_temperature"] = hourly["temperature_2m"][idx]
            telemetry[f"hour_{i+1}_humidity"] = hourly["relative_humidity_2m"][idx]
            telemetry[f"hour_{i+1}_weather_desc"] = WEATHER_CODE_MAP.get(
                hourly["weathercode"][idx], "Không rõ"
            )

            # Gán giá trị hiển thị top-level (lấy giờ đầu tiên sau hiện tại)
            if i == 0:
                telemetry["temperature_h"] = hourly["temperature_2m"][idx]
                telemetry["humidity"] = hourly["relative_humidity_2m"][idx]

    # Dự báo ngày mai (index 1)
    if len(daily["time"]) >= 2:
        telemetry["weather_tomorrow_min"] = daily["temperature_2m_min"][1]
        telemetry["weather_tomorrow_max"] = daily["temperature_2m_max"][1]
        telemetry["humidity_tomorrow"] = daily["relative_humidity_2m_mean"][1]
        telemetry["weather_tomorrow_desc"] = WEATHER_CODE_MAP.get(
            daily["weathercode"][1], "Không rõ"
        )

    return telemetry

# ------------------------------------------------
# Hàm gửi dữ liệu lên ThingsBoard
# ------------------------------------------------
def push_to_thingsboard(payload):
    """
    Gửi telemetry lên ThingsBoard qua REST API.
    """
    url = f"{THINGSBOARD_URL}/api/v1/{DEVICE_TOKEN}/telemetry"
    logging.info("[TB PUSH] Payload ▶ %s", payload)

    r = requests.post(url, json=payload, timeout=10)
    if r.status_code != 200:
        logging.error("[TB RESP] %s %s", r.status_code, r.text)
    else:
        logging.info("[TB RESP] OK")

# ------------------------------------------------
# Auto-loop: chạy định kỳ 10 phút
# ------------------------------------------------
async def auto_loop():
    while True:
        try:
            data = fetch_weather()
            telemetry = process_weather(data)
            logging.info("[AUTO] Sample ▶ %s", telemetry)
            push_to_thingsboard(telemetry)
        except Exception as e:
            logging.error("[AUTO] Lỗi: %s", e)
        await asyncio.sleep(600)  # 10 phút 1 lần

# ------------------------------------------------
# FastAPI app
# ------------------------------------------------
app = FastAPI()

@app.on_event("startup")
async def startup_event():
    logging.info("✅ Auto-loop simulator started")
    asyncio.create_task(auto_loop())

@app.get("/")
def home():
    return {"status": "running", "location": LOCATION_NAME}

import os
import asyncio
import logging
from datetime import datetime, timedelta
import requests
from fastapi import FastAPI
from zoneinfo import ZoneInfo

# =========================
# Cấu hình logging
# =========================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)

# =========================
# Env vars
# =========================
TB_HOST = os.getenv("TB_HOST")
TB_TOKEN = os.getenv("TB_TOKEN")
TB_DEVICE_URL = os.getenv("TB_DEVICE_URL")
OWM_API_KEY = os.getenv("OWM_API_KEY")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")

# =========================
# Bản đồ mã thời tiết
# =========================
WEATHER_CODE_MAP = {
    0: "Trời nắng đẹp",
    1: "Trời không mây",
    2: "Trời có mây",
    3: "Trời nhiều mây",
    51: "Mưa phùn nhẹ",
    53: "Mưa phùn vừa",
    55: "Mưa phùn dày hạt",
    61: "Mưa nhẹ",
    63: "Mưa vừa",
    65: "Mưa to",
    80: "Mưa rào nhẹ",
    81: "Mưa rào vừa",
    82: "Mưa rào to",
    95: "Có giông nhẹ",
    96: "Có giông vừa",
    99: "Có giông lớn",
}

# =========================
# Hàm fetch từ Open-Meteo
# =========================
async def fetch_openmeteo(lat: float, lon: float):
    try:
        url = (
            f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}"
            "&hourly=temperature_2m,relative_humidity_2m,weathercode"
            "&daily=temperature_2m_max,temperature_2m_min,weathercode"
            "&timezone=auto"
        )
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        logging.warning(f"Open-Meteo lỗi: {e}")
        return None

# =========================
# Hàm fetch từ OWM
# =========================
async def fetch_owm(lat: float, lon: float):
    try:
        if not OWM_API_KEY:
            return None
        url = (
            f"https://api.openweathermap.org/data/2.5/forecast?lat={lat}&lon={lon}&units=metric&appid={OWM_API_KEY}&lang=vi"
        )
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        logging.warning(f"OWM lỗi: {e}")
        return None

# =========================
# Fallback OpenRouter (placeholder)
# =========================
async def fetch_openrouter():
    try:
        if not OPENROUTER_API_KEY:
            return None
        # TODO: implement khi cần thiết
        return None
    except Exception as e:
        logging.warning(f"OpenRouter lỗi: {e}")
        return None

# =========================
# Push telemetry lên TB
# =========================
async def push_to_tb(payload: dict):
    try:
        url = f"{TB_HOST}/api/v1/{TB_TOKEN}/telemetry"
        r = requests.post(url, json=payload, timeout=10)
        r.raise_for_status()
        logging.info(f"Đã push telemetry: {payload}")
    except Exception as e:
        logging.error(f"Push TB lỗi: {e}")

# =========================
# Build payload
# =========================
def build_payload(data: dict, lat: float, lon: float, location: str):
    tz = ZoneInfo("Asia/Ho_Chi_Minh")
    now = datetime.now(tz)
    minute = now.minute
    start_hour = (now.replace(minute=0, second=0, microsecond=0)
                  + timedelta(hours=1 if minute > 0 else 0))

    payload = {
        "latitude": lat,
        "longitude": lon,
        "location": location,
        "timestamp": now.isoformat(),
    }

    try:
        hourly_time = [datetime.fromisoformat(t).astimezone(tz) for t in data["hourly"]["time"]]
        temps = data["hourly"]["temperature_2m"]
        hums = data["hourly"]["relative_humidity_2m"]
        codes = data["hourly"]["weathercode"]

        for i in range(4):
            target = start_hour + timedelta(hours=i)
            if target in hourly_time:
                idx = hourly_time.index(target)
                payload[f"hour_{i+1}_temperature"] = temps[idx]
                payload[f"hour_{i+1}_humidity"] = hums[idx]
                payload[f"hour_{i+1}_weather_desc"] = WEATHER_CODE_MAP.get(codes[idx], "Không rõ")

        # Giá trị hiện tại
        payload["temperature_h"] = temps[0]
        payload["humidity"] = hums[0]

        # Ngày mai
        payload["weather_tomorrow_min"] = data["daily"]["temperature_2m_min"][1]
        payload["weather_tomorrow_max"] = data["daily"]["temperature_2m_max"][1]
        code_tomorrow = data["daily"]["weathercode"][1]
        payload["weather_tomorrow_desc"] = WEATHER_CODE_MAP.get(code_tomorrow, "Không rõ")

    except Exception as e:
        logging.error(f"Build payload lỗi: {e}")

    return payload

# =========================
# Auto loop fetch & push
# =========================
async def auto_loop(lat: float, lon: float, location: str, interval: int = 600):
    while True:
        data = await fetch_openmeteo(lat, lon)
        if not data:
            data = await fetch_owm(lat, lon)
        if not data:
            data = await fetch_openrouter()

        if data:
            payload = build_payload(data, lat, lon, location)
            await push_to_tb(payload)
        else:
            logging.error("Không fetch được dữ liệu từ cả 3 nguồn")

        await asyncio.sleep(interval)

# =========================
# FastAPI app
# =========================
app = FastAPI()

@app.get("/weather")
async def get_weather():
    lat, lon, location = 10.762622, 106.660172, "An Phú, Hồ Chí Minh"
    data = await fetch_openmeteo(lat, lon)
    if not data:
        data = await fetch_owm(lat, lon)
    if not data:
        data = await fetch_openrouter()
    if not data:
        return {"error": "Không fetch được dữ liệu"}

    payload = build_payload(data, lat, lon, location)
    return payload

# =========================
# Chạy loop nền
# =========================
if __name__ == "__main__":
    lat, lon, location = 10.762622, 106.660172, "An Phú, Hồ Chí Minh"
    asyncio.run(auto_loop(lat, lon, location, interval=600))
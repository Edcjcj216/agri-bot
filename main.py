# ============================================================
# main.py — Agri-bot với fallback Open-Meteo → OWM → OpenRouter
# ============================================================

import os
import time
import json
import logging
import requests
import asyncio
from datetime import datetime, timedelta
from fastapi import FastAPI

# --------------------------------------
# Logging
# --------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger("agri-bot")

# --------------------------------------
# ENV
# --------------------------------------
LAT = 10.762622
LON = 106.660172
REQUEST_TIMEOUT = 10
EXTENDED_HOURS = 4

TB_HOST = os.getenv("TB_HOST", "https://thingsboard.cloud")
TB_TOKEN = os.getenv("TB_TOKEN")
TB_DEVICE_URL = f"{TB_HOST}/api/v1/{TB_TOKEN}/telemetry" if TB_TOKEN else None

OWM_API_KEY = os.getenv("OWM_API_KEY")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")

# --------------------------------------
# Weather desc mapping
# --------------------------------------
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

def weather_desc_from_code(code: int):
    return WEATHER_CODE_MAP.get(code, "Không xác định")

def translate_desc(desc):
    if not desc:
        return None
    desc = desc.lower().strip()
    mapping = {
        "clear sky": "Trời quang đãng",
        "few clouds": "Ít mây",
        "scattered clouds": "Mây rải rác",
        "broken clouds": "Mây nhiều",
        "overcast clouds": "Trời u ám",
        "light rain": "Mưa nhẹ",
        "moderate rain": "Mưa vừa",
        "heavy intensity rain": "Mưa to",
        "thunderstorm": "Có giông",
        "snow": "Tuyết rơi",
        "mist": "Sương mù",
    }
    return mapping.get(desc, desc)

# --------------------------------------
# Fetchers
# --------------------------------------
def fetch_open_meteo(lat=LAT, lon=LON):
    url = (
        f"https://api.open-meteo.com/v1/forecast"
        f"?latitude={lat}&longitude={lon}"
        f"&hourly=temperature_2m,relative_humidity_2m,weathercode"
        f"&daily=temperature_2m_max,temperature_2m_min,weathercode,relative_humidity_2m_max"
        f"&timezone=auto"
    )
    try:
        r = requests.get(url, timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        logger.warning(f"Open-Meteo fetch failed: {e}")
        return None

def fetch_owm_weather(lat=LAT, lon=LON):
    if not OWM_API_KEY:
        return None
    url = "https://api.openweathermap.org/data/2.5/onecall"
    params = {
        "lat": lat, "lon": lon,
        "appid": OWM_API_KEY,
        "units": "metric", "lang": "vi",
        "exclude": "minutely,alerts"
    }
    try:
        r = requests.get(url, params=params, timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        logger.warning(f"OWM fetch failed: {e}")
        return None

def fetch_openrouter_weather(lat=LAT, lon=LON):
    if not OPENROUTER_API_KEY:
        return None
    url = "https://api.openrouter.ai/v1/weather/forecast"
    headers = {"Authorization": f"Bearer {OPENROUTER_API_KEY}"}
    params = {"latitude": lat, "longitude": lon, "units": "metric", "lang": "vi"}
    try:
        r = requests.get(url, headers=headers, params=params, timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        logger.warning(f"OpenRouter fetch failed: {e}")
        return None

# --------------------------------------
# Merge & payload builder
# --------------------------------------
def merge_weather_and_hours():
    telemetry = {}
    hourly_list = []

    # --- Try Open-Meteo ---
    data = fetch_open_meteo()
    if data:
        times = data.get("hourly", {}).get("time", [])
        temps = data.get("hourly", {}).get("temperature_2m", [])
        hums = data.get("hourly", {}).get("relative_humidity_2m", [])
        codes = data.get("hourly", {}).get("weathercode", [])

        for i in range(len(times)):
            hourly_list.append({
                "time": times[i],
                "temperature": temps[i],
                "humidity": hums[i],
                "weather_desc": weather_desc_from_code(codes[i]) if i < len(codes) else None,
            })

        # daily
        dmax = data.get("daily", {}).get("temperature_2m_max", [None])[0]
        dmin = data.get("daily", {}).get("temperature_2m_min", [None])[0]
        dcode = data.get("daily", {}).get("weathercode", [None])[0]
        telemetry["weather_tomorrow_max"] = dmax
        telemetry["weather_tomorrow_min"] = dmin
        telemetry["weather_tomorrow_desc"] = weather_desc_from_code(dcode)

    # --- Fallback OWM ---
    if len(hourly_list) < EXTENDED_HOURS:
        owm = fetch_owm_weather()
        if owm and "hourly" in owm:
            for h in owm["hourly"]:
                if len(hourly_list) >= EXTENDED_HOURS: break
                hourly_list.append({
                    "time": datetime.fromtimestamp(h["dt"]).isoformat(),
                    "temperature": h.get("temp"),
                    "humidity": h.get("humidity"),
                    "weather_desc": translate_desc(h["weather"][0]["description"]) if h.get("weather") else None
                })

    # --- Fallback OpenRouter ---
    if len(hourly_list) < EXTENDED_HOURS:
        orw = fetch_openrouter_weather()
        if orw and "hourly" in orw:
            for h in orw["hourly"]:
                if len(hourly_list) >= EXTENDED_HOURS: break
                hourly_list.append({
                    "time": h.get("time"),
                    "temperature": h.get("temperature"),
                    "humidity": h.get("humidity"),
                    "weather_desc": translate_desc(h.get("weather_desc"))
                })

    # --- Build telemetry 4 giờ tới ---
    for k in range(EXTENDED_HOURS):
        if k < len(hourly_list):
            h = hourly_list[k]
            telemetry[f"hour_{k+1}"] = h.get("time")
            telemetry[f"hour_{k+1}_temperature"] = h.get("temperature")
            telemetry[f"hour_{k+1}_humidity"] = h.get("humidity")
            telemetry[f"hour_{k+1}_weather_desc"] = h.get("weather_desc")

    # --- Realtime (lấy từ hour_1) ---
    telemetry["temperature_h"] = telemetry.get("hour_1_temperature")
    telemetry["humidity"] = telemetry.get("hour_1_humidity")

    # --- Location ---
    telemetry["latitude"] = LAT
    telemetry["longitude"] = LON
    telemetry["location"] = "An Phú, Hồ Chí Minh"

    return telemetry

# --------------------------------------
# Push to ThingsBoard
# --------------------------------------
def send_to_thingsboard(payload: dict):
    if not TB_DEVICE_URL:
        logger.warning("[TB] No TB_DEVICE_URL configured")
        return
    try:
        r = requests.post(TB_DEVICE_URL, json=payload, timeout=REQUEST_TIMEOUT)
        logger.info(f"[TB RESP] {r.status_code}")
    except Exception as e:
        logger.error(f"[TB ERROR] {e}")

# --------------------------------------
# Auto loop
# --------------------------------------
async def auto_loop():
    while True:
        telemetry = merge_weather_and_hours()
        logger.info(f"[TB PUSH] keys={list(telemetry.keys())}")
        send_to_thingsboard(telemetry)
        await asyncio.sleep(3600)  # mỗi giờ push 1 lần

# --------------------------------------
# FastAPI
# --------------------------------------
app = FastAPI()

@app.on_event("startup")
async def startup_event():
    logger.info(f"Startup: TB_DEVICE_URL present: {bool(TB_DEVICE_URL)}")
    asyncio.create_task(auto_loop())

@app.get("/")
def root():
    return {"status": "ok", "service": "Agri-bot"}

@app.get("/weather")
def get_weather():
    return merge_weather_and_hours()

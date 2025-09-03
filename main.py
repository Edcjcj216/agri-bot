# main.py
# Agri-bot — Weather + Sensor telemetry publisher

import os
import time
import json
import logging
import requests
import asyncio
from datetime import datetime, timedelta
from fastapi import FastAPI
from pydantic import BaseModel

# ---------------- CONFIG ----------------
THINGSBOARD_URL = os.getenv("THINGSBOARD_URL", "http://demo.thingsboard.io")
ACCESS_TOKEN = os.getenv("THINGSBOARD_TOKEN", "YOUR_ACCESS_TOKEN")

LATITUDE = 10.7639
LONGITUDE = 106.6563
LOCATION_NAME = "An Phú, Hồ Chí Minh"

OPEN_METEO_URL = (
    f"https://api.open-meteo.com/v1/forecast?"
    f"latitude={LATITUDE}&longitude={LONGITUDE}"
    "&hourly=temperature_2m,relative_humidity_2m,weathercode"
    "&daily=temperature_2m_max,temperature_2m_min,weathercode"
    "&timezone=auto"
)

# ---------------- FASTAPI APP ----------------
app = FastAPI()


class SensorData(BaseModel):
    temperature_h: float
    humidity: float
    illuminance: float
    avg_soil_moisture: float
    battery: float


# Weather code → Vietnamese description
WEATHER_CODES = {
    0: "Trời quang",
    1: "Ít mây",
    2: "Có mây",
    3: "Nhiều mây",
    45: "Sương mù",
    48: "Sương mù có sương giá",
    51: "Mưa phùn nhẹ",
    53: "Mưa phùn",
    55: "Mưa phùn dày",
    61: "Mưa nhỏ",
    63: "Mưa vừa",
    65: "Mưa to",
    71: "Tuyết nhẹ",
    73: "Tuyết vừa",
    75: "Tuyết dày",
    80: "Mưa rào nhẹ",
    81: "Mưa rào vừa",
    82: "Mưa rào to",
    95: "Có giông",
    96: "Có giông kèm mưa đá nhẹ",
    99: "Có giông kèm mưa đá to",
}


def get_weather_description(code: int) -> str:
    return WEATHER_CODES.get(code, "Không xác định")


# ---------------- WEATHER FETCHER ----------------
async def fetch_weather():
    try:
        response = requests.get(OPEN_METEO_URL, timeout=10)
        data = response.json()

        now = datetime.now()
        current_hour = (now + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)

        hours = data["hourly"]["time"]
        temps = data["hourly"]["temperature_2m"]
        hums = data["hourly"]["relative_humidity_2m"]
        weathers = data["hourly"]["weathercode"]

        # Dự báo 4 giờ sắp tới
        forecast = {}
        for i in range(4):
            target_time = current_hour + timedelta(hours=i)
            if target_time.isoformat() in hours:
                idx = hours.index(target_time.isoformat())
                forecast[f"hour_{i+1}"] = target_time.strftime("%H:%M")
                forecast[f"hour_{i+1}_temperature"] = temps[idx]
                forecast[f"hour_{i+1}_humidity"] = hums[idx]
                forecast[f"hour_{i+1}_weather_desc"] = get_weather_description(weathers[idx])

        # Thời tiết ngày mai
        forecast["weather_tomorrow_min"] = data["daily"]["temperature_2m_min"][1]
        forecast["weather_tomorrow_max"] = data["daily"]["temperature_2m_max"][1]
        forecast["weather_tomorrow_desc"] = get_weather_description(data["daily"]["weathercode"][1])

        # Thêm vị trí & toạ độ
        forecast["latitude"] = LATITUDE
        forecast["longitude"] = LONGITUDE
        forecast["location"] = LOCATION_NAME

        return forecast

    except Exception as e:
        logging.error(f"Lỗi lấy dự báo thời tiết: {e}")
        return {}


# ---------------- SEND TO THINGSBOARD ----------------
def send_telemetry(payload: dict):
    try:
        url = f"{THINGSBOARD_URL}/api/v1/{ACCESS_TOKEN}/telemetry"
        headers = {"Content-Type": "application/json"}
        requests.post(url, headers=headers, data=json.dumps(payload), timeout=5)
    except Exception as e:
        logging.error(f"Lỗi gửi telemetry: {e}")


# ---------------- TASK LOOP ----------------
async def telemetry_loop():
    while True:
        weather = await fetch_weather()

        # fake sensor data (thay bằng sensor thật nếu có)
        sensors = {
            "temperature_h": 29.8,
            "humidity": 69.1,
            "illuminance": 550,
            "avg_soil_moisture": 40.5,
            "battery": 4.19,
        }

        telemetry = {**weather, **sensors}
        send_telemetry(telemetry)

        await asyncio.sleep(300)  # gửi mỗi 5 phút


@app.on_event("startup")
async def startup_event():
    asyncio.create_task(telemetry_loop())

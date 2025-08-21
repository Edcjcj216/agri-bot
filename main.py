import os
import requests
import asyncio
from fastapi import FastAPI
from apscheduler.schedulers.asyncio import AsyncIOScheduler

app = FastAPI()

# === Config từ Environment Variables (Render) ===
OPENWEATHER_API_KEY = os.getenv("OPENWEATHER_API_KEY")
THINGSBOARD_TOKEN = os.getenv("THINGSBOARD_TOKEN")
LAT = os.getenv("LAT", "10.806094263669602")   # Vĩ độ (default: HCM)
LON = os.getenv("LON", "106.75222004270555")   # Kinh độ (default: HCM)

# === URL ThingsBoard Cloud ===
THINGSBOARD_URL = f"https://thingsboard.cloud/api/v1/{THINGSBOARD_TOKEN}/telemetry"


# Hàm lấy dữ liệu từ OpenWeather
def fetch_weather():
    url = (
        f"https://api.openweathermap.org/data/2.5/forecast?"
        f"lat={LAT}&lon={LON}&appid={OPENWEATHER_API_KEY}&units=metric&lang=vi"
    )
    response = requests.get(url)
    data = response.json()
    return data


# Hàm gửi dữ liệu lên ThingsBoard
def push_to_thingsboard():
    data = fetch_weather()

    # Lấy city name
    location_name = data.get("city", {}).get("name", "Unknown")

    # Lấy thời tiết hiện tại (list[0] gần nhất)
    current = data["list"][0]
    current_weather = {
        "location": location_name,  # Tên thành phố
        "current_temp": current["main"]["temp"],
        "current_humidity": current["main"]["humidity"],
        "current_weather": current["weather"][0]["description"],
    }

    # Lấy 24h forecast (8 cột = 24h vì mỗi cột cách 3h)
    forecast_24h = {}
    for i in range(8):
        hour_data = data["list"][i]
        key = f"forecast_{i*3}h"  # ví dụ forecast_0h, forecast_3h, ...
        forecast_24h[key] = {
            "temp": hour_data["main"]["temp"],
            "humidity": hour_data["main"]["humidity"],
            "weather": hour_data["weather"][0]["description"],
        }

    # Lấy forecast ngày mai (sau 24h = index 8)
    tomorrow = data["list"][8]
    tomorrow_forecast = {
        "tomorrow_temp": tomorrow["main"]["temp"],
        "tomorrow_humidity": tomorrow["main"]["humidity"],
        "tomorrow_weather": tomorrow["weather"][0]["description"],
    }

    # Gộp tất cả dữ liệu lại
    payload = {**current_weather, **forecast_24h, **tomorrow_forecast}

    # Push lên ThingsBoard
    requests.post(THINGSBOARD_URL, json=payload)


# Dùng scheduler để tự động 5 phút chạy 1 lần
scheduler = AsyncIOScheduler()
scheduler.add_job(push_to_thingsboard, "interval", minutes=5)
scheduler.start()


@app.get("/")
def root():
    return {"status": "Weather service running 🚀"}

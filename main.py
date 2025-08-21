import os
import requests
from fastapi import FastAPI
from apscheduler.schedulers.background import BackgroundScheduler
from datetime import datetime

app = FastAPI()

# Lấy API Key từ biến môi trường Render
OPENWEATHER_API_KEY = os.getenv("OPENWEATHER_API_KEY")
THINGSBOARD_TOKEN = os.getenv("THINGSBOARD_TOKEN")
LAT = "10.806094263669602"
LON = "106.75222004270555"
CROP_TYPE = "Rau Muống"

# URL ThingsBoard
THINGSBOARD_URL = f"https://thingsboard.cloud/api/v1/{THINGSBOARD_TOKEN}/telemetry"

# --- Hàm lấy dữ liệu thời tiết ---
def fetch_weather():
    url = "https://api.openweathermap.org/data/3.0/onecall"
    params = {
        "lat": LAT,
        "lon": LON,
        "appid": OPENWEATHER_API_KEY,
        "units": "metric",
        "lang": "vi"
    }
    resp = requests.get(url, params=params)
    resp.raise_for_status()
    return resp.json()

# --- Hàm gửi dữ liệu lên ThingsBoard ---
def push_to_thingsboard():
    try:
        data = fetch_weather()

        now = datetime.now().hour
        today_forecast = []
        tomorrow_forecast = []

        # lấy dự báo từng giờ
        for h in data["hourly"]:
            hour = datetime.fromtimestamp(h["dt"]).hour
            if hour >= now:  # còn lại trong hôm nay
                today_forecast.append({
                    "hour": hour,
                    "temp": h["temp"],
                    "weather": h["weather"][0]["description"]
                })
            else:  # thuộc ngày mai
                tomorrow_forecast.append({
                    "hour": hour,
                    "temp": h["temp"],
                    "weather": h["weather"][0]["description"]
                })

        payload = {
            "location": data.get("timezone", "Unknown"),  # city name từ OpenWeather
            "crop": CROP_TYPE,
            "current": {
                "temp": data["current"]["temp"],
                "weather": data["current"]["weather"][0]["description"]
            },
            "today": today_forecast,
            "tomorrow": tomorrow_forecast
        }

        r = requests.post(THINGSBOARD_URL, json=payload)
        r.raise_for_status()
        print("✅ Data pushed:", payload)

    except Exception as e:
        print("❌ Error pushing to ThingsBoard:", e)

# Scheduler: 5 phút chạy 1 lần
scheduler = BackgroundScheduler()
scheduler.add_job(push_to_thingsboard, "interval", minutes=5)
scheduler.start()

@app.get("/")
def root():
    return {"status": "ok", "message": "AgriBot is running"}

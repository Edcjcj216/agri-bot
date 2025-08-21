import os
import requests
import asyncio
import uvicorn
from fastapi import FastAPI
from datetime import datetime, timedelta

app = FastAPI()

# Lấy API key và token từ Render Environment
OPENWEATHER_API_KEY = os.getenv("OPENWEATHER_API_KEY")
THINGSBOARD_TOKEN = os.getenv("THINGSBOARD_TOKEN")

# Tọa độ của bạn (Ho Chi Minh City)
LAT = 10.806094263669602
LON = 106.75222004270555
CROP = "Rau muống"

# URL ThingsBoard
TB_URL = f"https://thingsboard.cloud/api/v1/{THINGSBOARD_TOKEN}/telemetry"

# Hàm lấy dữ liệu từ OpenWeather
def fetch_weather():
    url = f"http://api.openweathermap.org/data/2.5/onecall"
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

# Hàm push dữ liệu lên ThingsBoard
def push_to_thingsboard(payload):
    try:
        resp = requests.post(TB_URL, json=payload)
        if resp.status_code != 200:
            print("❌ Lỗi push:", resp.text)
        else:
            print("✅ Push thành công")
    except Exception as e:
        print("⚠️ Exception:", e)

# Hàm xử lý dữ liệu thời tiết và push
def process_and_push():
    data = fetch_weather()

    now = datetime.utcfromtimestamp(data["current"]["dt"]) + timedelta(seconds=data["timezone_offset"])
    today = now.date()
    tomorrow = today + timedelta(days=1)

    payload = {
        "location": data.get("timezone", "Unknown"),
        "latitude": LAT,
        "longitude": LON,
        "crop": CROP,
        "current_temp": data["current"]["temp"],
        "current_weather": data["current"]["weather"][0]["description"],
        "hourly_forecast": {},
        "tomorrow_forecast": {}
    }

    # Thêm các giờ còn lại trong hôm nay
    for hour in data["hourly"]:
        dt = datetime.utcfromtimestamp(hour["dt"]) + timedelta(seconds=data["timezone_offset"])
        if dt.date() == today and dt.hour >= now.hour:
            payload["hourly_forecast"][f"{dt.hour}:00"] = {
                "temp": hour["temp"],
                "weather": hour["weather"][0]["description"]
            }

    # Thêm dự báo nguyên ngày mai
    for day in data["daily"]:
        dt = datetime.utcfromtimestamp(day["dt"]) + timedelta(seconds=data["timezone_offset"])
        if dt.date() == tomorrow:
            payload["tomorrow_forecast"] = {
                "min_temp": day["temp"]["min"],
                "max_temp": day["temp"]["max"],
                "weather": day["weather"][0]["description"]
            }
            break

    push_to_thingsboard(payload)

# Task chạy nền mỗi 5 phút
async def background_task():
    while True:
        process_and_push()
        await asyncio.sleep(300)  # 5 phút

@app.on_event("startup")
async def startup_event():
    asyncio.create_task(background_task())

@app.get("/")
def root():
    return {"status": "Weather service is running 🚀"}

if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=int(os.getenv("PORT", 10000)))

import os
import requests
from fastapi import FastAPI
from apscheduler.schedulers.background import BackgroundScheduler
from datetime import datetime

# === Config từ Environment Render ===
OWM_API_KEY = os.getenv("OPENWEATHER_API_KEY")   # API key OpenWeather
THINGSBOARD_TOKEN = os.getenv("THINGSBOARD_TOKEN")  # Device token ThingsBoard
LAT = os.getenv("LAT", "10.806094263669602")  # Vĩ độ mặc định HCM
LON = os.getenv("LON", "106.75222004270555")  # Kinh độ mặc định HCM
CROP = os.getenv("CROP", "Rau muống")         # Loại cây trồng

# URL ThingsBoard telemetry
THINGSBOARD_URL = f"https://thingsboard.cloud/api/v1/{THINGSBOARD_TOKEN}/telemetry"

app = FastAPI()
scheduler = BackgroundScheduler()

# ================== Hàm lấy dự báo thời tiết ==================
def fetch_weather():
    try:
        url = (
            f"http://api.openweathermap.org/data/2.5/onecall"
            f"?lat={LAT}&lon={LON}&units=metric&lang=vi&appid={OWM_API_KEY}"
        )
        resp = requests.get(url)
        resp.raise_for_status()
        data = resp.json()

        # Lấy tên địa điểm từ API khác (vì OneCall không có city name)
        city_url = f"http://api.openweathermap.org/data/2.5/weather?lat={LAT}&lon={LON}&appid={OWM_API_KEY}&lang=vi"
        city_resp = requests.get(city_url)
        city_resp.raise_for_status()
        city_name = city_resp.json().get("name", "Unknown")

        now = datetime.now()
        current_hour = now.hour

        # Dự báo hiện tại
        current = {
            "hour": current_hour,
            "temp": data["current"]["temp"],
            "humidity": data["current"]["humidity"],
            "weather": data["current"]["weather"][0]["description"],
        }

        # Các giờ còn lại hôm nay
        today_forecast = []
        for h in data["hourly"]:
            hour = datetime.fromtimestamp(h["dt"]).hour
            if hour >= current_hour:  # chỉ lấy từ giờ hiện tại trở đi
                today_forecast.append({
                    "hour": hour,
                    "temp": h["temp"],
                    "humidity": h["humidity"],
                    "weather": h["weather"][0]["description"],
                })

        # Ngày mai (daily[1])
        tomorrow = {
            "min": data["daily"][1]["temp"]["min"],
            "max": data["daily"][1]["temp"]["max"],
            "humidity": data["daily"][1]["humidity"],
            "weather": data["daily"][1]["weather"][0]["description"],
        }

        # Payload gửi ThingsBoard
        payload = {
            "location": city_name,
            "crop": CROP,
            "current": current,
            "today": today_forecast,
            "tomorrow": tomorrow,
        }
        return payload

    except Exception as e:
        print(f"❌ Error fetching weather: {e}")
        return None

# ================== Hàm push lên ThingsBoard ==================
def push_to_thingsboard():
    weather = fetch_weather()
    if weather:
        try:
            resp = requests.post(THINGSBOARD_URL, json=weather)
            resp.raise_for_status()
            print(f"✅ Pushed telemetry at {datetime.now()}")
            print(f"Payload: {weather}")
        except Exception as e:
            print(f"❌ Error pushing to ThingsBoard: {e}")

# ================== Scheduler chạy 5 phút/lần ==================
scheduler.add_job(push_to_thingsboard, "interval", minutes=5)
scheduler.start()

# Gọi ngay lần đầu khi service khởi động
push_to_thingsboard()

# ================== Endpoint kiểm tra ==================
@app.get("/")
def root():
    return {"status": "ok", "message": "Weather bot running"}

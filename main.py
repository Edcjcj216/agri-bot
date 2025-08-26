import os
import requests
from fastapi import FastAPI
from datetime import datetime, timedelta
from apscheduler.schedulers.background import BackgroundScheduler

app = FastAPI()

# --- Config ---
OWM_API_KEY = os.getenv("OWM_API_KEY")  # OpenWeatherMap Key
DEVICE_TOKEN = os.getenv("DEVICE_TOKEN")  # ThingsBoard Device Token
THINGSBOARD_URL = os.getenv("THINGSBOARD_URL", "https://thingsboard.cloud/api/v1")

CROP_NAME = "Rau muống"
LOCATION = "Ho Chi Minh City"
LAT = 10.81
LON = 106.75

# --- 16 kiểu thời tiết cho tiếng Việt ---
WEATHER_TRANSLATE = {
    # Ánh sáng / Nhiệt
    "Nắng nhẹ / Nắng ấm": "Nắng nhẹ / Nắng ấm",
    "Nắng gắt / Nắng nóng": "Nắng gắt / Nắng nóng",
    "Trời hanh khô": "Trời hanh khô",
    "Trời lạnh": "Trời lạnh",

    # ☁️ Mây / Âm u
    "Trời âm u / Nhiều mây": "Trời âm u / Nhiều mây",
    "Che phủ hoàn toàn": "Che phủ hoàn toàn",

    # 🌧️ Mưa
    "Mưa phùn / Lất phất": "Mưa phùn / Lất phất",
    "Mưa nhẹ / Mưa vừa": "Mưa nhẹ / Mưa vừa",
    "Mưa to / Mưa lớn": "Mưa to / Mưa lớn",
    "Mưa rất to / Kéo dài": "Mưa rất to / Kéo dài",
    "Mưa rào": "Mưa rào",
    "Mưa rào kèm dông / Mưa dông": "Mưa rào kèm dông / Mưa dông",

    # ⚡ Gió / Dông
    "Dông / Sấm sét": "Dông / Sấm sét",
    "Gió giật mạnh": "Gió giật mạnh",

    # 🌀 Bão / Áp thấp
    "Áp thấp nhiệt đới / Bão / Siêu bão": "Áp thấp nhiệt đới / Bão / Siêu bão"
}

# --- Hàm lấy dự báo 7 giờ tới ---
def get_weather_data():
    url = f"https://api.openweathermap.org/data/2.5/onecall?lat={LAT}&lon={LON}&exclude=minutely,daily,alerts&units=metric&appid={OWM_API_KEY}&lang=en"
    r = requests.get(url, timeout=10)
    if r.status_code != 200:
        return None
    return r.json()

def process_forecast(data):
    now = datetime.now()
    # 4–7 giờ tới
    hourly_forecast = []
    for i in range(4, 8):
        if i < len(data['hourly']):
            h = data['hourly'][i]
            desc_en = h['weather'][0]['description'].title()
            desc_vn = WEATHER_TRANSLATE.get(desc_en, desc_en)
            hourly_forecast.append({
                "temperature": round(h['temp'], 1),
                "humidity": h['humidity'],
                "weather_desc": desc_vn,
                "weather_desc_en": desc_en
            })
        else:
            hourly_forecast.append({
                "temperature": "Không có dữ liệu",
                "humidity": "Không có dữ liệu",
                "weather_desc": "Không có dữ liệu",
                "weather_desc_en": "No data"
            })

    # Hôm nay
    today_data = data['daily'][0] if 'daily' in data and len(data['daily'])>0 else None
    today = {}
    if today_data:
        desc_en = today_data['weather'][0]['description'].title()
        desc_vn = WEATHER_TRANSLATE.get(desc_en, desc_en)
        today = {
            "min_temp": today_data['temp']['min'],
            "max_temp": today_data['temp']['max'],
            "avg_humidity": int(today_data.get('humidity', 0)),
            "weather_desc": desc_vn,
            "weather_desc_en": desc_en
        }
    else:
        today = {
            "min_temp": "Không có dữ liệu",
            "max_temp": "Không có dữ liệu",
            "avg_humidity": "Không có dữ liệu",
            "weather_desc": "Không có dữ liệu",
            "weather_desc_en": "No data"
        }

    # Hôm qua
    yesterday = {
        "min_temp": "Không có dữ liệu",
        "max_temp": "Không có dữ liệu",
        "avg_humidity": "Không có dữ liệu",
        "weather_desc": "Không có dữ liệu",
        "weather_desc_en": "No data"
    }

    # Ngày mai
    tomorrow_data = data['daily'][1] if 'daily' in data and len(data['daily'])>1 else None
    tomorrow = {}
    if tomorrow_data:
        desc_en = tomorrow_data['weather'][0]['description'].title()
        desc_vn = WEATHER_TRANSLATE.get(desc_en, desc_en)
        tomorrow = {
            "min_temp": tomorrow_data['temp']['min'],
            "max_temp": tomorrow_data['temp']['max'],
            "avg_humidity": int(tomorrow_data.get('humidity', 0)),
            "weather_desc": desc_vn,
            "weather_desc_en": desc_en
        }
    else:
        tomorrow = {
            "min_temp": "Không có dữ liệu",
            "max_temp": "Không có dữ liệu",
            "avg_humidity": "Không có dữ liệu",
            "weather_desc": "Không có dữ liệu",
            "weather_desc_en": "No data"
        }

    return hourly_forecast, yesterday, today, tomorrow

def push_to_thingsboard(payload):
    url = f"{THINGSBOARD_URL}/{DEVICE_TOKEN}/telemetry"
    try:
        r = requests.post(url, json=payload, timeout=10)
        r.raise_for_status()
    except Exception as e:
        print("Error pushing to ThingsBoard:", e)

def build_payload():
    data = get_weather_data()
    if not data:
        return {}

    hourly_forecast, yesterday, today, tomorrow = process_forecast(data)

    payload = {
        "crop": CROP_NAME,
        "location": LOCATION,
        "time": datetime.now().isoformat(),
        "hourly_forecast": hourly_forecast,
        "yesterday": yesterday,
        "today": today,
        "tomorrow": tomorrow
    }
    return payload

def job():
    payload = build_payload()
    if payload:
        push_to_thingsboard(payload)
        print("Telemetry pushed:", payload)

# --- Scheduler ---
scheduler = BackgroundScheduler()
scheduler.add_job(job, 'interval', minutes=5)
scheduler.start()

@app.get("/")
def root():
    return {"status": "running"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=int(os.environ.get("PORT", 8000)), log_level="info")

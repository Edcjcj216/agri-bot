import os
import requests
from fastapi import FastAPI
from datetime import datetime, timedelta

app = FastAPI()

WEATHER_API_KEY = os.getenv("WEATHER_API_KEY")
DEVICE_TOKEN = os.getenv("TB_TOKEN")
THINGSBOARD_URL = "https://thingsboard.cloud/api/v1"

# 16 kiểu thời tiết theo yêu cầu
VIET_CONDITIONS = {
    "Sunny": "Nắng nhẹ / Nắng ấm",
    "Hot": "Nắng gắt / Nắng nóng",
    "Dry": "Trời hanh khô",
    "Cold": "Trời lạnh",
    "Cloudy": "Trời âm u / Nhiều mây",
    "Overcast": "Che phủ hoàn toàn",
    "Drizzle": "Mưa phùn / Lất phất",
    "Light rain": "Mưa nhẹ / Mưa vừa",
    "Moderate rain": "Mưa vừa",
    "Heavy rain": "Mưa to / Mưa lớn",
    "Very heavy rain": "Mưa rất to / Kéo dài",
    "Showers": "Mưa rào",
    "Thunderstorm": "Mưa dông / Mưa rào kèm dông",
    "Wind": "Gió giật mạnh",
    "Storm": "Áp thấp / Bão / Siêu bão",
    "Thunder / Lightning": "Dông / Sấm sét"
}

def fetch_weather(lat=10.81, lon=106.75):
    url = f"http://api.weatherapi.com/v1/forecast.json?key={WEATHER_API_KEY}&q={lat},{lon}&days=2&aqi=no&alerts=no"
    r = requests.get(url)
    data = r.json()
    return data

def process_weather(data):
    telemetry = {}
    # Crop & location
    telemetry["crop"] = "Rau muống"
    telemetry["location"] = data["location"]["name"]
    telemetry["time"] = datetime.utcnow().isoformat()

    # 4–7 giờ tới
    forecast_hours = data["forecast"]["forecastday"][0]["hour"]
    for i in range(4, 8):  # 4–7 giờ tới
        h = forecast_hours[i]
        telemetry[f"hour_{i}_temperature"] = h["temp_c"]
        telemetry[f"hour_{i}_humidity"] = h["humidity"]
        desc_en = h["condition"]["text"]
        telemetry[f"hour_{i}_weather_desc_en"] = desc_en
        telemetry[f"hour_{i}_weather_desc"] = VIET_CONDITIONS.get(desc_en, desc_en)

    # Hôm qua, hôm nay, ngày mai
    days = ["yesterday", "today", "tomorrow"]
    for idx, day in enumerate(days):
        if day == "yesterday":
            # WeatherAPI free không trả hôm qua trực tiếp, dùng today - 1
            dt = datetime.utcnow() - timedelta(days=1)
            telemetry[f"weather_{day}_min"] = None
            telemetry[f"weather_{day}_max"] = None
            telemetry[f"weather_{day}_avg_humidity"] = None
            telemetry[f"weather_{day}_desc"] = "Không có dữ liệu"
            telemetry[f"weather_{day}_desc_en"] = "No data"
        else:
            f_day = data["forecast"]["forecastday"][idx]
            telemetry[f"weather_{day}_min"] = f_day["day"]["mintemp_c"]
            telemetry[f"weather_{day}_max"] = f_day["day"]["maxtemp_c"]
            telemetry[f"weather_{day}_avg_humidity"] = f_day["day"]["avghumidity"]
            desc_en = f_day["day"]["condition"]["text"]
            telemetry[f"weather_{day}_desc_en"] = desc_en
            telemetry[f"weather_{day}_desc"] = VIET_CONDITIONS.get(desc_en, desc_en)

    return telemetry

def push_to_thingsboard(telemetry):
    url = f"{THINGSBOARD_URL}/{DEVICE_TOKEN}/telemetry"
    try:
        requests.post(url, json=telemetry, timeout=10)
    except Exception as e:
        print("Failed to push telemetry:", e)

@app.get("/update_weather")
def update_weather():
    data = fetch_weather()
    telemetry = process_weather(data)
    push_to_thingsboard(telemetry)
    return {"status": "ok", "telemetry": telemetry}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=int(os.getenv("PORT", 8000)))

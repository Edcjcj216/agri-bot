import os
import requests
from fastapi import FastAPI
from datetime import datetime, timedelta
import pytz

app = FastAPI()

OWM_API_KEY = os.getenv("OWM_API_KEY")
LOCATION = "Ho Chi Minh City"
LAT = 10.81
LON = 106.75
CROP = "Rau muống"

# Chỉ 16 kiểu thời tiết được dùng
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

def fetch_weather():
    url = (
        f"https://api.openweathermap.org/data/2.5/onecall?"
        f"lat={LAT}&lon={LON}&exclude=minutely,current,alerts&appid={OWM_API_KEY}&units=metric&lang=vi"
    )
    resp = requests.get(url)
    if resp.status_code != 200:
        print("❌ Lỗi OpenWeatherMap:", resp.text)
        return None
    return resp.json()

def parse_forecast(data):
    telemetry = {}
    now = datetime.utcnow()
    
    # --- 4–7 giờ tới ---
    for i in range(4, 8):
        key_prefix = f"hour_{i}_"
        try:
            hour_data = data["hourly"][i]
            telemetry[key_prefix + "temperature"] = round(hour_data.get("temp", 0), 2)
            telemetry[key_prefix + "humidity"] = hour_data.get("humidity", 0)
            desc = hour_data.get("weather", [{}])[0].get("description", "Không có dữ liệu")
            telemetry[key_prefix + "weather_desc"] = WEATHER_TRANSLATE.get(desc, desc)
            telemetry[key_prefix + "weather_desc_en"] = hour_data.get("weather", [{}])[0].get("main", "No data")
        except (IndexError, KeyError):
            telemetry[key_prefix + "temperature"] = "Không có dữ liệu"
            telemetry[key_prefix + "humidity"] = "Không có dữ liệu"
            telemetry[key_prefix + "weather_desc"] = "Không có dữ liệu"
            telemetry[key_prefix + "weather_desc_en"] = "No data"
    
    # --- Hôm nay / hôm qua / ngày mai ---
    daily_map = {"yesterday": -1, "today": 0, "tomorrow": 1}
    for key, idx in daily_map.items():
        try:
            if idx == -1:
                # Hôm qua lấy từ daily[0] trừ 1 ngày
                dt = datetime.utcfromtimestamp(data["daily"][0]["dt"]) - timedelta(days=1)
                daily_data = data["daily"][0]
            else:
                daily_data = data["daily"][idx]
                dt = datetime.utcfromtimestamp(daily_data["dt"])
            telemetry[f"{key}_min_temp"] = round(daily_data.get("temp", {}).get("min", 0), 2)
            telemetry[f"{key}_max_temp"] = round(daily_data.get("temp", {}).get("max", 0), 2)
            telemetry[f"{key}_avg_humidity"] = round(daily_data.get("humidity", 0), 2)
            desc = daily_data.get("weather", [{}])[0].get("description", "Không có dữ liệu")
            telemetry[f"{key}_weather_desc"] = WEATHER_TRANSLATE.get(desc, desc)
            telemetry[f"{key}_weather_desc_en"] = daily_data.get("weather", [{}])[0].get("main", "No data")
        except (IndexError, KeyError):
            telemetry[f"{key}_min_temp"] = "Không có dữ liệu"
            telemetry[f"{key}_max_temp"] = "Không có dữ liệu"
            telemetry[f"{key}_avg_humidity"] = "Không có dữ liệu"
            telemetry[f"{key}_weather_desc"] = "Không có dữ liệu"
            telemetry[f"{key}_weather_desc_en"] = "No data"
    
    telemetry["crop"] = CROP
    telemetry["location"] = LOCATION
    telemetry["time"] = now.isoformat()
    return telemetry

def push_telemetry(telemetry):
    # Placeholder: bạn replace bằng MQTT / ThingsBoard push
    print("📡 Telemetry push:")
    for k, v in telemetry.items():
        print(f"{k}: {v}")

@app.on_event("startup")
def startup_event():
    data = fetch_weather()
    if not data:
        print("❌ Không lấy được dữ liệu thời tiết")
        return
    telemetry = parse_forecast(data)
    push_telemetry(telemetry)

@app.get("/")
def root():
    return {"status": "ok", "message": "Dữ liệu thời tiết đã push telemetry."}

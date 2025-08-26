# main.py
import os
import requests
from fastapi import FastAPI
from datetime import datetime, timedelta

app = FastAPI()

# --- Cấu hình ---
OWM_API_KEY = os.getenv("OWM_API_KEY")  # OpenWeatherMap API Key
DEVICE_TOKEN = os.getenv("DEVICE_TOKEN")  # ThingsBoard Device Token
TB_URL = "https://thingsboard.cloud/api/v1/{}/telemetry".format(DEVICE_TOKEN)
LOCATION = {"lat": 10.81, "lon": 106.75}  # HCM City

# --- Chỉ 15 kiểu thời tiết tiếng Việt ---
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

def get_weather():
    """Lấy dữ liệu thời tiết từ OpenWeatherMap OneCall API"""
    url = (
        f"https://api.openweathermap.org/data/2.5/onecall"
        f"?lat={LOCATION['lat']}&lon={LOCATION['lon']}&exclude=minutely,current,alerts"
        f"&units=metric&lang=vi&appid={OWM_API_KEY}"
    )
    resp = requests.get(url)
    data = resp.json()
    return data

def parse_forecast(data):
    """Chuẩn hóa dữ liệu 4–7 giờ tới + hôm qua/hôm nay/ngày mai"""
    telemetry = {}
    now = datetime.utcfromtimestamp(data['hourly'][0]['dt'] + data['timezone_offset'])
    
    # 4–7 giờ tới
    for i in range(4, 8):
        if i < len(data['hourly']):
            hour_data = data['hourly'][i]
            key_prefix = f"hour_{i}_"
            temp = round(hour_data['temp'], 1)
            hum = round(hour_data['humidity'], 1)
            desc_en = hour_data['weather'][0]['description'].capitalize()
            desc_vi = WEATHER_TRANSLATE.get(desc_en, "Không có dữ liệu")
            telemetry[key_prefix + "temperature"] = temp
            telemetry[key_prefix + "humidity"] = hum
            telemetry[key_prefix + "weather_desc"] = desc_vi
            telemetry[key_prefix + "weather_desc_en"] = desc_en
        else:
            key_prefix = f"hour_{i}_"
            telemetry[key_prefix + "temperature"] = "Không có dữ liệu"
            telemetry[key_prefix + "humidity"] = "Không có dữ liệu"
            telemetry[key_prefix + "weather_desc"] = "Không có dữ liệu"
            telemetry[key_prefix + "weather_desc_en"] = "No data"
    
    # Hôm nay
    today = data['daily'][0]
    telemetry.update({
        "today_min_temp": round(today['temp']['min'], 1),
        "today_max_temp": round(today['temp']['max'], 1),
        "today_avg_humidity": round(today['humidity'], 1),
        "today_weather_desc": WEATHER_TRANSLATE.get(today['weather'][0]['description'].capitalize(), "Không có dữ liệu"),
        "today_weather_desc_en": today['weather'][0]['description'].capitalize()
    })
    
    # Ngày mai
    tomorrow = data['daily'][1]
    telemetry.update({
        "tomorrow_min_temp": round(tomorrow['temp']['min'], 1),
        "tomorrow_max_temp": round(tomorrow['temp']['max'], 1),
        "tomorrow_avg_humidity": round(tomorrow['humidity'], 1),
        "tomorrow_weather_desc": WEATHER_TRANSLATE.get(tomorrow['weather'][0]['description'].capitalize(), "Không có dữ liệu"),
        "tomorrow_weather_desc_en": tomorrow['weather'][0]['description'].capitalize()
    })
    
    # Hôm qua (không có dữ liệu từ OWM, dùng placeholder)
    telemetry.update({
        "yesterday_min_temp": "Không có dữ liệu",
        "yesterday_max_temp": "Không có dữ liệu",
        "yesterday_avg_humidity": "Không có dữ liệu",
        "yesterday_weather_desc": "Không có dữ liệu",
        "yesterday_weather_desc_en": "No data"
    })
    
    # Crop, location, time
    telemetry.update({
        "crop": "Rau muống",
        "location": "Ho Chi Minh City",
        "time": datetime.utcnow().isoformat()
    })
    
    return telemetry

def push_telemetry(telemetry):
    """Gửi dữ liệu lên ThingsBoard"""
    try:
        r = requests.post(TB_URL, json=telemetry, timeout=10)
        r.raise_for_status()
        print("✅ Telemetry pushed successfully")
    except Exception as e:
        print("❌ Failed to push telemetry:", e)

@app.on_event("startup")
def startup_event():
    """Gửi ngay khi deploy"""
    data = get_weather()
    telemetry = parse_forecast(data)
    push_telemetry(telemetry)

@app.get("/")
def read_root():
    return {"status": "OK, telemetry pushed"}


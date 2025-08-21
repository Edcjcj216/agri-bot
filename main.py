import os
import time
import json
import logging
import threading
import requests
from fastapi import FastAPI

# ================== CONFIG ==================
TB_DEMO_TOKEN = os.getenv("TB_DEMO_TOKEN", "I1s5bI2FQCZw6umLvwLG")
TB_DEVICE_URL = f"https://thingsboard.cloud/api/v1/{TB_DEMO_TOKEN}/telemetry"
OWM_API_KEY = os.getenv("OWM_API_KEY", "")
LAT = float(os.getenv("LAT", "10.79"))
LON = float(os.getenv("LON", "106.70"))
LOOP_INTERVAL = 300  # 5 phút

# ================== LOGGING ==================
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ================== FASTAPI ==================
app = FastAPI()
_last_payload = None

# ================== WEATHER ==================
OWM_DESC_VI = {
    "clear sky": "Trời quang",
    "few clouds": "Trời quang nhẹ",
    "scattered clouds": "Có mây",
    "broken clouds": "Nhiều mây",
    "shower rain": "Mưa rào",
    "rain": "Mưa",
    "light rain": "Mưa nhẹ",
    "moderate rain": "Mưa vừa",
    "heavy intensity rain": "Mưa to",
    "thunderstorm": "Giông",
    "snow": "Tuyết",
    "mist": "Sương mù"
}

def _empty_weather():
    keys = ["weather_yesterday_desc","weather_yesterday_max","weather_yesterday_min","humidity_yesterday",
            "weather_today_desc","weather_today_max","weather_today_min","humidity_today",
            "weather_tomorrow_desc","weather_tomorrow_max","weather_tomorrow_min","humidity_tomorrow"]
    return {k: 0 if "max" in k or "min" in k or "humidity" in k else "?" for k in keys}

def get_weather_forecast():
    if not OWM_API_KEY:
        logger.warning("OWM_API_KEY chưa cấu hình, dùng giá trị mẫu")
        return _empty_weather()
    try:
        now = int(time.time())
        yesterday = now - 86400

        # Forecast
        r = requests.get("https://api.openweathermap.org/data/2.5/onecall", params={
            "lat": LAT, "lon": LON, "exclude": "minutely,hourly,alerts", "units": "metric", "appid": OWM_API_KEY
        }, timeout=10)
        r.raise_for_status()
        data = r.json()
        daily = data.get("daily", [])

        def extract_day_info(day):
            temp = day.get("temp", {})
            weather = day.get("weather",[{}])[0].get("description","?")
            humidity = day.get("humidity",0)
            return {
                "desc": OWM_DESC_VI.get(weather, weather),
                "max": temp.get("max",0),
                "min": temp.get("min",0),
                "humidity": humidity
            }

        # Yesterday
        r_hist = requests.get("https://api.openweathermap.org/data/2.5/onecall/timemachine", params={
            "lat": LAT, "lon": LON, "dt": yesterday, "units": "metric", "appid": OWM_API_KEY
        }, timeout=10)
        r_hist.raise_for_status()
        hist_data = r_hist.json()
        hist_temp_list = [h["temp"] for h in hist_data.get("hourly",[])]
        hist_humi_list = [h["humidity"] for h in hist_data.get("hourly",[])]
        weather_result = {
            "weather_yesterday_desc": OWM_DESC_VI.get(hist_data.get("current",{}).get("weather",[{}])[0].get("description","?"),"?"),
            "weather_yesterday_max": max(hist_temp_list) if hist_temp_list else 0,
            "weather_yesterday_min": min(hist_temp_list) if hist_temp_list else 0,
            "humidity_yesterday": round(sum(hist_humi_list)/len(hist_humi_list),1) if hist_humi_list else 0
        }

        today_info = extract_day_info(daily[0]) if len(daily)>0 else {"desc":"?","max":0,"min":0,"humidity":0}
        tomorrow_info = extract_day_info(daily[1]) if len(daily)>1 else {"desc":"?","max":0,"min":0,"humidity":0}

        weather_result.update({
            "weather_today_desc": today_info["desc"],
            "weather_today_max": today_info["max"],
            "weather_today_min": today_info["min"],
            "humidity_today": today_info["humidity"],
            "weather_tomorrow_desc": tomorrow_info["desc"],
            "weather_tomorrow_max": tomorrow_info["max"],
            "weather_tomorrow_min": tomorrow_info["min"],
            "humidity_tomorrow": tomorrow_info["humidity"]
        })
        return weather_result
    except Exception as e:
        logger.warning(f"Weather API error: {e}")
        return _empty_weather()

# ================== AI ADVICE ==================
def call_ai_advice(temperature, humidity, weather_desc):
    # Local logic linh hoạt
    care_options = []
    if temperature > 35:
        care_options.append("Tránh nắng gắt")
    elif temperature >= 30:
        care_options.append("Tưới đủ nước")
    elif temperature <= 15:
        care_options.append("Giữ ấm")
    else:
        care_options.append("Nhiệt độ bình thường")

    if humidity <= 40:
        care_options.append("Độ ẩm thấp: tăng tưới")
    elif humidity <= 60:
        care_options.append("Độ ẩm hơi thấp: theo dõi, tưới khi cần")
    elif humidity >= 85:
        care_options.append("Độ ẩm cao: tránh úng, kiểm tra thoát nước")
    else:
        care_options.append("Độ ẩm ổn định cho rau muống")

    nutrition = ["Ưu tiên Kali (K)", "Cân bằng NPK", "Bón phân hữu cơ"]

    return {
        "prediction": f"Nhiệt độ {temperature:.1f}°C, độ ẩm {humidity:.1f}%",
        "advice_nutrition": " | ".join(nutrition),
        "advice_care": " | ".join(care_options),
        "advice_note": "Quan sát cây trồng và điều chỉnh thực tế",
        "advice": " | ".join(nutrition + care_options + ["Quan sát cây trồng và điều chỉnh thực tế"])
    }

# ================== TELEMETRY ==================
def send_telemetry(sensor_data):
    global _last_payload
    weather = get_weather_forecast()
    ai_advice = call_ai_advice(sensor_data['temperature'], sensor_data['humidity'], weather['weather_today_desc'])
    payload = {**sensor_data, **weather, **ai_advice}
    try:
        r = requests.post(TB_DEVICE_URL, json=payload, timeout=10)
        r.raise_for_status()
        logger.info(f"Telemetry sent: {payload}")
        _last_payload = payload
    except Exception as e:
        logger.error(f"Send telemetry failed: {e}")

# ================== BACKGROUND LOOP ==================
def telemetry_loop():
    while True:
        # Nếu ESP32 đã gửi lên TB, lấy dữ liệu từ TB hoặc giả lập tạm sensor
        sensor_data = {
            "temperature": round(random.uniform(20,35),1),  # Thay bằng dữ liệu ESP32 nếu cần
            "humidity": round(random.uniform(40,85),1),
            "battery_voltage": round(random.uniform(3.5,4.2),2),
            "battery_percent": round(random.uniform(60,100),0),
            "crop": "Rau muống",
            "location": "An Phú, Hồ Chí Minh"
        }
        send_telemetry(sensor_data)
        time.sleep(LOOP_INTERVAL)

@app.on_event("startup")
def start_background_loop():
    threading.Thread(target=telemetry_loop, daemon=True).start()

@app.get("/last")
def last_telemetry():
    return _last_payload or {"message": "Chưa có dữ liệu"}

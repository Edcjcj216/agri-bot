import os
import time
import json
import logging
import requests
from datetime import datetime
import random

# ================== CONFIG ==================
TB_DEMO_TOKEN = os.getenv("TB_DEMO_TOKEN", "I1s5bI2FQCZw6umLvwLG")
TB_DEVICE_URL = f"https://thingsboard.cloud/api/v1/{TB_DEMO_TOKEN}/telemetry"

AI_API_URL = os.getenv("AI_API_URL", "https://api-inference.huggingface.co/models/gpt2")
HF_TOKEN = os.getenv("HF_TOKEN", "")

OWM_API_KEY = os.getenv("OWM_API_KEY", "")
LAT = float(os.getenv("LAT", "10.79"))
LON = float(os.getenv("LON", "106.70"))

LOOP_INTERVAL = 300  # 5 phút

# ================== LOGGING ==================
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

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
def call_ai_api(data: dict):
    prompt = f"Nhiệt độ {data['temperature']:.1f}°C, độ ẩm {data['humidity']:.1f}%, thời tiết {data.get('weather_today_desc','?')}."
    try:
        headers = {"Authorization": f"Bearer {HF_TOKEN}"} if HF_TOKEN else {}
        body = {"inputs": prompt, "options":{"wait_for_model":True}}
        r = requests.post(AI_API_URL, headers=headers, json=body, timeout=20)
        r.raise_for_status()
        out = r.json()
        text = ""
        if isinstance(out, list) and out:
            text = out[0].get("generated_text","") if isinstance(out[0],dict) else str(out[0])
        if text.strip():
            return _parse_ai_advice(text, data)
    except Exception as e:
        logger.warning(f"AI API failed: {e}")
    return _local_ai_advice(data)

def _parse_ai_advice(text:str, data:dict):
    advice_care = text.strip().replace("\n"," | ")
    nutrition = ["Ưu tiên Kali (K)","Cân bằng NPK","Bón phân hữu cơ"]
    return {
        "prediction": f"Nhiệt độ {data['temperature']:.1f}°C, độ ẩm {data['humidity']:.1f}%",
        "advice_nutrition": " | ".join(nutrition),
        "advice_care": advice_care,
        "advice_note": "Quan sát cây trồng và điều chỉnh thực tế",
        "advice": " | ".join(nutrition + [advice_care] + ["Quan sát cây trồng và điều chỉnh thực tế"])
    }

def _local_ai_advice(data:dict):
    temp = data['temperature']
    humi = data['humidity']
    care_options = []

    if temp > 35:
        care_options.append(random.choice(["Tránh nắng gắt", "Tưới sáng sớm/chiều mát"]))
    elif temp >= 30:
        care_options.append(random.choice(["Tưới đủ nước", "Theo dõi sâu bệnh"]))
    elif temp <= 15:
        care_options.append(random.choice(["Giữ ấm", "Tránh sương muối"]))
    else:
        care_options.append("Nhiệt độ bình thường")

    if humi <= 40:
        care_options.append("Độ ẩm thấp: tăng tưới")
    elif humi <= 60:
        care_options.append("Độ ẩm hơi thấp: theo dõi, tưới khi cần")
    elif humi >= 85:
        care_options.append("Độ ẩm cao: tránh úng, kiểm tra thoát nước")
    else:
        care_options.append("Độ ẩm ổn định cho rau muống")

    nutrition = ["Ưu tiên Kali (K)", "Cân bằng NPK", "Bón phân hữu cơ"]

    return {
        "prediction": f"Nhiệt độ {temp:.1f}°C, độ ẩm {humi:.1f}%",
        "advice_nutrition": " | ".join(nutrition),
        "advice_care": " | ".join(care_options),
        "advice_note": "Quan sát cây trồng và điều chỉnh thực tế",
        "advice": " | ".join(nutrition + care_options + ["Quan sát cây trồng và điều chỉnh thực tế"])
    }

# ================== TELEMETRY ==================
def read_sensors():
    # Giả lập ESP32 sensor
    return {
        "temperature": round(random.uniform(20,35),1),
        "humidity": round(random.uniform(40,85),1),
        "battery_voltage": round(random.uniform(3.5,4.2),2),
        "battery_percent": random.randint(60,100)
    }

def send_telemetry():
    sensor_data = read_sensors()
    weather = get_weather_forecast()
    ai_advice = call_ai_api(sensor_data | {"weather_today_desc": weather.get("weather_today_desc","?")})
    payload = sensor_data | weather | ai_advice
    try:
        r = requests.post(TB_DEVICE_URL, json=payload, timeout=10)
        r.raise_for_status()
        logger.info(f"Telemetry sent: {json.dumps(payload)}")
    except Exception as e:
        logger.error(f"Send telemetry failed: {e}")

# ================== MAIN LOOP ==================
if __name__ == "__main__":
    logger.info("Starting telemetry loop...")
    while True:
        send_telemetry()
        time.sleep(LOOP_INTERVAL)

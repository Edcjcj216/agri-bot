import os
import json
import logging
import requests
from datetime import datetime
from fastapi import FastAPI
import uvicorn
from apscheduler.schedulers.background import BackgroundScheduler

# ================== CONFIG ==================
TB_URL = "https://thingsboard.cloud/api/v1"
TB_TOKEN = os.getenv("TB_DEMO_TOKEN", "your_tb_token_here")
OPENWEATHER_KEY = os.getenv("OPENWEATHER_KEY", "your_openweather_key_here")
LOCATION = "Ho Chi Minh,vn"
LAT, LON = 10.7769, 106.7009  # Hồ Chí Minh
PORT = int(os.getenv("PORT", 10000))

# Logging setup
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

# FastAPI app
app = FastAPI()
scheduler = BackgroundScheduler()

# ================== HELPERS ==================
def tb_post(payload: dict):
    url = f"{TB_URL}/{TB_TOKEN}/telemetry"
    try:
        r = requests.post(url, json=payload, timeout=10)
        logging.info(f"TB POST {r.status_code}: {payload}")
    except Exception as e:
        logging.error(f"Error posting TB: {e}")

def get_weather():
    """Fetch weather from OpenWeather: current + forecast + yesterday"""
    try:
        # current
        current_url = f"https://api.openweathermap.org/data/2.5/weather?lat={LAT}&lon={LON}&appid={OPENWEATHER_KEY}&units=metric&lang=vi"
        current = requests.get(current_url).json()

        # forecast 5 days / 3h
        forecast_url = f"https://api.openweathermap.org/data/2.5/forecast?lat={LAT}&lon={LON}&appid={OPENWEATHER_KEY}&units=metric&lang=vi"
        forecast = requests.get(forecast_url).json()

        # yesterday (use OneCall timemachine)
        import time
        yesterday_ts = int(time.time()) - 86400
        hist_url = f"https://api.openweathermap.org/data/2.5/onecall/timemachine?lat={LAT}&lon={LON}&dt={yesterday_ts}&appid={OPENWEATHER_KEY}&units=metric&lang=vi"
        hist = requests.get(hist_url).json()

        return current, forecast, hist
    except Exception as e:
        logging.error(f"Weather fetch error: {e}")
        return None, None, None

def call_ai(weather_summary: str) -> dict:
    """Fake AI for now: normally call OpenRouter/HF, here return static"""
    advice_nutrition = "Ưu tiên Kali (K) | Cân bằng NPK | Bón phân hữu cơ"
    advice_care = "Nhiệt độ trong ngưỡng an toàn | Độ ẩm cao: tránh úng; kiểm tra hệ thống thoát nước | Dự báo mưa sắp tới: giảm bón, tránh tưới trước cơn mưa | Gió mạnh dự báo: chằng buộc, bảo vệ cây non"
    advice_note = "Quan sát thực tế và điều chỉnh"
    advice = f"{advice_nutrition} | {advice_care} | {advice_note}"
    return {
        "advice": advice,
        "advice_nutrition": advice_nutrition,
        "advice_care": advice_care,
        "advice_note": advice_note,
    }

def build_payload():
    current, forecast, hist = get_weather()
    if not current or not forecast or not hist:
        return {}

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # current
    temp = current["main"]["temp"]
    hum = current["main"]["humidity"]
    desc = current["weather"][0]["description"]

    # today forecast (first 8 slots ~ 24h)
    today_temp_max = max([f["main"]["temp_max"] for f in forecast["list"][:8]])
    today_temp_min = min([f["main"]["temp_min"] for f in forecast["list"][:8]])
    today_desc = forecast["list"][0]["weather"][0]["description"]

    # tomorrow (next 8 slots)
    tomorrow_temp_max = max([f["main"]["temp_max"] for f in forecast["list"][8:16]])
    tomorrow_temp_min = min([f["main"]["temp_min"] for f in forecast["list"][8:16]])
    tomorrow_desc = forecast["list"][8]["weather"][0]["description"]

    # yesterday
    y_temps = [h["temp"] for h in hist.get("hourly", [])]
    if y_temps:
        y_temp_max = max(y_temps)
        y_temp_min = min(y_temps)
    else:
        y_temp_max, y_temp_min = 0, 0
    y_desc = hist.get("current", {}).get("weather", [{}])[0].get("description", "N/A")

    # hourly forecast (first 7h)
    hours = {}
    for i in range(7):
        f = forecast["list"][i]
        t = datetime.fromtimestamp(f["dt"]).strftime("%H:00")
        hours[f"hour_{i}"] = t
        hours[f"hour_{i}_temperature"] = f["main"]["temp"]
        hours[f"hour_{i}_humidity"] = f["main"]["humidity"]
        hours[f"hour_{i}_weather_desc"] = f["weather"][0]["description"]
        hours[f"hour_{i}_temperature_corrected"] = f["main"]["temp"] - 5.0

    # AI advice
    ai_advice = call_ai(desc)

    payload = {
        "ts": now,
        "crop": "Rau muống",
        "location": "An Phú, Hồ Chí Minh",
        "temperature": temp - 5.0,
        "humidity": hum + 0.0,
        "prediction": f"Nhiệt độ {temp-5.0}°C, độ ẩm {hum:.1f}%",
        "forecast_bias": -5.0,
        "forecast_history_len": 7,
        "weather_today_max": today_temp_max,
        "weather_today_min": today_temp_min,
        "weather_today_desc": today_desc,
        "weather_tomorrow_max": tomorrow_temp_max,
        "weather_tomorrow_min": tomorrow_temp_min,
        "weather_tomorrow_desc": tomorrow_desc,
        "weather_yesterday_max": y_temp_max,
        "weather_yesterday_min": y_temp_min,
        "weather_yesterday_desc": y_desc,
        **hours,
        **ai_advice,
        "llm_advice": {"skipped": "disabled/backoff_or_rate_or_history"},
    }
    return payload

def job():
    payload = build_payload()
    if payload:
        tb_post(payload)

# ================== FASTAPI ==================
@app.get("/")
def root():
    return {"status": "ok"}

# ================== MAIN ==================
if __name__ == "__main__":
    scheduler.add_job(job, "interval", minutes=5)
    scheduler.start()
    logging.info("Startup: sending first payload")
    job()
    uvicorn.run(app, host="0.0.0.0", port=PORT)

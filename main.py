import requests
import time
import logging
import asyncio
from fastapi import FastAPI, Request
from pydantic import BaseModel
from threading import Thread

# ================= CONFIG =================
THINGSBOARD_URL = "http://demo.thingsboard.io/api/v1"
THINGSBOARD_TOKEN = "I1s5bI2FQCZw6umLvwLG"

LAT, LON = 10.8019, 106.7463
OPENWEATHER_KEY = "a53f443795604c41b72305c1806784db"

# ================= LOGGING =================
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("main")

# ================= FASTAPI APP =================
app = FastAPI()

class SensorData(BaseModel):
    temperature: float
    humidity: float
    battery: float | None = None
    crop: str | None = "Rau muống"

# ================= WEATHER =================
def get_weather_forecast():
    try:
        url = "https://api.openweathermap.org/data/2.5/onecall"  # One Call 2.5
        params = {
            "lat": LAT,
            "lon": LON,
            "exclude": "current,minutely,hourly,alerts",
            "appid": OPENWEATHER_KEY,
            "units": "metric",
            "lang": "vi"
        }
        r = requests.get(url, params=params, timeout=10)
        r.raise_for_status()
        data = r.json()
        daily = data.get("daily", [])

        def pick_day(idx, prefix):
            if idx < len(daily):
                d = daily[idx]
                return {
                    f"weather_{prefix}_desc": d["weather"][0]["description"] if d.get("weather") else "?",
                    f"weather_{prefix}_max": round(d["temp"]["max"], 1) if "temp" in d else 0,
                    f"weather_{prefix}_min": round(d["temp"]["min"], 1) if "temp" in d else 0,
                    f"humidity_{prefix}": d.get("humidity", 0)
                }
            return {
                f"weather_{prefix}_desc": "?",
                f"weather_{prefix}_max": 0,
                f"weather_{prefix}_min": 0,
                f"humidity_{prefix}": 0
            }

        return {**pick_day(0, "today"), **pick_day(1, "tomorrow")}
    except Exception as e:
        logger.warning(f"OpenWeather API error: {e}")
        return {
            "weather_today_desc": "?",
            "weather_today_max": 0,
            "weather_today_min": 0,
            "humidity_today": 0,
            "weather_tomorrow_desc": "?",
            "weather_tomorrow_max": 0,
            "weather_tomorrow_min": 0,
            "humidity_tomorrow": 0
        }

# ================= AI ADVICE =================
def get_ai_advice(temp, hum, crop):
    return {
        "advice": "Ưu tiên Kali (K) | Cân bằng NPK | Bón phân hữu cơ | Tưới đủ nước, theo dõi thường xuyên | Độ ẩm ổn định cho rau muống | Quan sát cây trồng và điều chỉnh thực tế",
        "advice_nutrition": "Ưu tiên Kali (K) | Cân bằng NPK | Bón phân hữu cơ",
        "advice_care": "Tưới đủ nước, theo dõi thường xuyên | Độ ẩm ổn định cho rau muống",
        "advice_note": "Quan sát cây trồng và điều chỉnh thực tế"
    }

# ================= THINGSBOARD =================
def upload_to_thingsboard(data):
    url = f"{THINGSBOARD_URL}/{THINGSBOARD_TOKEN}/telemetry"
    try:
        r = requests.post(url, json=data, timeout=10)
        r.raise_for_status()
        logger.info("Telemetry pushed to ThingsBoard.")
    except Exception as e:
        logger.error(f"Failed to upload telemetry: {e}")

# ================= ROUTE ESP32 =================
@app.post("/esp32-data")
async def esp32_data(sensor: SensorData):
    content = sensor.dict()
    logger.info(f"ESP32 data: {content}")

    advice = get_ai_advice(content["temperature"], content["humidity"], content.get("crop", "Rau muống"))
    weather = get_weather_forecast()

    payload = {
        "temperature": content["temperature"],
        "humidity": content["humidity"],
        "crop": content.get("crop", "Rau muống"),
        "prediction": f"Nhiệt độ {content['temperature']}°C, độ ẩm {content['humidity']}%",
        **advice,
        **weather,
        "location": "An Phú, Hồ Chí Minh"
    }

    upload_to_thingsboard(payload)
    return {"status": "ok", "uploaded": payload}

# ================= BACKGROUND LOOP =================
def background_loop():
    while True:
        try:
            temperature = 30.1
            humidity = 69.2
            crop = "Rau muống"

            advice = get_ai_advice(temperature, humidity, crop)
            weather = get_weather_forecast()

            payload = {
                "temperature": temperature,
                "humidity": humidity,
                "crop": crop,
                "prediction": f"Nhiệt độ {temperature}°C, độ ẩm {humidity}%",
                **advice,
                **weather,
                "location": "An Phú, Hồ Chí Minh"
            }

            upload_to_thingsboard(payload)
        except Exception as e:
            logger.error(f"Background loop error: {e}")
        time.sleep(300)  # 5 phút

# ================= MAIN =================
if __name__ == "__main__":
    # Chạy background loop trong thread
    t = Thread(target=background_loop, daemon=True)
    t.start()

    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=5000)

import os
import logging
from datetime import datetime
import requests
import httpx
from fastapi import FastAPI
from apscheduler.schedulers.background import BackgroundScheduler

# ================== CONFIG ==================
TB_URL = os.getenv("TB_URL", "https://thingsboard.cloud/api/v1")
TB_TOKEN = os.getenv("TB_TOKEN", "your_tb_token_here")
OWM_API_KEY = os.getenv("OWM_API_KEY")
LAT = os.getenv("LAT", "10.8781")
LON = os.getenv("LON", "106.7594")
LOCATION_NAME = os.getenv("LOCATION_NAME", "An Phú, Hồ Chí Minh")
AI_MODEL = os.getenv("AI_MODEL", "gemini")  # gemini or openrouter
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

app = FastAPI(title="AgriBot Telemetry Service")
scheduler = BackgroundScheduler()

# ================== THINGSBOARD ==================
def push_telemetry(payload: dict):
    url = f"{TB_URL}/{TB_TOKEN}/telemetry"
    try:
        logging.info(f"Pushing telemetry: {payload}")
        r = requests.post(url, json=payload, timeout=10)
        logging.info(f"Response status: {r.status_code}, body: {r.text}")
    except Exception as e:
        logging.error(f"[ERROR] Failed to push telemetry: {e}")

# ================== OPENWEATHER ==================
def fetch_weather():
    url = f"https://api.openweathermap.org/data/2.5/onecall?lat={LAT}&lon={LON}&appid={OWM_API_KEY}&units=metric&exclude=minutely,daily,alerts"
    try:
        r = httpx.get(url, timeout=10)
        r.raise_for_status()
        return r.json()
    except httpx.HTTPStatusError as e:
        logging.error(f"[ERROR] Error fetching OpenWeather: {e.response.text}")
    except Exception as e:
        logging.error(f"[ERROR] Unexpected error fetching OpenWeather: {e}")
    return None

# ================== AI ADVICE ==================
def generate_weather_advice(current_temp, current_humidity, weather_today_desc, weather_tomorrow_desc):
    # Simple placeholder: bạn có thể thay bằng call Gemini/OpenRouter
    advice = (
        f"Ưu tiên Kali (K) | Cân bằng NPK | Bón phân hữu cơ | "
        f"Nhiệt độ trong ngưỡng an toàn | Độ ẩm ổn định cho rau | "
        f"Dự báo hôm nay: {weather_today_desc}, ngày mai: {weather_tomorrow_desc} | "
        f"Quan sát thực tế và điều chỉnh"
    )
    return advice

# ================== JOB ==================
def job():
    weather = fetch_weather()
    if not weather:
        logging.error("[ERROR] Skipping job due to weather fetch failure")
        return

    current = weather["current"]
    hourly = weather["hourly"][:7]  # lấy 7 giờ
    payload = {
        "temperature": current["temp"],
        "humidity": current["humidity"],
        "location": LOCATION_NAME,
        "prediction": f"Nhiệt độ {current['temp']}°C, độ ẩm {current['humidity']}%",
    }

    # Thêm hourly giống format last telemetry
    for i, h in enumerate(hourly):
        payload[f"hour_{i}"] = datetime.fromtimestamp(h["dt"]).strftime("%H:%M")
        payload[f"hour_{i}_temperature"] = h["temp"]
        payload[f"hour_{i}_temperature_corrected"] = round(h["temp"] - 5, 1)  # ví dụ correction
        payload[f"hour_{i}_humidity"] = h["humidity"]
        payload[f"hour_{i}_weather_desc"] = h["weather"][0]["description"]

    # Thêm weather_today/tomorrow
    payload["weather_today_desc"] = hourly[0]["weather"][0]["description"]
    payload["weather_today_max"] = max(h["temp"] for h in hourly)
    payload["weather_today_min"] = min(h["temp"] for h in hourly)
    payload["weather_tomorrow_desc"] = hourly[-1]["weather"][0]["description"]
    payload["weather_tomorrow_max"] = max(h["temp"] for h in hourly)
    payload["weather_tomorrow_min"] = min(h["temp"] for h in hourly)
    payload["advice"] = generate_weather_advice(
        current_temp=current["temp"],
        current_humidity=current["humidity"],
        weather_today_desc=payload["weather_today_desc"],
        weather_tomorrow_desc=payload["weather_tomorrow_desc"]
    )

    push_telemetry(payload)

# ================== STARTUP ==================
@app.on_event("startup")
def startup_event():
    logging.info("[INFO] Starting app...")

    # Push startup ping và dummy last telemetry để ThingsBoard có ngay last telemetry
    payload = {"startup_ping": datetime.utcnow().isoformat()}
    push_telemetry(payload)

    # Lên lịch job 5 phút 1 lần
    scheduler.add_job(job, "interval", minutes=5, id="weather_job", replace_existing=True)
    scheduler.start()

# ================== ROOT ==================
@app.get("/")
def read_root():
    return {"status": "AgriBot Telemetry Service running"}

# ================== MAIN ==================
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 10000)))

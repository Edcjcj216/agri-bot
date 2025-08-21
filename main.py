import os
import logging
import random
import requests
from datetime import datetime, timezone
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from apscheduler.schedulers.background import BackgroundScheduler
from zoneinfo import ZoneInfo

# --- Logging ---
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("agri-bot")

# --- Config chung từ ENV ---
THINGSBOARD_TOKEN = os.getenv("THINGSBOARD_TOKEN", "pk94asonfacs6mbeuutg")
TB_URL = f"https://thingsboard.cloud/api/v1/{THINGSBOARD_TOKEN}/telemetry"
TB_ATTR_URL = f"https://thingsboard.cloud/api/v1/{THINGSBOARD_TOKEN}/attributes"
LAT = os.getenv("LAT", "10.80609")
LON = os.getenv("LON", "106.75222")
CROP = os.getenv("CROP", "Rau muống")
OWM_API_KEY = os.getenv("OPENWEATHER_API_KEY")
LOCATION_NAME_OVERRIDE = os.getenv("LOCATION_NAME")

LOCAL_TZ = ZoneInfo("Asia/Ho_Chi_Minh")
_location_cache = None

# --- FastAPI ---
app = FastAPI()
scheduler = BackgroundScheduler()

# --- Hàm lấy địa chỉ từ lat/lon ---
def get_location_name():
    global _location_cache
    if LOCATION_NAME_OVERRIDE:
        return LOCATION_NAME_OVERRIDE
    if _location_cache:
        return _location_cache
    try:
        url = (
            f"https://nominatim.openstreetmap.org/reverse?"
            f"format=jsonv2&lat={LAT}&lon={LON}&accept-language=vi"
        )
        headers = {"User-Agent": "agri-bot/1.0"}
        r = requests.get(url, headers=headers, timeout=10)
        if r.status_code == 200:
            j = r.json()
            display = j.get("display_name")
            if display:
                # Rút gọn địa chỉ, lấy 3 phần đầu tiên
                parts = [p.strip() for p in display.split(",")]
                short = ", ".join(parts[:3]) if len(parts) >= 3 else display
                _location_cache = short
                return short
    except Exception as e:
        logger.warning(f"Lỗi reverse geocode: {e}")
    return f"{LAT},{LON}"

# --- Hàm push lên ThingsBoard ---
def send_to_thingsboard(payload: dict):
    try:
        logger.info(f"[TB ▶] {payload}")
        resp = requests.post(TB_URL, json=payload, timeout=10)
        if resp.status_code in (200, 201, 204):
            logger.info(f"[TB ◀] OK {resp.status_code}")
        else:
            logger.warning(f"[TB ◀] LỖI {resp.status_code}: {resp.text}")
    except Exception as e:
        logger.error(f"[TB] EXCEPTION: {e}")

# --- Telemetry mẫu (sensor) ---
def generate_sample_data():
    temperature = round(random.uniform(25, 35), 1)
    humidity = round(random.uniform(60, 80), 1)
    battery = random.randint(50, 100)
    return {
        "time_sent": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "temperature": temperature,
        "humidity": humidity,
        "battery": battery,
        "plant_type": CROP,
        "location_name": get_location_name(),
        "weather_now_desc": "Nhiều mây",
        "weather_now_temp": temperature,
        "weather_now_humidity": humidity,
        "advice": "Ưu tiên Kali (K) | Cân bằng NPK | Bón phân hữu cơ | Tưới đủ nước",
        "prediction": f"Nhiệt độ {temperature}°C, độ ẩm {humidity}%"
    }

# --- Weather fetch ---
def fetch_weather():
    try:
        if OWM_API_KEY:
            url = (f"https://api.openweathermap.org/data/2.5/weather"
                   f"?lat={LAT}&lon={LON}&units=metric&lang=vi&appid={OWM_API_KEY}")
            r = requests.get(url, timeout=10)
            r.raise_for_status()
            d = r.json()
            return {
                "time_sent": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "crop": CROP,
                "vi_tri": get_location_name(),
                "weather_temp": d["main"]["temp"],
                "weather_humidity": d["main"]["humidity"],
                "weather_desc": d["weather"][0]["description"]
            }
        else:
            # fallback dummy
            return {
                "time_sent": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "crop": CROP,
                "vi_tri": get_location_name(),
                "weather_temp": round(random.uniform(26, 34), 1),
                "weather_humidity": random.randint(50, 80),
                "weather_desc": "Trời quang (demo)"
            }
    except Exception as e:
        logger.error(f"Lỗi fetch weather: {e}")
        return None

# --- Jobs định kỳ ---
def job_send_all():
    logger.info("[JOB] Push dữ liệu cảm biến + weather")
    send_to_thingsboard(generate_sample_data())
    w = fetch_weather()
    if w:
        send_to_thingsboard(w)

scheduler.add_job(job_send_all, "interval", minutes=5)
scheduler.start()

# --- API ---
@app.get("/")
def root():
    return {"status": "ok", "message": "AgriBot server running (sensor + weather push every 5min)"}

@app.post("/telemetry")
async def receive_telemetry(req: Request):
    data = await req.json()
    data["time_sent"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    logger.info(f"[ESP32 ▶] {data}")
    send_to_thingsboard(data)
    return {"status": "OK"}

@app.post("/push")
def push_now():
    job_send_all()
    return {"status": "OK", "message": "Pushed telemetry + weather"}

@app.get("/last")
def last_telemetry():
    """Lấy last telemetry từ ThingsBoard (qua REST API)"""
    try:
        url = f"https://thingsboard.cloud/api/v1/{THINGSBOARD_TOKEN}/attributes?sharedKeys=none"
        r = requests.get(url, timeout=10)
        if r.status_code == 200:
            return JSONResponse(content={"ok": True, "last": r.json()})
        else:
            return JSONResponse(content={"ok": False, "status": r.status_code, "body": r.text}, status_code=500)
    except Exception as e:
        return JSONResponse(content={"ok": False, "error": str(e)}, status_code=500)

# --- Run ---
if __name__ == "__main__":
    logger.info("[INIT] Gửi dữ liệu lần đầu...")
    job_send_all()
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=10000, reload=False)

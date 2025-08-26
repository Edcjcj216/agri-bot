# main.py
import os
import json
import logging
import requests
from fastapi import FastAPI, Response
from apscheduler.schedulers.background import BackgroundScheduler
from datetime import datetime
from typing import Optional

# ================== CONFIG ==================
TB_URL = "https://thingsboard.cloud/api/v1"
TB_TOKEN = os.getenv("TB_TOKEN")

if not TB_TOKEN:
    raise RuntimeError("⚠️ Missing TB_TOKEN in environment variables!")

# support both names you mentioned
WEATHER_KEY = os.getenv("WEATHER_KEY") or os.getenv("WEATHER_API_KEY")
if not WEATHER_KEY:
    raise RuntimeError("⚠️ Missing WEATHER_KEY / WEATHER_API_KEY in environment variables!")

LOCATION = os.getenv("LOCATION", "Ho Chi Minh,VN")
SCHEDULE_MINUTES = int(os.getenv("SCHEDULE_MINUTES", "5"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("main")
logger.info(f"✅ Startup with TB_TOKEN (first 4 chars): {TB_TOKEN[:4]}****")

# ================== APP ==================
app = FastAPI()

# ================== WEATHER MAPPING ==================
weather_mapping = {
    "Sunny": "Nắng",
    "Clear": "Trời quang",
    "Partly cloudy": "Trời ít mây",
    "Cloudy": "Có mây",
    "Overcast": "Trời âm u",
    "Mist": "Sương mù nhẹ",
    "Patchy rain possible": "Có thể có mưa",
    "Light rain": "Mưa nhẹ",
    "Patchy light rain": "Mưa nhẹ",
    "Moderate rain": "Mưa vừa",
    "Heavy rain": "Mưa to",
    "Torrential rain shower": "Mưa rất to",
    "Thundery outbreaks possible": "Có thể có dông",
    "Patchy light rain with thunder": "Mưa nhẹ kèm dông",
    "Moderate or heavy rain with thunder": "Mưa to kèm dông",
    "Patchy rain nearby": "Có mưa cục bộ",
    "Fog": "Sương mù",
    "Haze": "Sương khói / Haze"
    # mở rộng khi cần
}

def translate_condition(cond: str) -> str:
    if not cond:
        return cond
    return weather_mapping.get(cond, cond)

# ================== GLOBALS ==================
session = requests.Session()
scheduler: Optional[BackgroundScheduler] = None
last_telemetry = None  # store last telemetry successfully pushed

# ================== FUNCTIONS ==================
def fetch_weather():
    """
    Lấy dữ liệu từ WeatherAPI (forecast.json) và trả về telemetry đã chuyển sang tiếng Việt.
    Giữ nguyên tên key giống như bạn đã dùng trên dashboard.
    """
    url = f"http://api.weatherapi.com/v1/forecast.json?key={WEATHER_KEY}&q={LOCATION}&days=2&aqi=no&alerts=no"
    try:
        r = session.get(url, timeout=12)
        r.raise_for_status()
        data = r.json()

        current = data.get("current", {})
        cond_current = current.get("condition", {}).get("text", "")

        telemetry = {
            "time": datetime.utcnow().isoformat() + "Z",
            "location": data.get("location", {}).get("name", LOCATION),
            "temperature": current.get("temp_c"),
            "humidity": current.get("humidity"),
            "weather_desc": translate_condition(cond_current),
            "crop": "Rau muống"
        }

        # build hours list (current first, then forecast hours)
        hours = []
        if current:
            hours.append({
                "time": current.get("last_updated"),
                "temp_c": current.get("temp_c"),
                "humidity": current.get("humidity"),
                "condition": {"text": cond_current}
            })
        forecast_days = data.get("forecast", {}).get("forecastday", [])
        for fd in forecast_days:
            # day-level may contain "hour" list
            for h in fd.get("hour", []):
                hours.append(h)

        # fill hour_0 .. hour_6 (same naming as telemetry you shared)
        for i in range(0, 7):
            if i < len(hours):
                h = hours[i]
                telemetry[f"hour_{i}_temperature"] = h.get("temp_c")
                telemetry[f"hour_{i}_humidity"] = h.get("humidity")
                telemetry[f"hour_{i}_weather_desc"] = translate_condition(h.get("condition", {}).get("text", ""))
            else:
                telemetry[f"hour_{i}_temperature"] = None
                telemetry[f"hour_{i}_humidity"] = None
                telemetry[f"hour_{i}_weather_desc"] = None

        # today's and tomorrow's summary
        if len(forecast_days) >= 1:
            today = forecast_days[0].get("day", {})
            telemetry["weather_today_desc"] = translate_condition(today.get("condition", {}).get("text", ""))
            telemetry["weather_today_max"] = today.get("maxtemp_c")
            telemetry["weather_today_min"] = today.get("mintemp_c")
        if len(forecast_days) >= 2:
            tom = forecast_days[1].get("day", {})
            telemetry["weather_tomorrow_desc"] = translate_condition(tom.get("condition", {}).get("text", ""))
            telemetry["weather_tomorrow_max"] = tom.get("maxtemp_c")
            telemetry["weather_tomorrow_min"] = tom.get("mintemp_c")

        # keep room to add other custom keys (prediction, advice...) elsewhere in your app
        return telemetry

    except Exception as e:
        logger.error(f"[ERROR] Fetch WeatherAPI (forecast): {e}")
        return None

def push_thingsboard(payload: dict, max_retries: int = 3):
    url = f"{TB_URL}/{TB_TOKEN}/telemetry"
    for attempt in range(1, max_retries + 1):
        try:
            r = session.post(url, json=payload, timeout=10)
            r.raise_for_status()
            logger.info(f"✅ Pushed telemetry (attempt {attempt}): keys={list(payload.keys())}")
            return True
        except Exception as e:
            logger.warning(f"[WARN] Push attempt {attempt} failed: {e}")
    logger.error("[ERROR] All push attempts failed.")
    return False

def job():
    global last_telemetry
    telemetry = fetch_weather()
    if telemetry:
        ok = push_thingsboard(telemetry)
        if ok:
            last_telemetry = {"pushed_at": datetime.utcnow().isoformat() + "Z", "payload": telemetry}
    else:
        logger.warning("[WARN] job(): No telemetry fetched, skipping push.")

# ================== SCHEDULER / LIFECYCLE ==================
@app.on_event("startup")
def startup_event():
    global scheduler
    logger.info("🚀 Service startup event triggered.")
    # send a startup ping (non-blocking best-effort)
    try:
        push_thingsboard({"startup": True, "time": datetime.utcnow().isoformat() + "Z"})
    except Exception:
        pass
    # run job immediately once
    job()
    # create and start scheduler if not started
    if scheduler is None:
        scheduler = BackgroundScheduler()
        scheduler.add_job(job, "interval", minutes=SCHEDULE_MINUTES, id="weather_job", replace_existing=True)
        scheduler.start()
        logger.info(f"⏱ Scheduler started: job every {SCHEDULE_MINUTES} minute(s).")

@app.on_event("shutdown")
def shutdown_event():
    global scheduler
    if scheduler:
        logger.info("🛑 Shutting down scheduler...")
        scheduler.shutdown(wait=False)
        logger.info("🛑 Scheduler stopped.")

# ================== ENDPOINTS ==================
@app.get("/health")
async def health():
    return {"status": "ok", "time": datetime.utcnow().isoformat() + "Z"}

@app.get("/last-push")
async def last_push():
    """Return last telemetry that was succesfully pushed. If none, fetch current (not pushed)."""
    global last_telemetry
    if last_telemetry:
        return last_telemetry
    telemetry = fetch_weather()
    return {"pushed_at": None, "payload": telemetry}

@app.get("/mapping")
async def get_mapping():
    """Return the weather mapping (EN -> VN) as JSON."""
    return weather_mapping

@app.get("/mapping.csv")
async def get_mapping_csv():
    """Return mapping as CSV download."""
    rows = ["english,vietnamese"]
    for k, v in weather_mapping.items():
        esc_k = k.replace('"', '""')
        esc_v = v.replace('"', '""')
        rows.append(f'"{esc_k}","{esc_v}"')
    csv_data = "\n".join(rows)
    return Response(content=csv_data, media_type="text/csv", headers={
        "Content-Disposition": "attachment; filename=weather_mapping.csv"
    })

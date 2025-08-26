# main.py
"""
Service for ThingsBoard telemetry:
- Translate weather descriptions to Vietnamese (keep original EN in *_en fields).
- Merge incoming ESP32 data (POST /esp32-data) with weather telemetry.
- Periodically fetch weather (WeatherAPI) and push merged telemetry to ThingsBoard.
- Expose endpoints: /health, /last-push, /push-now, /esp32-data (POST), /translate-telemetry (POST), /mapping, /mapping.csv
Env required:
- TB_TOKEN (ThingsBoard device token)
- WEATHER_KEY or WEATHER_API_KEY (WeatherAPI key)
Optional:
- LOCATION (default "Ho Chi Minh,VN")
- SCHEDULE_MINUTES (default 5)
"""
import os
import json
import logging
import requests
from fastapi import FastAPI, Request, Response, HTTPException
from apscheduler.schedulers.background import BackgroundScheduler
from datetime import datetime
from typing import Optional, Dict, Any
import threading

# ---------------- CONFIG ----------------
TB_URL = "https://thingsboard.cloud/api/v1"
TB_TOKEN = os.getenv("TB_TOKEN")
WEATHER_KEY = os.getenv("WEATHER_KEY") or os.getenv("WEATHER_API_KEY")
LOCATION = os.getenv("LOCATION", "Ho Chi Minh,VN")
SCHEDULE_MINUTES = int(os.getenv("SCHEDULE_MINUTES", "5"))

if not TB_TOKEN:
    raise RuntimeError("⚠️ Missing TB_TOKEN in environment variables!")
if not WEATHER_KEY:
    raise RuntimeError("⚠️ Missing WEATHER_KEY / WEATHER_API_KEY in environment variables!")

# ---------------- LOGGING ----------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("main")
logger.info(f"✅ Startup (TB_TOKEN starts {TB_TOKEN[:4]}****)")

# ---------------- APP ----------------
app = FastAPI()

# ---------------- GLOBALS ----------------
session = requests.Session()
scheduler: Optional[BackgroundScheduler] = None

last_telemetry: Optional[Dict[str, Any]] = None
_latest_esp_data: Optional[Dict[str, Any]] = None
_esp_lock = threading.Lock()

# ---------------- WEATHER MAPPING ----------------
weather_mapping = {
    "Sunny": "Nắng",
    "Clear": "Trời quang",
    "Partly cloudy": "Có mưa cục bộ",
    "Partly Cloudy": "Có mưa cục bộ",
    "Mostly Cloudy": "Trời nhiều mây",
    "Cloudy": "Có mây",
    "Overcast": "Trời âm u",
    "Mist": "Sương mù nhẹ",
    "Fog": "Sương mù",
    "Haze": "Sương khói",
    "Patchy rain possible": "Có thể có mưa",
    "Patchy rain nearby": "Có mưa cục bộ",
    "Patchy light rain": "Mưa nhẹ",
    "Light rain": "Mưa nhẹ",
    "Moderate rain": "Mưa vừa",
    "Heavy rain": "Mưa to",
    "Torrential rain shower": "Mưa rất to",
    "Thundery outbreaks possible": "Có thể có dông",
    "Patchy light rain with thunder": "Mưa nhẹ kèm dông",
    "Moderate or heavy rain with thunder": "Mưa to kèm dông",
    "Light drizzle": "Mưa phùn",
    # thêm khi cần
}

def translate_condition(cond: Optional[str]) -> str:
    if cond is None:
        return None
    s = str(cond).strip()
    if not s:
        return s
    return weather_mapping.get(s, s)

# ---------------- HELPERS ----------------
def translate_telemetry_fields(payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    - For keys: weather_desc, weather_today_desc, weather_tomorrow_desc, weather_yesterday_desc
      and all hour_{i}_weather_desc, create *_en (store original) and overwrite original with VN translation.
    - Keep all other keys unchanged.
    """
    out = dict(payload)  # shallow copy
    # list of top-level keys to translate
    candidates = ["weather_desc", "weather_today_desc", "weather_tomorrow_desc", "weather_yesterday_desc"]
    for key in candidates:
        if key in payload:
            orig = payload.get(key)
            out[f"{key}_en"] = orig
            out[key] = translate_condition(orig)

    # translate hour_i_weather_desc (i from 0..24 safe)
    for i in range(0, 25):
        k = f"hour_{i}_weather_desc"
        if k in payload:
            orig = payload.get(k)
            out[f"{k}_en"] = orig
            out[k] = translate_condition(orig)

    # Also translate any "weather_..._desc" generically
    for key, val in list(payload.items()):
        if key.endswith("_weather_desc") and key not in out:
            # handle any other patterns safe
            orig = val
            out[f"{key}_en"] = orig
            out[key] = translate_condition(orig)

    return out

def merge_with_esp(base: Dict[str, Any], esp: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Merge esp dict into base (ESP overrides base where keys collide).
    Return new dict (do not mutate inputs).
    """
    out = dict(base)
    if not esp:
        return out
    # prefer esp values when present (even if null?), only override if key present in esp
    for k, v in esp.items():
        out[k] = v
    return out

def push_thingsboard(payload: Dict[str, Any], max_retries: int = 3) -> bool:
    url = f"{TB_URL}/{TB_TOKEN}/telemetry"
    for attempt in range(1, max_retries+1):
        try:
            r = session.post(url, json=payload, timeout=10)
            r.raise_for_status()
            logger.info(f"Pushed telemetry (attempt {attempt}) keys={list(payload.keys())[:10]}")
            return True
        except Exception as e:
            logger.warning(f"Push attempt {attempt} failed: {e}")
    logger.error("All push attempts failed")
    return False

# ---------------- WEATHER FETCH ----------------
def fetch_weather_forecast() -> Optional[Dict[str, Any]]:
    """
    Fetch forecast.json from WeatherAPI and return a telemetry-like dict
    with keys consistent with your existing telemetry (hour_0_temperature, hour_0_humidity, hour_0_weather_desc, etc.)
    Also includes weather_today_desc, weather_tomorrow_desc, rain_1h_mm (current.precip_mm), etc.
    """
    url = f"http://api.weatherapi.com/v1/forecast.json?key={WEATHER_KEY}&q={LOCATION}&days=2&aqi=no&alerts=no"
    try:
        r = session.get(url, timeout=12)
        r.raise_for_status()
        data = r.json()

        current = data.get("current", {}) or {}
        loc = data.get("location", {}) or {}
        cond_current = current.get("condition", {}).get("text", "")

        telemetry = {}
        telemetry["time"] = datetime.utcnow().isoformat()
        telemetry["location"] = f"{loc.get('name', LOCATION)}"
        telemetry["temperature"] = current.get("temp_c")
        telemetry["humidity"] = current.get("humidity")
        telemetry["weather_desc_en"] = cond_current
        telemetry["weather_desc"] = translate_condition(cond_current)
        telemetry["rain_1h_mm"] = current.get("precip_mm")
        telemetry["wind_kph"] = current.get("wind_kph")
        telemetry["wind_gust_kph"] = current.get("gust_kph")
        telemetry["pressure_mb"] = current.get("pressure_mb")
        telemetry["uv_index"] = current.get("uv")
        telemetry["visibility_km"] = current.get("vis_km")
        telemetry["forecast_generated_at"] = current.get("last_updated") or datetime.utcnow().isoformat()

        # build hours: current then forecast hour lists
        hours = []
        # current as hour 0
        hours.append({
            "time": current.get("last_updated"),
            "temp_c": current.get("temp_c"),
            "humidity": current.get("humidity"),
            "condition_text": cond_current
        })
        for fd in data.get("forecast", {}).get("forecastday", []):
            for h in fd.get("hour", []):
                hours.append({
                    "time": h.get("time"),
                    "temp_c": h.get("temp_c"),
                    "humidity": h.get("humidity"),
                    "condition_text": h.get("condition", {}).get("text")
                })

        # populate hour_0 .. hour_6 (or more if you want)
        for i in range(0, 7):
            if i < len(hours):
                h = hours[i]
                telemetry[f"hour_{i}_temperature"] = h.get("temp_c")
                telemetry[f"hour_{i}_humidity"] = h.get("humidity")
                telemetry[f"hour_{i}_weather_desc_en"] = h.get("condition_text")
                telemetry[f"hour_{i}_weather_desc"] = translate_condition(h.get("condition_text"))
            else:
                telemetry[f"hour_{i}_temperature"] = None
                telemetry[f"hour_{i}_humidity"] = None
                telemetry[f"hour_{i}_weather_desc_en"] = None
                telemetry[f"hour_{i}_weather_desc"] = None

        # day summaries
        if data.get("forecast", {}).get("forecastday"):
            today = data["forecast"]["forecastday"][0].get("day", {})
            telemetry["weather_today_desc_en"] = today.get("condition", {}).get("text")
            telemetry["weather_today_desc"] = translate_condition(telemetry["weather_today_desc_en"])
            telemetry["weather_today_max"] = today.get("maxtemp_c")
            telemetry["weather_today_min"] = today.get("mintemp_c")
            # if tomorrow exists
            if len(data["forecast"]["forecastday"]) > 1:
                tom = data["forecast"]["forecastday"][1].get("day", {})
                telemetry["weather_tomorrow_desc_en"] = tom.get("condition", {}).get("text")
                telemetry["weather_tomorrow_desc"] = translate_condition(telemetry["weather_tomorrow_desc_en"])
                telemetry["weather_tomorrow_max"] = tom.get("maxtemp_c")
                telemetry["weather_tomorrow_min"] = tom.get("mintemp_c")

        return telemetry

    except Exception as e:
        logger.error(f"Fetch weather failed: {e}")
        return None

# ---------------- JOB ----------------
def job_push():
    global last_telemetry
    weather = fetch_weather_forecast()
    if not weather:
        logger.warning("No weather, skipping push")
        return
    # apply translation to all weather_desc keys (function already writes *_en and VN)
    # merge with current _latest_esp_data (ESP overrides)
    with _esp_lock:
        esp = dict(_latest_esp_data) if _latest_esp_data else None
    merged = merge_with_esp(weather, esp)
    # ensure we add advice/crop fields if present in esp or existing last telemetry
    # push
    ok = push_thingsboard(merged)
    if ok:
        last_telemetry = {"pushed_at": datetime.utcnow().isoformat(), "payload": merged}
    else:
        logger.warning("Push failed for merged telemetry")

# ---------------- LIFECYCLE ----------------
@app.on_event("startup")
def startup_event():
    global scheduler
    logger.info("Service starting up... performing initial job and starting scheduler.")
    # initial job
    try:
        job_push()
    except Exception as e:
        logger.warning(f"Initial job error: {e}")
    # scheduler
    if scheduler is None:
        scheduler = BackgroundScheduler()
        scheduler.add_job(job_push, "interval", minutes=SCHEDULE_MINUTES, id="job_push", replace_existing=True)
        scheduler.start()
        logger.info(f"Scheduler started every {SCHEDULE_MINUTES} minutes.")

@app.on_event("shutdown")
def shutdown_event():
    global scheduler
    if scheduler:
        scheduler.shutdown(wait=False)
        logger.info("Scheduler shut down.")

# ---------------- ENDPOINTS ----------------
@app.get("/health")
async def health():
    return {"status": "ok", "time": datetime.utcnow().isoformat()}

@app.get("/last-push")
async def get_last_push():
    if last_telemetry:
        return last_telemetry
    return {"pushed_at": None, "payload": None}

@app.post("/esp32-data")
async def esp32_data(req: Request):
    """
    ESP32 posts JSON telemetry here. Example: {"temperature":26.8,"humidity":42,"light_lux":0,"soil_moisture":0}
    We store latest ESP data and it will be merged on next push (or push-now).
    """
    global _latest_esp_data
    try:
        body = await req.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")
    with _esp_lock:
        _latest_esp_data = dict(body)
        _latest_esp_data["_received_at"] = datetime.utcnow().isoformat()
    logger.info("Received ESP32 data keys=%s", list(body.keys()))
    return {"status": "ok", "stored_keys": list(body.keys())}

@app.post("/translate-telemetry")
async def translate_telemetry(req: Request):
    """
    Accept a telemetry JSON (dict of key->value) and return a new dict with translations applied:
    - Adds *_en fields for original English weather descriptions.
    - Overwrites weather_desc fields with Vietnamese translations.
    Use this to process the K/V you pasted.
    """
    try:
        body = await req.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Expected JSON object/dict")
    translated = translate_telemetry_fields(body)
    return translated

@app.post("/push-now")
async def push_now():
    """
    Trigger immediate fetch/merge/push (manual).
    """
    try:
        job_push()
        return {"status": "ok", "pushed_at": last_telemetry.get("pushed_at") if last_telemetry else None}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/mapping")
async def get_mapping():
    return weather_mapping

@app.get("/mapping.csv")
async def get_mapping_csv():
    rows = ["english,vietnamese"]
    for k, v in weather_mapping.items():
        esc_k = k.replace('"', '""')
        esc_v = v.replace('"', '""')
        rows.append(f'"{esc_k}","{esc_v}"')
    csv_data = "\n".join(rows)
    return Response(content=csv_data, media_type="text/csv", headers={
        "Content-Disposition": "attachment; filename=weather_mapping.csv"
    })

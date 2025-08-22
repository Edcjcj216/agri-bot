import os
import time
import json
import math
import random
import logging
import requests
import asyncio
from fastapi import FastAPI
from pydantic import BaseModel
from datetime import datetime, timedelta
from collections import deque

# Try to use zoneinfo when available for correct local times
try:
    from zoneinfo import ZoneInfo
except Exception:
    ZoneInfo = None

# ================== CONFIG ==================
TB_DEMO_TOKEN = os.getenv("TB_DEMO_TOKEN", "sgkxcrqntuki8gu1oj8u")
TB_DEVICE_URL = f"https://thingsboard.cloud/api/v1/{TB_DEMO_TOKEN}/telemetry"

LAT = float(os.getenv("LAT", "10.79"))
LON = float(os.getenv("LON", "106.70"))
AUTO_LOOP_INTERVAL = int(os.getenv("AUTO_LOOP_INTERVAL", 300))  # seconds
WEATHER_CACHE_SECONDS = int(os.getenv("WEATHER_CACHE_SECONDS", 15 * 60))  # default 15 minutes
TIMEZONE = os.getenv("TZ", "Asia/Ho_Chi_Minh")

# Bias correction settings
MAX_HISTORY = int(os.getenv("BIAS_MAX_HISTORY", 48))  # number of samples to keep
bias_history = deque(maxlen=MAX_HISTORY)

# ================== LOGGING ==================
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# ================== FASTAPI ==================
app = FastAPI()

class SensorData(BaseModel):
    temperature: float
    humidity: float
    battery: float | None = None

# ================== WEATHER ==================
WEATHER_CODE_MAP = {
    0: "Trời quang",
    1: "Trời quang nhẹ",
    2: "Có mây",
    3: "Nhiều mây",
    45: "Sương mù",
    48: "Sương mù đóng băng",
    51: "Mưa phùn nhẹ",
    53: "Mưa phùn vừa",
    55: "Mưa phùn dày",
    56: "Mưa phùn lạnh",
    57: "Mưa phùn lạnh dày",
    61: "Mưa nhẹ",
    63: "Mưa vừa",
    65: "Mưa to",
    66: "Mưa lạnh nhẹ",
    67: "Mưa lạnh to",
    71: "Tuyết nhẹ",
    73: "Tuyết vừa",
    75: "Tuyết dày",
    77: "Mưa tuyết/Trận băng",
    80: "Mưa rào nhẹ",
    81: "Mưa rào vừa",
    82: "Mưa rào mạnh",
    85: "Tuyết rơi nhẹ",
    86: "Tuyết rơi mạnh",
    95: "Giông nhẹ hoặc vừa",
    96: "Giông kèm mưa đá nhẹ",
    99: "Giông kèm mưa đá mạnh"
}

weather_cache = {"ts": 0, "data": {}}


def _now_local():
    """Return timezone-aware now in the configured TIMEZONE if possible."""
    if ZoneInfo is not None:
        try:
            return datetime.now(ZoneInfo(TIMEZONE))
        except Exception:
            return datetime.now()
    return datetime.now()


def _mean(lst):
    return round(sum(lst) / len(lst), 1) if lst else None


def _find_hour_index(hour_times, target_hour_str):
    """Find the index of the target hour string in the API's hourly time array.
    If exact match not found, find the nearest by absolute time difference.
    """
    if not hour_times:
        return None
    if target_hour_str in hour_times:
        return hour_times.index(target_hour_str)

    # fallback: parse strings like 'YYYY-MM-DDTHH:00' and compute nearest
    try:
        parsed = [datetime.fromisoformat(t) for t in hour_times]
        target = datetime.fromisoformat(target_hour_str)
        diffs = [abs((p - target).total_seconds()) for p in parsed]
        return int(min(range(len(diffs)), key=lambda i: diffs[i]))
    except Exception:
        return 0


def get_weather_forecast():
    """Call Open-Meteo and return a structured, more detailed and annotated forecast.
    Uses caching for WEATHER_CACHE_SECONDS to reduce API calls.
    Adds aggregated humidity for yesterday/today/tomorrow to preserve telemetry keys.
    """
    now = _now_local()
    if time.time() - weather_cache["ts"] < WEATHER_CACHE_SECONDS:
        return weather_cache["data"]

    try:
        start_date = (now - timedelta(days=1)).strftime("%Y-%m-%d")
        end_date = (now + timedelta(days=2)).strftime("%Y-%m-%d")
        url = "https://api.open-meteo.com/v1/forecast"
        params = {
            "latitude": LAT,
            "longitude": LON,
            "daily": ",".join([
                "weathercode", "temperature_2m_max", "temperature_2m_min",
                "precipitation_sum", "windspeed_10m_max"
            ]),
            "hourly": ",".join([
                "time", "temperature_2m", "relativehumidity_2m", "weathercode",
                "precipitation", "windspeed_10m", "winddirection_10m", "precipitation_probability"
            ]),
            "timezone": TIMEZONE,
            "start_date": start_date,
            "end_date": end_date
        }
        r = requests.get(url, params=params, timeout=10)
        r.raise_for_status()
        data = r.json()

        daily = data.get("daily", {})
        hourly = data.get("hourly", {})

        # build daily summary (today, yesterday, tomorrow)
        def safe_get(arr, idx):
            try:
                return arr[idx]
            except Exception:
                return None

        # aggregated humidity for yesterday/today/tomorrow (use 24-value slices if available)
        hums = hourly.get("relativehumidity_2m", [])
        humidity_yesterday = round(sum(hums[0:24]) / len(hums[0:24]), 1) if len(hums) >= 24 else None
        humidity_today = round(sum(hums[24:48]) / len(hums[24:48]), 1) if len(hums) >= 48 else None
        humidity_tomorrow = round(sum(hums[48:72]) / len(hums[48:72]), 1) if len(hums) >= 72 else None

        # Determine indices for yesterday/today/tomorrow relative to daily.time if present
        weather_yesterday = {
            "date": None,
            "desc": None,
            "max": None,
            "min": None,
            "precipitation_sum": None,
            "windspeed_max": None
        }
        weather_today = weather_yesterday.copy()
        weather_tomorrow = weather_yesterday.copy()

        if "time" in daily and daily["time"]:
            # find today's index
            today_str = now.strftime("%Y-%m-%d")
            try:
                idx_today = daily["time"].index(today_str)
            except ValueError:
                idx_today = 1 if len(daily.get("time", [])) > 1 else 0

            # yesterday / tomorrow indices
            idx_yesterday = max(0, idx_today - 1)
            idx_tomorrow = min(len(daily.get("time", [])) - 1, idx_today + 1)

            for name, idx in [("yesterday", idx_yesterday), ("today", idx_today), ("tomorrow", idx_tomorrow)]:
                target = {
                    "date": safe_get(daily.get("time", []), idx),
                    "desc": WEATHER_CODE_MAP.get(safe_get(daily.get("weathercode", []), idx), "?"),
                    "max": safe_get(daily.get("temperature_2m_max", []), idx),
                    "min": safe_get(daily.get("temperature_2m_min", []), idx),
                    "precipitation_sum": safe_get(daily.get("precipitation_sum", []), idx),
                    "windspeed_max": safe_get(daily.get("windspeed_10m_max", []), idx)
                }
                if name == "yesterday":
                    weather_yesterday.update(target)
                elif name == "today":
                    weather_today.update(target)
                else:
                    weather_tomorrow.update(target)

        # hourly: find current hour index in hourly['time'] and take next 6 hours
        hour_times = hourly.get("time", [])
        current_hour_str = now.replace(minute=0, second=0, microsecond=0).strftime("%Y-%m-%dT%H:00")
        idx = _find_hour_index(hour_times, current_hour_str) or 0

        next_hours = []
        for offset in range(0, 6):
            i = idx + offset
            if i >= len(hour_times):
                break
            h = {
                "time": hour_times[i],
                "temperature": safe_get(hourly.get("temperature_2m", []), i),
                "humidity": safe_get(hourly.get("relativehumidity_2m", []), i),
                "weather_code": safe_get(hourly.get("weathercode", []), i),
                "weather_desc": WEATHER_CODE_MAP.get(safe_get(hourly.get("weathercode", []), i), "?"),
                "precipitation": safe_get(hourly.get("precipitation", []), i),
                "precip_probability": safe_get(hourly.get("precipitation_probability", []), i),
                "windspeed": safe_get(hourly.get("windspeed_10m", []), i),
                "winddir": safe_get(hourly.get("winddirection_10m", []), i)
            }
            next_hours.append(h)

        result = {
            "meta": {
                "latitude": LAT,
                "longitude": LON,
                "tz": TIMEZONE,
                "fetched_at": now.isoformat()
            },
            "yesterday": weather_yesterday,
            "today": weather_today,
            "tomorrow": weather_tomorrow,
            "next_hours": next_hours,
            # preserve humidity_* keys expected in telemetry
            "humidity_yesterday": humidity_yesterday,
            "humidity_today": humidity_today,
            "humidity_tomorrow": humidity_tomorrow
        }

        weather_cache["data"] = result
        weather_cache["ts"] = time.time()
        return result

    except Exception as e:
        logger.warning(f"Weather API error: {e}")
        return {"meta": {}, "yesterday": {}, "today": {}, "tomorrow": {}, "next_hours": []}

# ================== BIAS CORRECTION ==================

def update_bias_and_correct(next_hours, observed_temp):
    """Update bias history with (api_now, observed_temp) and return bias + corrected next_hours.

    - Stores recent pairs in a deque (bias_history).
    - Computes bias = mean(observed - api) and applies it to each hourly temperature
      producing 'temperature_corrected'.
    - Returns (bias, next_hours_modified).
    """
    global bias_history
    if not next_hours:
        return 0.0, next_hours

    api_now = next_hours[0].get("temperature")
    if api_now is not None and observed_temp is not None:
        try:
            bias_history.append((api_now, observed_temp))
        except Exception:
            pass

    # compute bias = mean(observed - api)
    diffs = [obs - api for api, obs in bias_history if api is not None and obs is not None]
    bias = round(sum(diffs) / len(diffs), 1) if diffs else 0.0

    for h in next_hours:
        if h.get("temperature") is not None:
            h["temperature_corrected"] = round(h["temperature"] + bias, 1)
        else:
            h["temperature_corrected"] = None

    return bias, next_hours

# ================== AI HELPER ==================

def get_advice(temp, humi, upcoming_weather=None):
    """Return practical advice strings. If upcoming_weather (list of hourly dicts) is
    provided, include precipitation / wind considerations.
    """
    nutrition = ["Ưu tiên Kali (K)", "Cân bằng NPK", "Bón phân hữu cơ"]
    care = []

    # Temperature-based advice
    if temp is None:
        care.append("Thiếu dữ liệu nhiệt độ")
    else:
        if temp >= 35:
            care.append("Tránh nắng gắt; che phủ, tưới vào sáng sớm/chiều mát")
        elif temp >= 30:
            care.append("Tưới đủ nước; kiểm tra stress do nóng")
        elif temp <= 10:
            care.append("Nhiệt độ rất thấp: giữ ấm, tránh sương muối")
        elif temp <= 15:
            care.append("Giữ ấm vào ban đêm")
        else:
            care.append("Nhiệt độ trong ngưỡng an toàn")

    # Humidity-based advice
    if humi is None:
        care.append("Thiếu dữ liệu độ ẩm")
    else:
        if humi <= 40:
            care.append("Độ ẩm thấp: tăng tưới, che gió nếu cần")
        elif humi <= 60:
            care.append("Độ ẩm hơi thấp: theo dõi và tưới khi cần")
        elif humi >= 85:
            care.append("Độ ẩm cao: tránh úng; kiểm tra hệ thống thoát nước")
        else:
            care.append("Độ ẩm ổn định cho rau")

    # Upcoming weather: check for rain or strong wind in next_hours
    notes = []
    if upcoming_weather:
        rain_expected = any((h.get("precipitation", 0) or 0) > 0.1 or (h.get("precip_probability") or 0) >= 40 for h in upcoming_weather)
        strong_wind = any((h.get("windspeed") or 0) >= 8 for h in upcoming_weather)
        if rain_expected:
            notes.append("Dự báo mưa sắp tới: giảm bón, tránh tưới trước cơn mưa")
        if strong_wind:
            notes.append("Gió mạnh dự báo: chằng buộc, bảo vệ cây non")

    advice_care = " | ".join(care + notes)
    return {
        "advice": " | ".join(nutrition + [advice_care, "Quan sát thực tế và điều chỉnh"]),
        "advice_nutrition": " | ".join(nutrition),
        "advice_care": advice_care,
        "advice_note": "Quan sát thực tế và điều chỉnh",
        "prediction": f"Nhiệt độ {temp}°C, độ ẩm {humi}%"
    }

# ================== THINGSBOARD ==================

def send_to_thingsboard(data: dict):
    try:
        logger.info(f"TB ▶ {json.dumps(data, ensure_ascii=False)}")
        r = requests.post(TB_DEVICE_URL, json=data, timeout=10)
        logger.info(f"TB ◀ {r.status_code}")
    except Exception as e:
        logger.error(f"ThingsBoard push error: {e}")

# ================== HELPERS ==================

def merge_weather_and_hours(existing_data=None):
    """Merge detailed weather forecast and compute localized hour labels.
    Returns a single flattened dict suitable for pushing to ThingsBoard.

    This function will preserve the original telemetry key names exactly as seen
    in the ThingsBoard dashboard (advice, advice_care, hour_0, hour_0_temperature, ... etc.)
    and will add corrected temperature fields as additional keys without renaming existing keys.
    """
    if existing_data is None:
        existing_data = {}
    weather = get_weather_forecast()

    now = _now_local()

    flattened = {**existing_data}

    # Ensure top-level daily keys match original telemetry names
    # Copy over daily summary fields
    flattened["weather_today_desc"] = weather.get("today", {}).get("desc")
    flattened["weather_today_max"] = weather.get("today", {}).get("max")
    flattened["weather_today_min"] = weather.get("today", {}).get("min")
    flattened["weather_tomorrow_desc"] = weather.get("tomorrow", {}).get("desc")
    flattened["weather_tomorrow_max"] = weather.get("tomorrow", {}).get("max")
    flattened["weather_tomorrow_min"] = weather.get("tomorrow", {}).get("min")
    flattened["weather_yesterday_desc"] = weather.get("yesterday", {}).get("desc")
    flattened["weather_yesterday_max"] = weather.get("yesterday", {}).get("max")
    flattened["weather_yesterday_min"] = weather.get("yesterday", {}).get("min")

    # Preserve aggregated humidity keys if available from forecast
    if weather.get("humidity_today") is not None:
        flattened["humidity_today"] = weather.get("humidity_today")
    else:
        # fallback to any value in next_hours
        hlist = [h.get("humidity") for h in weather.get("next_hours", []) if h.get("humidity") is not None]
        flattened["humidity_today"] = round(sum(hlist)/len(hlist),1) if hlist else None

    if weather.get("humidity_tomorrow") is not None:
        flattened["humidity_tomorrow"] = weather.get("humidity_tomorrow")
    else:
        flattened["humidity_tomorrow"] = None

    if weather.get("humidity_yesterday") is not None:
        flattened["humidity_yesterday"] = weather.get("humidity_yesterday")
    else:
        flattened["humidity_yesterday"] = None

    # Build per-hour telemetry exactly matching existing key names
    for idx in range(0, 7):
        h = None
        if idx < len(weather.get("next_hours", [])):
            h = weather["next_hours"][idx]

        # time label (hour_0, hour_1, ...)
        time_label = None
        if h and h.get("time"):
            try:
                t = datetime.fromisoformat(h.get("time"))
                time_label = t.strftime("%H:%M")
            except Exception:
                time_label = h.get("time")
        flattened[f"hour_{idx}"] = time_label

        # temperature, humidity, weather desc
        temp = h.get("temperature") if h else None
        hum = h.get("humidity") if h else None
        desc = h.get("weather_desc") if h else None

        # keep original keys
        flattened[f"hour_{idx}_temperature"] = temp
        flattened[f"hour_{idx}_humidity"] = hum
        flattened[f"hour_{idx}_weather_desc"] = desc

        # also add corrected temperature if present (new additional key)
        corrected = h.get("temperature_corrected") if h else None
        if corrected is not None:
            flattened[f"hour_{idx}_temperature_corrected"] = corrected

    # Keep common top-level telemetry keys present in original dataset
    # If they are not provided by existing_data, try to populate them
    # temperature & humidity (current observed)
    if "temperature" not in flattened:
        flattened["temperature"] = existing_data.get("temperature")
    if "humidity" not in flattened:
        flattened["humidity"] = existing_data.get("humidity")

    # prediction string
    if "prediction" not in flattened:
        flattened["prediction"] = existing_data.get("prediction")

    # location and crop if present
    if "location" not in flattened:
        flattened["location"] = existing_data.get("location")
    if "crop" not in flattened:
        flattened["crop"] = existing_data.get("crop")

    return flattened

# ================== ROUTES ==================
@app.get("/")
def root():
    return {"status": "running", "demo_token": TB_DEMO_TOKEN[:4] + "***"}

@app.get("/weather")
def weather_endpoint():
    """Return the processed weather forecast (useful for testing)."""
    return get_weather_forecast()

@app.post("/esp32-data")
def receive_data(data: SensorData):
    logger.info(f"ESP32 ▶ {data.dict()}")
    # get next_hours to include in advice and bias correction
    weather = get_weather_forecast()
    next_hours = weather.get("next_hours", [])

    # apply bias correction: update history and get corrected temperatures
    bias, corrected_next_hours = update_bias_and_correct(next_hours, data.temperature)

    advice_data = get_advice(data.temperature, data.humidity, upcoming_weather=corrected_next_hours)

    merged = {
        **data.dict(),
        **advice_data,
        "location": "An Phú, Hồ Chí Minh",
        "crop": "Rau muống",
        "forecast_bias": bias,
        "forecast_history_len": len(bias_history)
    }
    # attach corrected next_hours into weather cache so merge picks them up
    # (we'll cheat by replacing weather['next_hours'] for merging)
    weather["next_hours"] = corrected_next_hours
    merged = merge_weather_and_hours(existing_data=merged)
    send_to_thingsboard(merged)
    return {"received": data.dict(), "pushed": merged}

# ================== AUTO LOOP (more realistic simulator) ==================
async def auto_loop():
    logger.info("Auto-loop simulator started")
    battery = 4.2  # volts, example lithium
    tick = 0
    while True:
        try:
            now = _now_local()
            # create diurnal realistic temperature using sine wave + noise
            hour = now.hour + now.minute / 60.0
            # base temperature (tuned for HCMC average) - adjust as needed
            base = 27.0
            amplitude = 6.0
            temp = base + amplitude * math.sin((hour - 14) / 24.0 * 2 * math.pi) + random.uniform(-0.7, 0.7)
            # humidity roughly inverse to temperature, plus noise
            humi = max(20.0, min(95.0, 75 - (temp - base) * 3 + random.uniform(-5, 5)))

            # battery slowly discharges
            battery = max(3.3, battery - random.uniform(0.0005, 0.0025))

            sample = {"temperature": round(temp, 1), "humidity": round(humi, 1), "battery": round(battery, 3)}

            # advice using upcoming hours
            weather = get_weather_forecast()

            # update bias using simulated observed temperature
            bias, corrected_next_hours = update_bias_and_correct(weather.get("next_hours", []), sample["temperature"])

            advice_data = get_advice(sample["temperature"], sample["humidity"], upcoming_weather=corrected_next_hours)

            merged = {
                **sample,
                **advice_data,
                "location": "An Phú, Hồ Chí Minh",
                "crop": "Rau muống",
                "forecast_bias": bias,
                "forecast_history_len": len(bias_history)
            }
            # ensure merge uses corrected hours
            weather["next_hours"] = corrected_next_hours
            merged = merge_weather_and_hours(existing_data=merged)
            send_to_thingsboard(merged)
            tick += 1
        except Exception as e:
            logger.error(f"AUTO loop error: {e}")
        await asyncio.sleep(AUTO_LOOP_INTERVAL)

@app.on_event("startup")
async def start_auto_loop():
    asyncio.create_task(auto_loop())

# ================== NOTES ==================
# - Run with: uvicorn fastapi_thingsboard_realistic:app --reload
# - Test endpoints: GET /weather  POST /esp32-data
# - Configure via environment variables: TB_DEMO_TOKEN, LAT, LON, AUTO_LOOP_INTERVAL, TZ, WEATHER_CACHE_SECONDS, BIAS_MAX_HISTORY

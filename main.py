import os
import time
import json
import math
import random
import logging
import requests
import asyncio
import sqlite3
from fastapi import FastAPI
from pydantic import BaseModel
from datetime import datetime, timedelta
from collections import deque
import re

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

# Extended hours to include in telemetry (default 12). Can set EXTENDED_HOURS in env.
EXTENDED_HOURS = int(os.getenv("EXTENDED_HOURS", 12))

# LLM / OpenRouter configuration
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
LLM_MODEL = os.getenv("LLM_MODEL", "google/gemini-2.5-pro")
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
# You can tune this if you want to require more history before LLM is meaningful
LLM_CALL_MIN_HISTORY = int(os.getenv("LLM_CALL_MIN_HISTORY", 1))

# Bias correction settings (kept in-memory and persisted in SQLite)
MAX_HISTORY = int(os.getenv("BIAS_MAX_HISTORY", 48))
bias_history = deque(maxlen=MAX_HISTORY)
BIAS_DB_FILE = os.getenv("BIAS_DB_FILE", "bias_history.db")

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
    0: "Nắng", 1: "Nắng nhẹ", 2: "Có mây", 3: "Nhiều mây",
    45: "Sương mù", 48: "Sương muối/đóng băng", 51: "Mưa phùn nhẹ", 53: "Mưa phùn vừa",
    55: "Mưa phùn dày", 56: "Mưa phùn lạnh", 57: "Mưa phùn lạnh dày", 61: "Mưa nhẹ",
    63: "Mưa vừa", 65: "Mưa to", 66: "Mưa lạnh nhẹ", 67: "Mưa lạnh to",
    71: "Tuyết nhẹ", 73: "Tuyết vừa", 75: "Tuyết dày", 77: "Mưa tuyết/Trận băng",
    80: "Mưa rào nhẹ", 81: "Mưa rào vừa", 82: "Mưa rào mạnh", 85: "Tuyết rơi nhẹ",
    86: "Tuyết rơi mạnh", 95: "Giông", 96: "Giông kèm mưa đá nhẹ", 99: "Giông kèm mưa đá mạnh"
}

weather_cache = {"ts": 0, "data": {}}

# ----------------- SQLite persistence for bias history -----------------

def init_db():
    """Create SQLite DB/table if not exists."""
    try:
        conn = sqlite3.connect(BIAS_DB_FILE)
        cur = conn.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS bias_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                api_temp REAL NOT NULL,
                observed_temp REAL NOT NULL,
                ts INTEGER NOT NULL
            )
            """
        )
        conn.commit()
    except Exception as e:
        logger.warning(f"Failed to init bias DB: {e}")
    finally:
        try:
            conn.close()
        except Exception:
            pass


def load_history_from_db():
    """Load most recent MAX_HISTORY rows into bias_history deque."""
    try:
        conn = sqlite3.connect(BIAS_DB_FILE)
        cur = conn.cursor()
        cur.execute("SELECT api_temp, observed_temp FROM bias_history ORDER BY id DESC LIMIT ?", (MAX_HISTORY,))
        rows = cur.fetchall()
        # rows returned newest-first; reverse to chronological
        rows.reverse()
        for api, obs in rows:
            bias_history.append((float(api), float(obs)))
        logger.info(f"Loaded {len(rows)} bias_history samples from DB")
    except Exception as e:
        logger.warning(f"Failed to load bias history from DB: {e}")
    finally:
        try:
            conn.close()
        except Exception:
            pass


def insert_history_to_db(api_temp, observed_temp):
    """Insert one history row into DB."""
    try:
        conn = sqlite3.connect(BIAS_DB_FILE, timeout=10)
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO bias_history (api_temp, observed_temp, ts) VALUES (?, ?, ?)",
            (float(api_temp), float(observed_temp), int(time.time()))
        )
        conn.commit()
    except Exception as e:
        logger.warning(f"Failed to insert bias history to DB: {e}")
    finally:
        try:
            conn.close()
        except Exception:
            pass

# -------------------------------------------------------------------------


def _now_local():
    if ZoneInfo is not None:
        try:
            return datetime.now(ZoneInfo(TIMEZONE))
        except Exception:
            return datetime.now()
    return datetime.now()


def _mean(lst):
    return round(sum(lst) / len(lst), 1) if lst else None


def _find_hour_index(hour_times, target_hour_str):
    if not hour_times:
        return None
    if target_hour_str in hour_times:
        return hour_times.index(target_hour_str)
    try:
        parsed = [datetime.fromisoformat(t) for t in hour_times]
        target = datetime.fromisoformat(target_hour_str)
        diffs = [abs((p - target).total_seconds()) for p in parsed]
        return int(min(range(len(diffs)), key=lambda i: diffs[i]))
    except Exception:
        return 0


def _nice_weather_desc(base_phrase: str, precip: float | None, precip_prob: float | None, windspeed: float | None):
    """Compose a human-friendly Vietnamese description combining base phrase + precip + wind info."""
    parts = []
    if base_phrase:
        parts.append(base_phrase)

    # precipitation probability
    if precip_prob is not None:
        try:
            pp = int(round(float(precip_prob)))
            if pp > 0:
                parts.append(f"khả năng mưa ~{pp}%")
        except Exception:
            pass

    # precipitation amount
    if precip is not None:
        try:
            p = float(precip)
            if p > 0.0:
                # show in mm with one decimal if small
                parts.append(f"lượng mưa ~{round(p,1)} mm")
        except Exception:
            pass

    # wind
    if windspeed is not None:
        try:
            w = float(windspeed)
            if w >= 15:
                parts.append(f"gió mạnh {int(round(w))} km/h")
            elif w >= 8:
                parts.append(f"gió vừa {int(round(w))} km/h")
            elif w > 0:
                parts.append(f"gió nhẹ {int(round(w))} km/h")
        except Exception:
            pass

    # join into natural sentence
    if not parts:
        return base_phrase or "Không có dữ liệu"
    # capitalize first part
    s = ", ".join(parts)
    return s[0].upper() + s[1:]


def get_weather_forecast():
    now = _now_local()
    if time.time() - weather_cache["ts"] < WEATHER_CACHE_SECONDS and weather_cache.get("data"):
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
        if r.status_code != 200:
            logger.warning(f"Weather API non-200 ({r.status_code}). Body: {r.text}")
            # try retry without start/end dates
            try:
                params.pop("start_date", None)
                params.pop("end_date", None)
                r2 = requests.get(url, params=params, timeout=10)
                if r2.status_code == 200:
                    data = r2.json()
                else:
                    logger.warning(f"Retry without dates also failed ({r2.status_code}): {r2.text}")
                    if weather_cache.get("data"):
                        return weather_cache["data"]
                    r2.raise_for_status()
            except Exception as e2:
                logger.warning(f"Retry error: {e2}")
                if weather_cache.get("data"):
                    return weather_cache["data"]
                raise
        else:
            r.raise_for_status()
            data = r.json()

        daily = data.get("daily", {})
        hourly = data.get("hourly", {})

        def safe_get(arr, idx):
            try:
                return arr[idx]
            except Exception:
                return None

        hums = hourly.get("relativehumidity_2m", [])
        humidity_yesterday = round(sum(hums[0:24]) / len(hums[0:24]), 1) if len(hums) >= 24 else None
        humidity_today = round(sum(hums[24:48]) / len(hums[24:48]), 1) if len(hums) >= 48 else None
        humidity_tomorrow = round(sum(hums[48:72]) / len(hums[48:72]), 1) if len(hums) >= 72 else None

        weather_yesterday = {"date": None, "desc": None, "max": None, "min": None, "precipitation_sum": None, "windspeed_max": None}
        weather_today = weather_yesterday.copy()
        weather_tomorrow = weather_yesterday.copy()

        if "time" in daily and daily["time"]:
            today_str = now.strftime("%Y-%m-%d")
            try:
                idx_today = daily["time"].index(today_str)
            except ValueError:
                idx_today = 1 if len(daily.get("time", [])) > 1 else 0
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

        hour_times = hourly.get("time", [])
        current_hour_str = now.replace(minute=0, second=0, microsecond=0).strftime("%Y-%m-%dT%H:00")
        idx = _find_hour_index(hour_times, current_hour_str) or 0

        next_hours = []
        for offset in range(0, EXTENDED_HOURS):
            i = idx + offset
            if i >= len(hour_times):
                break
            wcode = safe_get(hourly.get("weathercode", []), i)
            base = WEATHER_CODE_MAP.get(wcode, "Không rõ")
            precip = safe_get(hourly.get("precipitation", []), i)
            precip_prob = safe_get(hourly.get("precipitation_probability", []), i)
            windspeed = safe_get(hourly.get("windspeed_10m", []), i)

            # build nicer description
            nice_desc = _nice_weather_desc(base, precip, precip_prob, windspeed)

            h = {
                "time": hour_times[i],
                "temperature": safe_get(hourly.get("temperature_2m", []), i),
                "humidity": safe_get(hourly.get("relativehumidity_2m", []), i),
                "weather_code": wcode,
                "weather_desc": nice_desc,
                "precipitation": precip,
                "precip_probability": precip_prob,
                "windspeed": windspeed,
                "winddir": safe_get(hourly.get("winddirection_10m", []), i)
            }
            next_hours.append(h)

        result = {
            "meta": {"latitude": LAT, "longitude": LON, "tz": TIMEZONE, "fetched_at": now.isoformat()},
            "yesterday": weather_yesterday,
            "today": weather_today,
            "tomorrow": weather_tomorrow,
            "next_hours": next_hours,
            "humidity_yesterday": humidity_yesterday,
            "humidity_today": humidity_today,
            "humidity_tomorrow": humidity_tomorrow
        }

        weather_cache["data"] = result
        weather_cache["ts"] = time.time()
        return result

    except requests.HTTPError as he:
        logger.warning(f"Weather API HTTPError: {he}")
        if weather_cache.get("data"):
            logger.info("Returning last cached weather data due to API error.")
            return weather_cache["data"]
        return {"meta": {}, "yesterday": {}, "today": {}, "tomorrow": {}, "next_hours": [], "humidity_yesterday": None, "humidity_today": None, "humidity_tomorrow": None}
    except Exception as e:
        logger.warning(f"Weather API unexpected error: {e}")
        if weather_cache.get("data"):
            return weather_cache["data"]
        return {"meta": {}, "yesterday": {}, "today": {}, "tomorrow": {}, "next_hours": [], "humidity_yesterday": None, "humidity_today": None, "humidity_tomorrow": None}


# ================== BIAS CORRECTION ==================

def update_bias_and_correct(next_hours, observed_temp):
    """Update bias history with (api_now, observed_temp) and return bias.

    NOTE: This implementation **does NOT** create or push any "*_temperature_corrected" keys.
    It only updates the bias history (persisted) and returns the bias value for telemetry.
    """
    global bias_history
    if not next_hours:
        return 0.0

    api_now = next_hours[0].get("temperature")
    if api_now is not None and observed_temp is not None:
        try:
            bias_history.append((api_now, observed_temp))
            insert_history_to_db(api_now, observed_temp)
        except Exception:
            pass

    if bias_history:
        diffs = [obs - api for api, obs in bias_history if api is not None and obs is not None]
    else:
        diffs = []

    if diffs:
        bias = round(sum(diffs) / len(diffs), 1)
    else:
        bias = 0.0

    return bias


# ================== LLM (OpenRouter / Gemini) INTEGRATION ==================

def call_openrouter_llm(system_prompt: str, user_prompt: str, model: str = LLM_MODEL, max_tokens: int = 400, temperature: float = 0.0):
    if not OPENROUTER_API_KEY:
        raise RuntimeError("OPENROUTER_API_KEY is not configured in environment")

    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json"
    }

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt}
        ],
        "max_tokens": max_tokens,
        "temperature": temperature
    }

    r = requests.post(OPENROUTER_URL, headers=headers, json=payload, timeout=20)
    r.raise_for_status()
    data = r.json()

    try:
        content = data["choices"][0]["message"]["content"]
    except Exception:
        content = json.dumps(data)
    return content


def extract_json_like(text: str):
    if not text:
        raise ValueError("Empty text")
    m = re.search(r"\{[\s\S]*\}", text)
    if m:
        candidate = m.group(0)
        try:
            return json.loads(candidate)
        except Exception:
            pass
    try:
        return json.loads(text)
    except Exception as e:
        raise ValueError("No JSON found in LLM response") from e

# ================== AI HELPER (rule-based)

def get_advice(temp, humi, upcoming_weather=None):
    nutrition = ["Ưu tiên Kali (K)", "Cân bằng NPK", "Bón phân hữu cơ"]
    care = []

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
        logger.info(f"TB ▶ sending payload (keys: {list(data.keys())})")
        r = requests.post(TB_DEVICE_URL, json=data, timeout=10)
        logger.info(f"TB ◀ {r.status_code}")
    except Exception as e:
        logger.error(f"ThingsBoard push error: {e}")

# ================== HELPERS ==================

def merge_weather_and_hours(existing_data=None):
    if existing_data is None:
        existing_data = {}
    weather = get_weather_forecast()

    now = _now_local()
    flattened = {**existing_data}

    # daily (keep keys and expand description slightly)
    if weather.get("today"):
        t = weather.get("today")
        if t.get("desc") is not None:
            # include temp range
            rng = None
            if t.get("max") is not None and t.get("min") is not None:
                rng = f" ({t.get('min')}–{t.get('max')}°C)"
            flattened["weather_today_desc"] = (t.get("desc") or "") + (rng or "")
        if t.get("max") is not None:
            flattened["weather_today_max"] = t.get("max")
        if t.get("min") is not None:
            flattened["weather_today_min"] = t.get("min")
    if weather.get("tomorrow"):
        tt = weather.get("tomorrow")
        if tt.get("desc") is not None:
            rng = None
            if tt.get("max") is not None and tt.get("min") is not None:
                rng = f" ({tt.get('min')}–{tt.get('max')}°C)"
            flattened["weather_tomorrow_desc"] = (tt.get("desc") or "") + (rng or "")
        if tt.get("max") is not None:
            flattened["weather_tomorrow_max"] = tt.get("max")
        if tt.get("min") is not None:
            flattened["weather_tomorrow_min"] = tt.get("min")
    if weather.get("yesterday"):
        ty = weather.get("yesterday")
        if ty.get("desc") is not None:
            flattened["weather_yesterday_desc"] = ty.get("desc")
        if ty.get("max") is not None:
            flattened["weather_yesterday_max"] = ty.get("max")
        if ty.get("min") is not None:
            flattened["weather_yesterday_min"] = ty.get("min")

    # aggregated humidity
    if weather.get("humidity_today") is not None:
        flattened["humidity_today"] = weather.get("humidity_today")
    else:
        hlist = [h.get("humidity") for h in weather.get("next_hours", []) if h.get("humidity") is not None]
        flattened["humidity_today"] = round(sum(hlist)/len(hlist),1) if hlist else None
    if weather.get("humidity_tomorrow") is not None:
        flattened["humidity_tomorrow"] = weather.get("humidity_tomorrow")
    if weather.get("humidity_yesterday") is not None:
        flattened["humidity_yesterday"] = weather.get("humidity_yesterday")

    # hours (respect EXTENDED_HOURS keys, but keep key naming exactly as before)
    for idx in range(0, EXTENDED_HOURS):
        h = None
        if idx < len(weather.get("next_hours", [])):
            h = weather["next_hours"][idx]
        time_label = None
        if h and h.get("time"):
            try:
                t = datetime.fromisoformat(h.get("time"))
                time_label = t.strftime("%H:%M")
            except Exception:
                time_label = h.get("time")
        if time_label is not None:
            flattened[f"hour_{idx}"] = time_label
        temp = h.get("temperature") if h else None
        hum = h.get("humidity") if h else None
        desc = h.get("weather_desc") if h else None
        if temp is not None:
            flattened[f"hour_{idx}_temperature"] = temp
        if hum is not None:
            flattened[f"hour_{idx}_humidity"] = hum
        if desc is not None:
            flattened[f"hour_{idx}_weather_desc"] = desc
        # NOTE: intentionally DO NOT add hour_N_temperature_corrected keys

    # keep observed
    if "temperature" not in flattened:
        flattened["temperature"] = existing_data.get("temperature")
    if "humidity" not in flattened:
        flattened["humidity"] = existing_data.get("humidity")
    if "prediction" not in flattened:
        flattened["prediction"] = existing_data.get("prediction")
    if "location" not in flattened:
        flattened["location"] = existing_data.get("location")
    if "crop" not in flattened:
        flattened["crop"] = existing_data.get("crop")

    return flattened

# ================== ROUTES ==================
@app.get("/")
def root():
    return {"status": "running", "demo_token": TB_DEMO_TOKEN[:4] + "***", "extended_hours": EXTENDED_HOURS}

@app.get("/weather")
def weather_endpoint():
    return get_weather_forecast()

@app.get("/bias")
def bias_status():
    diffs = [round(obs - api, 2) for api, obs in bias_history if api is not None and obs is not None]
    bias = round(sum(diffs) / len(diffs), 2) if diffs else 0.0
    return {"bias": bias, "history_len": len(diffs)}

@app.post("/esp32-data")
def receive_data(data: SensorData):
    logger.info(f"ESP32 ▶ received sensor data: {{'temperature':..., 'humidity':..., 'battery':...}}")
    weather = get_weather_forecast()
    next_hours = weather.get("next_hours", [])

    # update bias (but do NOT create *_corrected keys)
    bias = update_bias_and_correct(next_hours, data.temperature)

    # baseline (rule-based) advice
    advice_data = get_advice(data.temperature, data.humidity, upcoming_weather=next_hours)

    merged = {
        **data.dict(),
        **advice_data,
        "location": "An Phú, Hồ Chí Minh",
        "crop": "Rau muống",
        "forecast_bias": bias,
        "forecast_history_len": len(bias_history)
    }

    llm_advice = None
    # CALL LLM ALWAYS if OPENROUTER_API_KEY is set (user requested AI on every request)
    if OPENROUTER_API_KEY:
        try:
            system_prompt = (
                "Bạn là một chuyên gia nông nghiệp và dự báo thời tiết. Trả về CHỈ MỘT đối tượng JSON ngắn gọn với các trường: "
                "advice (string ngắn, tiếng Việt), priority (low/medium/high), actions (mảng string các hành động cụ thể), "
                "reason (giải thích ngắn). KHÔNG kèm văn bản khác."
            )
            user_prompt = (
                f"Observed: temp={data.temperature}C, hum={data.humidity}%. Bias={bias}. "
                f"Next_hours: {json.dumps(next_hours, ensure_ascii=False)}. Return JSON only."
            )
            resp_text = call_openrouter_llm(system_prompt, user_prompt)
            try:
                llm_json = extract_json_like(resp_text)
                llm_advice = llm_json
                # If LLM returned structured advice, prefer it (but don't remove original keys)
                if isinstance(llm_json, dict):
                    if llm_json.get("advice"):
                        merged["advice"] = llm_json.get("advice")
                    # map optional fields into existing telemetry keys when sensible
                    if llm_json.get("actions"):
                        merged["advice_care"] = " | ".join(llm_json.get("actions"))
                    if llm_json.get("priority"):
                        merged["advice_note"] = f"priority: {llm_json.get('priority')}"
            except Exception:
                # LLM returned non-JSON; store raw text
                llm_advice = {"raw": resp_text}
        except Exception as e:
            logger.warning(f"LLM call failed: {e}")
            # fallback: keep rule-based advice but record the error
            llm_advice = {"error": "llm_failed", "reason": str(e)}
    else:
        logger.info("OPENROUTER_API_KEY not set; skipping LLM call (set env to enable)")

    merged["llm_advice"] = llm_advice

    # attach hours and push
    merged = merge_weather_and_hours(existing_data=merged)
    send_to_thingsboard(merged)
    return {"received": data.dict(), "pushed": merged}

# ================== AUTO LOOP (simulator) ==================
async def auto_loop():
    logger.info("Auto-loop simulator started (auto-calling LLM on each sample if enabled)")
    battery = 4.2
    tick = 0
    while True:
        try:
            now = _now_local()
            hour = now.hour + now.minute / 60.0
            base = 27.0
            amplitude = 6.0
            temp = base + amplitude * math.sin((hour - 14) / 24.0 * 2 * math.pi) + random.uniform(-0.7, 0.7)
            humi = max(20.0, min(95.0, 75 - (temp - base) * 3 + random.uniform(-5, 5)))
            battery = max(3.3, battery - random.uniform(0.0005, 0.0025))
            sample = {"temperature": round(temp, 1), "humidity": round(humi, 1), "battery": round(battery, 3)}

            weather = get_weather_forecast()
            bias = update_bias_and_correct(weather.get("next_hours", []), sample["temperature"])  # update only
            advice_data = get_advice(sample["temperature"], sample["humidity"], upcoming_weather=weather.get("next_hours", []))

            merged = {
                **sample,
                **advice_data,
                "location": "An Phú, Hồ Chí Minh",
                "crop": "Rau muống",
                "forecast_bias": bias,
                "forecast_history_len": len(bias_history)
            }

            # Call LLM in auto-loop as well if key present (user asked AI always on)
            llm_advice = None
            if OPENROUTER_API_KEY:
                try:
                    system_prompt = (
                        "Bạn là một chuyên gia nông nghiệp. Trả về CHỈ MỘT JSON ngắn gọn gồm: "
                        "advice, priority, actions, reason. Return JSON only."
                    )
                    user_prompt = (
                        f"Auto-sim sample: temp={sample['temperature']}C, hum={sample['humidity']}%. "
                        f"Next_hours: {json.dumps(weather.get('next_hours', []), ensure_ascii=False)}. Return JSON only."
                    )
                    resp_text = call_openrouter_llm(system_prompt, user_prompt)
                    try:
                        llm_json = extract_json_like(resp_text)
                        llm_advice = llm_json
                        if isinstance(llm_json, dict) and llm_json.get("advice"):
                            merged["advice"] = llm_json.get("advice")
                            if llm_json.get("actions"):
                                merged["advice_care"] = " | ".join(llm_json.get("actions"))
                    except Exception:
                        llm_advice = {"raw": resp_text}
                except Exception as e:
                    logger.warning(f"LLM call failed in auto-loop: {e}")
                    llm_advice = {"error": "llm_failed"}
            merged["llm_advice"] = llm_advice

            merged = merge_weather_and_hours(existing_data=merged)
            send_to_thingsboard(merged)
            tick += 1
        except Exception as e:
            logger.error(f"AUTO loop error: {e}")
        await asyncio.sleep(AUTO_LOOP_INTERVAL)

@app.on_event("startup")
async def startup():
    # init sqlite and load history
    init_db()
    load_history_from_db()
    # start autosave/auto-loop tasks
    asyncio.create_task(auto_loop())

# ================== NOTES ==================
# - Run with: uvicorn main:app --host 0.0.0.0 --port $PORT
# - To enable LLM (Gemini via OpenRouter), set OPENROUTER_API_KEY in your Render environment.
# - This version: improved human-friendly hourly descriptions (hour_N_weather_desc),
#   removed all hour_N_temperature_corrected keys (NOT pushed), and supports EXTENDED_HOURS env (default 12).

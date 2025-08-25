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

# LLM / OpenRouter configuration
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
LLM_MODEL = os.getenv("LLM_MODEL", "google/gemini-2.5-pro")
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

# LLM calling behavior
LLM_CALL_MIN_HISTORY = int(os.getenv("LLM_CALL_MIN_HISTORY", 1))  # minimum bias samples before calling LLM
LLM_BACKOFF_SECONDS = int(os.getenv("LLM_BACKOFF_SECONDS", 3600))  # backoff after 402 by default 1 hour
LLM_MIN_INTERVAL = int(os.getenv("LLM_MIN_INTERVAL", 300))  # minimal seconds between LLM calls (throttle)

# Bias correction settings (kept in-memory and persisted in SQLite)
MAX_HISTORY = int(os.getenv("BIAS_MAX_HISTORY", 48))
bias_history = deque(maxlen=MAX_HISTORY)
BIAS_DB_FILE = os.getenv("BIAS_DB_FILE", "bias_history.db")

# LLM runtime state
llm_disabled_until = 0  # timestamp until which LLM calls are skipped due to 402 or manual disable
_last_llm_call_ts = 0   # last LLM call timestamp (for rate-limiting)

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
# Adjusted weather code map: avoid explicit "mưa đá" label to reduce false alarms in VN.
WEATHER_CODE_MAP = {
    0: "Trời quang",
    1: "Trời quang nhẹ",
    2: "Có mây",
    3: "Nhiều mây",
    45: "Sương mù",
    48: "Sương mù (đóng băng rất hiếm ở VN)",
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
    71: "Hiếm (điều kiện có tuyết, không thường gặp ở VN)",
    73: "Hiếm (điều kiện có tuyết, không thường gặp ở VN)",
    75: "Hiếm (điều kiện có tuyết, không thường gặp ở VN)",
    77: "Giông (có thể kèm hiện tượng hiếm)",
    80: "Mưa rào nhẹ",
    81: "Mưa rào vừa",
    82: "Mưa rào mạnh",
    85: "Hiếm (tuyết, không thường gặp ở VN)",
    86: "Hiếm (tuyết, không thường gặp ở VN)",
    95: "Giông nhẹ hoặc vừa",
    96: "Giông (có thể kèm mưa đá — rất hiếm ở VN)",
    99: "Giông (có thể kèm mưa đá — rất hiếm ở VN)"
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


def _current_hour_str_rounded():
    """Return local ISO hour string rounded the same way merge_weather_and_hours uses.
    Rounding rule: if minute > 30 -> round up to next hour, else use current hour.
    This avoids mismatches between label generation and the hour index used to pick api temps.
    """
    now = _now_local()
    base = now.replace(minute=0, second=0, microsecond=0)
    if now.minute > 30:
        base = base + timedelta(hours=1)
    return base.strftime("%Y-%m-%dT%H:00")


def _find_hour_index(hour_times, target_hour_str):
    """Find the index in hour_times closest to target_hour_str. Returns 0 if not found.
    This is more robust than exact string match and tolerates timezone formatting differences.
    """
    if not hour_times:
        return None
    # exact match first
    if target_hour_str in hour_times:
        return hour_times.index(target_hour_str)
    # try parsing ISO datetimes and find nearest
    try:
        parsed = []
        for t in hour_times:
            try:
                parsed.append(datetime.fromisoformat(t))
            except Exception:
                parsed.append(None)
        target = datetime.fromisoformat(target_hour_str)
        diffs = [abs((p - target).total_seconds()) if p is not None else float('inf') for p in parsed]
        best = int(min(range(len(diffs)), key=lambda i: diffs[i]))
        return best
    except Exception:
        return 0


def _find_closest_index_in_next_hours(next_hours, target_hour_str):
    """Given next_hours (list of dicts with 'time'), find index closest to target_hour_str."""
    if not next_hours:
        return None
    times = [h.get("time") for h in next_hours if h.get("time")]
    if not times:
        return 0
    return _find_hour_index(times, target_hour_str)


def _call_open_meteo(params):
    """Helper wrapper to call Open-Meteo and return (status, response-json-or-text)."""
    url = "https://api.open-meteo.com/v1/forecast"
    try:
        r = requests.get(url, params=params, timeout=10)
        if r.status_code != 200:
            return r.status_code, r.text
        return 200, r.json()
    except Exception as e:
        return None, str(e)


def get_weather_forecast():
    """Fetch forecast with robust fallbacks and preserve cache if new fetch fails."""
    now = _now_local()
    # return cached if still fresh
    if time.time() - weather_cache["ts"] < WEATHER_CACHE_SECONDS and weather_cache.get("data"):
        return weather_cache["data"]

    # primary params (more detailed)
    start_date = (now - timedelta(days=1)).strftime("%Y-%m-%d")
    end_date = (now + timedelta(days=2)).strftime("%Y-%m-%d")
    common = {
        "latitude": LAT,
        "longitude": LON,
        "timezone": TIMEZONE,
        "start_date": start_date,
        "end_date": end_date
    }
    primary = dict(common)
    primary.update({
        "daily": ",".join(["weathercode", "temperature_2m_max", "temperature_2m_min", "precipitation_sum", "windspeed_10m_max"]),
        "hourly": ",".join(["time", "temperature_2m", "relativehumidity_2m", "weathercode", "precipitation", "windspeed_10m", "winddirection_10m", "precipitation_probability"])
    })

    # a simpler fallback hourly list (less likely to trigger server-side parsing bugs)
    fallback_hourly = ",".join(["temperature_2m", "relativehumidity_2m", "weathercode", "precipitation", "windspeed_10m"])
    fallback = dict(common)
    fallback.update({
        "daily": ",".join(["weathercode", "temperature_2m_max", "temperature_2m_min"]),
        "hourly": fallback_hourly
    })

    # Try primary
    status, data_or_text = _call_open_meteo(primary)
    if status == 200:
        data = data_or_text
    else:
        logger.warning(f"Weather API non-200 ({status}). Body: {str(data_or_text)[:1000]}")
        # Try fallback
        status2, data_or_text2 = _call_open_meteo(fallback)
        if status2 == 200:
            data = data_or_text2
            logger.info("Weather API: fallback parameters succeeded.")
        else:
            logger.warning(f"Fallback weather API failed ({status2}). Body: {str(data_or_text2)[:1000]}")
            # If we have cached data, return it (do not overwrite cache or set nulls)
            if weather_cache.get("data"):
                logger.info("Returning existing cached weather data due to API failure.")
                return weather_cache["data"]
            # Otherwise return a safe empty skeleton (but avoid populating None keys in telemetry later)
            return {"meta": {}, "yesterday": {}, "today": {}, "tomorrow": {}, "next_hours": [], "humidity_yesterday": None, "humidity_today": None, "humidity_tomorrow": None}

    # At this point `data` is the parsed JSON
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
    # Use the same rounding rule as merge_weather_and_hours
    current_hour_str = _current_hour_str_rounded()
    idx = _find_hour_index(hour_times, current_hour_str) or 0

    next_hours = []
    # build 7 hours (0..6) to match hour_0..hour_6 on ThingsBoard
    for offset in range(0, 7):
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
            "precip_probability": safe_get(hourly.get("precipitation_probability" , []), i),
            "windspeed": safe_get(hourly.get("windspeed_10m", []), i),
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

# ================== BIAS CORRECTION ==================

def update_bias_and_correct(next_hours, observed_temp):
    """Update bias history with (api_now, observed_temp) and return bias + corrected next_hours.

    Changes made:
    - find api_now by matching the same rounded local hour used in merge_weather_and_hours
      (avoids off-by-one-hour label mismatch that caused large apparent bias).
    - compute bias using an exponential moving average (EMA) over diffs to be robust to outliers.
    - clamp the applied bias to a reasonable range (default ±5°C) to avoid wild corrections.
    """
    global bias_history
    if not next_hours:
        return 0.0, next_hours

    # find the api temperature that corresponds to the current rounded hour
    api_now = None
    try:
        target_hour_str = _current_hour_str_rounded()
        idx = _find_closest_index_in_next_hours(next_hours, target_hour_str)
        if idx is None:
            idx = 0
        if 0 <= idx < len(next_hours):
            api_now = next_hours[idx].get("temperature")
    except Exception:
        api_now = next_hours[0].get("temperature") if next_hours else None

    if api_now is not None and observed_temp is not None:
        try:
            bias_history.append((api_now, observed_temp))
            insert_history_to_db(api_now, observed_temp)
        except Exception:
            pass

    # compute diffs safely (chronological order)
    diffs = [obs - api for api, obs in bias_history if api is not None and obs is not None]

    if diffs:
        # use EMA to be robust: recent samples weighted more
        alpha = 0.3
        ema = diffs[0]
        for d in diffs[1:]:
            ema = ema * (1 - alpha) + d * alpha
        bias = round(ema, 1)
        # clamp to reasonable bounds to avoid absurd corrections (e.g., sensor or API glitch)
        max_clamp = float(os.getenv("BIAS_MAX_CLAMP", 5.0))
        if abs(bias) > max_clamp:
            logger.warning(f"Computed bias {bias} exceeds clamp {max_clamp}; clamping")
            bias = round(math.copysign(max_clamp, bias), 1)
    else:
        bias = 0.0

    # apply correction
    for h in next_hours:
        if h.get("temperature") is not None:
            h["temperature_corrected"] = round(h["temperature"] + bias, 1)
        else:
            h["temperature_corrected"] = None

    return bias, next_hours

# ================== LLM (OpenRouter / Gemini) INTEGRATION ==================

def call_openrouter_llm(system_prompt: str, user_prompt: str, model: str = LLM_MODEL, max_tokens: int = 400, temperature: float = 0.0):
    """Call OpenRouter and return a dict with detailed result for robust handling."""
    global llm_disabled_until
    # quick skip if previously disabled due to 402
    if time.time() < llm_disabled_until:
        return {"ok": False, "error": "llm_disabled_temporarily", "detail": f"disabled until {llm_disabled_until}"}

    if not OPENROUTER_API_KEY:
        return {"ok": False, "error": "OPENROUTER_API_KEY_not_set"}

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

    try:
        r = requests.post(OPENROUTER_URL, headers=headers, json=payload, timeout=20)
    except Exception as e:
        logger.warning(f"LLM network error: {e}")
        return {"ok": False, "error": "network_error", "detail": str(e)}

    status = r.status_code
    body_text = ""
    try:
        body_text = r.text
    except Exception:
        body_text = "<unreadable body>"

    if status != 200:
        logger.warning(f"LLM HTTP {status}: {body_text[:1000]}")
        # If insufficient credits (402), temporarily disable LLM calls for configured backoff
        if status == 402:
            llm_disabled_until = time.time() + LLM_BACKOFF_SECONDS
            logger.warning(f"LLM disabled for {LLM_BACKOFF_SECONDS} seconds due to HTTP 402 (insufficient credits).")
        return {"ok": False, "error": "http_error", "status": status, "body": body_text}

    try:
        data = r.json()
    except Exception as e:
        logger.warning(f"LLM returned non-JSON: {e}; body: {body_text[:2000]}")
        return {"ok": False, "error": "non_json_response", "status": status, "body": body_text}

    try:
        content = data["choices"][0]["message"]["content"]
    except Exception:
        content = json.dumps(data)

    return {"ok": True, "content": content, "raw": data}


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

def get_advice_rule(temp, humi, upcoming_weather=None):
    """Original rule-based advice kept as a safe fallback.
    Returns the same shape as previous: advice, advice_nutrition, advice_care, advice_note, prediction
    """
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


def get_advice(temp, humi, upcoming_weather=None):
    """Primary advice function: try LLM (OpenRouter/Gemini) first, fall back to rule-based if LLM not available or fails.

    Expected LLM JSON response (Vietnamese):
    {
      "advice": "<short advice>",
      "priority": "low|medium|high",
      "actions": ["action1", "action2", ...],
      "reason": "<short reason>"
    }

    The returned dict keeps previous telemetry keys so ThingsBoard dashboards don't need changes.
    """
    # default nutrition strings kept for telemetry even when LLM used
    nutrition = ["Ưu tiên Kali (K)", "Cân bằng NPK", "Bón phân hữu cơ"]

    # If no API key or LLM in backoff, use rule-based immediately
    if not OPENROUTER_API_KEY or time.time() < llm_disabled_until:
        return get_advice_rule(temp, humi, upcoming_weather)

    # Build prompts (Vietnamese) and call LLM
    system_prompt = (
        "Bạn là một chuyên gia nông nghiệp ở Việt Nam, hiểu khí hậu nhiệt đới. "
        "Trả về CHỈ MỘT đối tượng JSON (không kèm chú giải nào khác) với các trường: "
        "advice (string ngắn, tiếng Việt), priority (low/medium/high), actions (mảng string các hành động cụ thể, tiếng Việt), reason (giải thích ngắn)."
    )

    # include upcoming_weather as compact JSON to give context (may be empty list)
    try:
        upcoming_json = json.dumps(upcoming_weather or [], ensure_ascii=False)
    except Exception:
        upcoming_json = "[]"

    user_prompt = (
        f"Observed: temp={temp}C, hum={humi}%. Upcoming_hours: {upcoming_json}. "
        "Trả về JSON theo yêu cầu."
    )

    resp = call_openrouter_llm(system_prompt, user_prompt, temperature=0.0, max_tokens=300)
    if not resp.get("ok"):
        # fallback when LLM fails or is disabled
        logger.warning(f"LLM advice failed or skipped: {resp.get('error')}")
        return get_advice_rule(temp, humi, upcoming_weather)

    # try parse LLM response to JSON
    try:
        llm_json = extract_json_like(resp.get("content", ""))
        if not isinstance(llm_json, dict):
            raise ValueError("LLM returned non-dict JSON")

        # normalize fields
        advice_text = llm_json.get("advice") or ""
        priority = llm_json.get("priority") or "low"
        actions = llm_json.get("actions") or []
        if isinstance(actions, str):
            # sometimes LLM returns a string list; try to split by lines or commas
            actions = [a.strip() for a in re.split(r"[
,;]+", actions) if a.strip()]
        reason = llm_json.get("reason") or ""

        advice_care = " | ".join(actions) if actions else (llm_json.get("reason") or "")

        return {
            "advice": advice_text or " | ".join(nutrition + ["Quan sát thực tế và điều chỉnh"]),
            "advice_nutrition": " | ".join(nutrition),
            "advice_care": advice_care,
            "advice_note": f"priority: {priority}" if priority else "",
            "prediction": f"Nhiệt độ {temp}°C, độ ẩm {humi}%",
        }
    except Exception as e:
        logger.warning(f"Failed to parse LLM advice: {e}; raw: {resp.get('content')}")
        # final fallback to rule-based
        return get_advice_rule(temp, humi, upcoming_weather)

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
    """
    Merge weather forecast into telemetry but DO NOT overwrite telemetry keys with None.
    Use local rounded hour labels for hour_0..hour_6 so labels always reflect current local time,
    while temperatures/humidities are taken from weather['next_hours'].
    Also remove stale keys if API did not return data for that slot.
    """
    if existing_data is None:
        existing_data = {}
    weather = get_weather_forecast()

    now = _now_local()
    flattened = {**existing_data}  # start with existing telemetry keys (preserve!)

    # daily: only set values when not None
    if weather.get("today"):
        tdesc = weather.get("today", {}).get("desc")
        if tdesc is not None:
            flattened["weather_today_desc"] = tdesc
        tmax = weather.get("today", {}).get("max")
        if tmax is not None:
            flattened["weather_today_max"] = tmax
        tmin = weather.get("today", {}).get("min")
        if tmin is not None:
            flattened["weather_today_min"] = tmin

    if weather.get("tomorrow"):
        tdesc = weather.get("tomorrow", {}).get("desc")
        if tdesc is not None:
            flattened["weather_tomorrow_desc"] = tdesc
        tmax = weather.get("tomorrow", {}).get("max")
        if tmax is not None:
            flattened["weather_tomorrow_max"] = tmax
        tmin = weather.get("tomorrow", {}).get("min")
        if tmin is not None:
            flattened["weather_tomorrow_min"] = tmin

    if weather.get("yesterday"):
        tdesc = weather.get("yesterday", {}).get("desc")
        if tdesc is not None:
            flattened["weather_yesterday_desc"] = tdesc
        tmax = weather.get("yesterday", {}).get("max")
        if tmax is not None:
            flattened["weather_yesterday_max"] = tmax
        tmin = weather.get("yesterday", {}).get("min")
        if tmin is not None:
            flattened["weather_yesterday_min"] = tmin

    # humidity aggregated: only set when not None
    if weather.get("humidity_today") is not None:
        flattened["humidity_today"] = weather.get("humidity_today")
    else:
        # try compute from next_hours if available and not empty
        hlist = [h.get("humidity") for h in weather.get("next_hours", []) if h.get("humidity") is not None]
        if hlist:
            flattened["humidity_today"] = round(sum(hlist)/len(hlist),1)

    if weather.get("humidity_tomorrow") is not None:
        flattened["humidity_tomorrow"] = weather.get("humidity_tomorrow")
    if weather.get("humidity_yesterday") is not None:
        flattened["humidity_yesterday"] = weather.get("humidity_yesterday")

    # Build hour labels from current local time (rounded): hour_0 is current rounded hour, hour_1 next hour, ...
    # Rounding rule: if minute > 30 -> round up to next hour, else use current hour
    hour_rounded_dt = _now_local().replace(minute=0, second=0, microsecond=0)
    if _now_local().minute > 30:
        hour_rounded_dt = hour_rounded_dt + timedelta(hours=1)
    hour_rounded = hour_rounded_dt.hour
    hour_labels = []
    for i in range(0, 7):
        next_h = (hour_rounded + i) % 24
        hour_labels.append(f"{next_h:02d}:00")

    # hours: set label from hour_labels, set values (temp/hum/desc) from weather.next_hours if present
    for idx in range(0, 7):
        # set time label from local rounding (always set)
        flattened[f"hour_{idx}"] = hour_labels[idx]

        if idx < len(weather.get("next_hours", [])):
            h = weather["next_hours"][idx]
            temp = h.get("temperature")
            hum = h.get("humidity")
            desc = h.get("weather_desc")
            corrected = h.get("temperature_corrected")

            if temp is not None:
                flattened[f"hour_{idx}_temperature"] = temp
            else:
                flattened.pop(f"hour_{idx}_temperature", None)

            if hum is not None:
                flattened[f"hour_{idx}_humidity"] = hum
            else:
                flattened.pop(f"hour_{idx}_humidity", None)

            if desc is not None:
                flattened[f"hour_{idx}_weather_desc"] = desc
            else:
                flattened.pop(f"hour_{idx}_weather_desc", None)

            if corrected is not None:
                flattened[f"hour_{idx}_temperature_corrected"] = corrected
            else:
                flattened.pop(f"hour_{idx}_temperature_corrected", None)
        else:
            # no new data for this slot -> remove the keys so ThingsBoard doesn't show stale values
            flattened.pop(f"hour_{idx}_temperature", None)
            flattened.pop(f"hour_{idx}_humidity", None)
            flattened.pop(f"hour_{idx}_weather_desc", None)
            flattened.pop(f"hour_{idx}_temperature_corrected", None)

    # keep observed (do not overwrite with None)
    if "temperature" not in flattened and existing_data.get("temperature") is not None:
        flattened["temperature"] = existing_data.get("temperature")
    if "humidity" not in flattened and existing_data.get("humidity") is not None:
        flattened["humidity"] = existing_data.get("humidity")
    if "prediction" not in flattened and existing_data.get("prediction") is not None:
        flattened["prediction"] = existing_data.get("prediction")
    if "location" not in flattened and existing_data.get("location") is not None:
        flattened["location"] = existing_data.get("location")
    if "crop" not in flattened and existing_data.get("crop") is not None:
        flattened["crop"] = existing_data.get("crop")

    return flattened

# ================== ROUTES ==================
@app.get("/")
def root():
    return {"status": "running", "demo_token": TB_DEMO_TOKEN[:4] + "***"}

@app.get("/weather")
def weather_endpoint():
    return get_weather_forecast()

@app.get("/bias")
def bias_status():
    diffs = [round(obs - api, 2) for api, obs in bias_history if api is not None and obs is not None]
    bias = round(sum(diffs) / len(diffs), 2) if diffs else 0.0
    return {"bias": bias, "history_len": len(diffs)}

# debug endpoints added
@app.get("/history")
def get_bias_history():
    """Return current in-memory bias_history (chronological oldest-first) for debugging."""
    items = [{"api_temp": api, "observed_temp": obs} for api, obs in list(bias_history)]
    return {"history": items, "len": len(items)}

@app.post("/llm-unblock")
def llm_unblock():
    """Reset LLM backoff immediately (useful if you refilled credits)."""
    global llm_disabled_until
    llm_disabled_until = 0
    return {"ok": True, "llm_disabled_until": llm_disabled_until}

@app.post("/esp32-data")
def receive_data(data: SensorData):
    global _last_llm_call_ts
    logger.info(f"ESP32 ▶ received sensor data: {{'temperature':..., 'humidity':..., 'battery':...}}")
    weather = get_weather_forecast()
    next_hours = weather.get("next_hours", [])

    # update bias and get corrected forecast
    bias, corrected_next_hours = update_bias_and_correct(next_hours, data.temperature)

    # baseline (rule-based) advice
    advice_data = get_advice(data.temperature, data.humidity, upcoming_weather=corrected_next_hours)

    merged = {
        **data.dict(),
        **advice_data,
        "location": "An Phú, Hồ Chí Minh",
        "crop": "Rau muống",
        "forecast_bias": bias,
        "forecast_history_len": len(bias_history)
    }

    llm_advice = None
    # Decide whether to call LLM:
    now_ts = time.time()
    can_call_llm = (
        OPENROUTER_API_KEY
        and now_ts >= llm_disabled_until
        and len(bias_history) >= LLM_CALL_MIN_HISTORY
        and (now_ts - _last_llm_call_ts) >= LLM_MIN_INTERVAL
    )

    if can_call_llm:
        _last_llm_call_ts = now_ts
        try:
            system_prompt = (
                "Bạn là một chuyên gia nông nghiệp và dự báo thời tiết. Trả về CHỈ MỘT đối tượng JSON ngắn gọn với các trường: "
                "advice (string ngắn, tiếng Việt), priority (low/medium/high), actions (mảng string các hành động cụ thể), "
                "reason (giải thích ngắn). KHÔNG kèm văn bản khác."
            )
            user_prompt = (
                f"Observed: temp={data.temperature}C, hum={data.humidity}%. Bias={bias}. "
                f"Corrected next_hours: {json.dumps(corrected_next_hours, ensure_ascii=False)}. Return JSON only."
            )
            resp = call_openrouter_llm(system_prompt, user_prompt)
            if not resp.get("ok"):
                llm_advice = {
                    "error": resp.get("error"),
                    "status": resp.get("status"),
                    "body": (resp.get("body") or resp.get("detail"))
                }
                logger.warning(f"LLM call failed detail: {llm_advice}")
            else:
                resp_text = resp.get("content")
                try:
                    llm_json = extract_json_like(resp_text)
                    llm_advice = llm_json
                    # If LLM returned structured advice, prefer it (but don't remove original keys)
                    if isinstance(llm_json, dict):
                        if llm_json.get("advice"):
                            merged["advice"] = llm_json.get("advice")
                        if llm_json.get("actions"):
                            merged["advice_care"] = " | ".join(llm_json.get("actions"))
                        if llm_json.get("priority"):
                            merged["advice_note"] = f"priority: {llm_json.get('priority')}"
                except Exception:
                    # LLM returned non-JSON; store raw text
                    llm_advice = {"raw": resp_text}
        except Exception as e:
            logger.warning(f"LLM call unexpected error: {e}")
            llm_advice = {"error": "llm_failed", "reason": str(e)}
    else:
        # reason for skipping
        reason = "OPENROUTER_API_KEY_not_set" if not OPENROUTER_API_KEY else (
                 "in_backoff" if now_ts < llm_disabled_until else
                 ("history_too_short" if len(bias_history) < LLM_CALL_MIN_HISTORY else
                  "rate_limited"))
        logger.info(f"Skipping LLM call: {reason}")
        llm_advice = {"skipped": reason}

    merged["llm_advice"] = llm_advice

    # attach corrected hours and push
    weather["next_hours"] = corrected_next_hours
    merged = merge_weather_and_hours(existing_data=merged)
    send_to_thingsboard(merged)
    return {"received": data.dict(), "pushed": merged}

# ================== AUTO LOOP (simulator) ==================
async def auto_loop():
    global _last_llm_call_ts
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

            # Call LLM in auto-loop as well if key present and not in backoff and history/min-interval satisfied
            llm_advice = None
            now_ts = time.time()
            can_call_llm = (
                OPENROUTER_API_KEY
                and now_ts >= llm_disabled_until
                and len(bias_history) >= LLM_CALL_MIN_HISTORY
                and (now_ts - _last_llm_call_ts) >= LLM_MIN_INTERVAL
            )
            if can_call_llm:
                _last_llm_call_ts = now_ts
                try:
                    system_prompt = (
                        "Bạn là một chuyên gia nông nghiệp. Trả về CHỈ MỘT JSON ngắn gọn gồm: "
                        "advice, priority, actions, reason. Return JSON only."
                    )
                    user_prompt = (
                        f"Auto-sim sample: temp={sample['temperature']}C, hum={sample['humidity']}%. "
                        f"Corrected next_hours: {json.dumps(corrected_next_hours, ensure_ascii=False)}. Return JSON only."
                    )

                    resp = call_openrouter_llm(system_prompt, user_prompt)
                    if not resp.get("ok"):
                        llm_advice = {
                            "error": resp.get("error"),
                            "status": resp.get("status"),
                            "body": (resp.get("body") or resp.get("detail"))
                        }
                        logger.warning(f"LLM call failed (auto-loop) detail: {llm_advice}")
                    else:
                        resp_text = resp.get("content")
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
                    llm_advice = {"error": "llm_failed", "reason": str(e)}
            else:
                if OPENROUTER_API_KEY:
                    llm_advice = {"skipped": "disabled/backoff_or_rate_or_history"}
                else:
                    llm_advice = None

            merged["llm_advice"] = llm_advice

            weather["next_hours"] = corrected_next_hours
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
# - This version persists bias history in SQLite (BIAS_DB_FILE), protects telemetry keys from being overwritten by None,
#   uses a safer Open-Meteo fallback if detailed params fail, builds 7 hourly slots, aligns rounding rules between
#   prediction selection and hour labels, avoids alarming "mưa đá" wording, uses EMA for bias and clamps corrections.

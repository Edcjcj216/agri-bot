# main.py
import os
import time
import json
import math
import random
import logging
import requests
import asyncio
import sqlite3
import re
from fastapi import FastAPI
from pydantic import BaseModel
from datetime import datetime, timedelta
from collections import deque

# zoneinfo để xử lý timezone chính xác (Python 3.9+)
try:
    from zoneinfo import ZoneInfo
except Exception:
    ZoneInfo = None

# ================== CẤU HÌNH (ENV) ==================
TB_DEMO_TOKEN = os.getenv("TB_DEMO_TOKEN", "sgkxcrqntuki8gu1oj8u")
TB_DEVICE_URL = f"https://thingsboard.cloud/api/v1/{TB_DEMO_TOKEN}/telemetry"

LAT = float(os.getenv("LAT", "10.79"))
LON = float(os.getenv("LON", "106.70"))
AUTO_LOOP_INTERVAL = int(os.getenv("AUTO_LOOP_INTERVAL", "300"))  # giây giữa các lần gửi mẫu (auto-loop)
WEATHER_CACHE_SECONDS = int(os.getenv("WEATHER_CACHE_SECONDS", str(15 * 60)))  # cache thời tiết mặc định 15 phút
TIMEZONE = os.getenv("TZ", "Asia/Ho_Chi_Minh")

# LLM / OpenRouter (Gemini) - giữ tên key như bạn muốn
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
LLM_MODEL = os.getenv("LLM_MODEL", "google/gemini-2.5-pro")
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

# Số giờ mở rộng để xuất hour_0..hour_N-1 (mặc định 12 theo yêu cầu)
EXTENDED_HOURS = int(os.getenv("EXTENDED_HOURS", "12"))

# Bias correction (lưu ở memory + SQLite)
MAX_HISTORY = int(os.getenv("BIAS_MAX_HISTORY", "48"))
bias_history = deque(maxlen=MAX_HISTORY)
BIAS_DB_FILE = os.getenv("BIAS_DB_FILE", "bias_history.db")

# LLM backoff khi gặp lỗi (ví dụ 402) -> tránh spam request
LLM_BACKOFF_SECONDS_ON_402 = int(os.getenv("LLM_BACKOFF_SECONDS_ON_402", str(3600)))
llm_disabled_until = 0  # timestamp UNIX khi LLM được phép gọi lại

# ================== LOGGING ==================
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("agri-bot")

# ================== FASTAPI ==================
app = FastAPI(title="Agri Bot - ThingsBoard Gateway with Bias & LLM")

# Dữ liệu sensor từ ESP32
class SensorData(BaseModel):
    temperature: float
    humidity: float
    battery: float | None = None

# ================== MAPPING MÃ THỜI TIẾT ==================
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
    77: "Mưa tuyết/Trận băng",
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

# ================== SQLITE: persist bias history ==================
def init_db():
    """Khởi tạo DB sqlite để lưu history bias (nếu chưa tồn tại)."""
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
        conn.close()
        logger.info("SQLite bias DB initialized.")
    except Exception as e:
        logger.warning(f"Không thể khởi tạo DB bias: {e}")

def load_history_from_db():
    """Load các dòng gần nhất vào deque bias_history khi ứng dụng khởi động."""
    try:
        conn = sqlite3.connect(BIAS_DB_FILE)
        cur = conn.cursor()
        cur.execute("SELECT api_temp, observed_temp FROM bias_history ORDER BY id DESC LIMIT ?", (MAX_HISTORY,))
        rows = cur.fetchall()
        rows.reverse()  # chuyển thành thứ tự thời gian tăng dần
        for api, obs in rows:
            bias_history.append((float(api), float(obs)))
        conn.close()
        logger.info(f"Loaded {len(rows)} bias_history samples from DB")
    except Exception as e:
        logger.warning(f"Failed to load bias history from DB: {e}")

def insert_history_to_db(api_temp, observed_temp):
    """Insert 1 record vào SQLite (dùng khi có sample mới)."""
    try:
        conn = sqlite3.connect(BIAS_DB_FILE, timeout=10)
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO bias_history (api_temp, observed_temp, ts) VALUES (?, ?, ?)",
            (float(api_temp), float(observed_temp), int(time.time()))
        )
        conn.commit()
        conn.close()
    except Exception as e:
        logger.warning(f"Failed to insert bias history to DB: {e}")

# ================== THỜI GIAN HIỆN TẠI (LOCAL TZ) ==================
def _now_local():
    """Trả về datetime theo TIMEZONE nếu có zoneinfo, ngược lại trả datetime naive."""
    if ZoneInfo is not None:
        try:
            return datetime.now(ZoneInfo(TIMEZONE))
        except Exception:
            return datetime.now()
    return datetime.now()

# ================== HELPER NHỎ ==================
def _safe_mean(lst):
    return round(sum(lst) / len(lst), 1) if lst else None

def _find_nearest_index(times, target_iso):
    """Tìm index gần nhất với target_iso trong danh sách times (ISO strings)."""
    if not times:
        return None
    if target_iso in times:
        return times.index(target_iso)
    try:
        parsed = [datetime.fromisoformat(t) for t in times]
        target = datetime.fromisoformat(target_iso)
        diffs = [abs((p - target).total_seconds()) for p in parsed]
        return int(min(range(len(diffs)), key=lambda i: diffs[i]))
    except Exception:
        return 0

# ================== GỌI OPEN-METEO (forecast) ==================
def get_weather_forecast():
    """
    Gọi Open-Meteo, trả về cấu trúc:
    {
      meta: {...},
      yesterday: {...},
      today: {...},
      tomorrow: {...},
      next_hours: [ {time, temperature, humidity, weather_code, weather_desc, precipitation, precip_probability, windspeed, winddir}, ... ],
      humidity_yesterday, humidity_today, humidity_tomorrow
    }
    Có cache để giảm tần suất gọi API.
    """
    now = _now_local()
    if time.time() - weather_cache["ts"] < WEATHER_CACHE_SECONDS and weather_cache.get("data"):
        return weather_cache["data"]

    try:
        # start/end ngày để request daily summary; nếu bị lỗi sẽ thử lại không dùng start/end
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

        r = requests.get(url, params=params, timeout=12)
        # nếu status không phải 200, thử fallback (bỏ start_date/end_date) vì một số endpoint có vấn đề với date range
        if r.status_code != 200:
            logger.warning(f"Weather API non-200 ({r.status_code}). Body: {r.text}")
            try:
                params.pop("start_date", None)
                params.pop("end_date", None)
                r2 = requests.get(url, params=params, timeout=12)
                if r2.status_code == 200:
                    data = r2.json()
                    logger.info("Weather API: fallback parameters succeeded.")
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

        def sg(arr, idx):
            try:
                return arr[idx]
            except Exception:
                return None

        # Tính độ ẩm trung bình cho yesterday/today/tomorrow (nếu hourly có đủ)
        hums = hourly.get("relativehumidity_2m", [])
        humidity_yesterday = round(sum(hums[0:24]) / len(hums[0:24]), 1) if len(hums) >= 24 else None
        humidity_today = round(sum(hums[24:48]) / len(hums[24:48]), 1) if len(hums) >= 48 else None
        humidity_tomorrow = round(sum(hums[48:72]) / len(hums[48:72]), 1) if len(hums) >= 72 else None

        # Build daily summaries (yesterday/today/tomorrow) an toàn
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
                    "date": sg(daily.get("time", []), idx),
                    "desc": WEATHER_CODE_MAP.get(sg(daily.get("weathercode", []), idx), "?"),
                    "max": sg(daily.get("temperature_2m_max", []), idx),
                    "min": sg(daily.get("temperature_2m_min", []), idx),
                    "precipitation_sum": sg(daily.get("precipitation_sum", []), idx),
                    "windspeed_max": sg(daily.get("windspeed_10m_max", []), idx)
                }
                if name == "yesterday":
                    weather_yesterday.update(target)
                elif name == "today":
                    weather_today.update(target)
                else:
                    weather_tomorrow.update(target)

        # Lấy danh sách giờ tiếp theo - dùng EXTENDED_HOURS (mặc định 12)
        hour_times = hourly.get("time", [])
        current_hour_iso = now.replace(minute=0, second=0, microsecond=0).strftime("%Y-%m-%dT%H:00")
        idx = _find_nearest_index(hour_times, current_hour_iso) or 0

        next_hours = []
        for offset in range(0, EXTENDED_HOURS):
            i = idx + offset
            if i >= len(hour_times):
                break
            h = {
                "time": hour_times[i],
                "temperature": sg(hourly.get("temperature_2m", []), i),
                "humidity": sg(hourly.get("relativehumidity_2m", []), i),
                "weather_code": sg(hourly.get("weathercode", []), i),
                "weather_desc_simple": WEATHER_CODE_MAP.get(sg(hourly.get("weathercode", []), i), "?"),
                "precipitation": sg(hourly.get("precipitation", []), i),
                "precip_probability": sg(hourly.get("precipitation_probability", []), i),
                "windspeed": sg(hourly.get("windspeed_10m", []), i),
                "winddir": sg(hourly.get("winddirection_10m", []), i)
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
            logger.info("Trả về dữ liệu cache do lỗi API.")
            return weather_cache["data"]
        return {"meta": {}, "yesterday": {}, "today": {}, "tomorrow": {}, "next_hours": [], "humidity_yesterday": None, "humidity_today": None, "humidity_tomorrow": None}
    except Exception as e:
        logger.warning(f"Weather API unexpected error: {e}")
        if weather_cache.get("data"):
            return weather_cache["data"]
        return {"meta": {}, "yesterday": {}, "today": {}, "tomorrow": {}, "next_hours": [], "humidity_yesterday": None, "humidity_today": None, "humidity_tomorrow": None}

# ================== BIAS CORRECTION ==================
def update_bias_and_correct(next_hours, observed_temp):
    """
    Cập nhật history bias (api_now, observed_temp) -> tính bias = mean(obs - api).
    Trả về bias và next_hours không sửa fields nhiệt độ (KHÔNG thêm temperature_corrected).
    Lưu history cả vào SQLite.
    """
    global bias_history
    if not next_hours:
        return 0.0, next_hours

    api_now = next_hours[0].get("temperature")
    if api_now is not None and observed_temp is not None:
        try:
            bias_history.append((api_now, observed_temp))
            insert_history_to_db(api_now, observed_temp)
        except Exception:
            # Không phá flow nếu DB lỗi
            pass

    # compute diffs
    if bias_history:
        diffs = [obs - api for api, obs in bias_history if api is not None and obs is not None]
    else:
        diffs = []

    bias = round(sum(diffs) / len(diffs), 1) if diffs else 0.0

    # IMPORTANT: theo yêu cầu bạn, KHÔNG gửi hour_N_temperature_corrected lên ThingsBoard.
    # Vì vậy, ở đây chúng ta tính bias nội bộ nhưng không modify next_hours temperature fields.
    return bias, next_hours

# ================== LLM (OpenRouter/Gemini) ==================
def call_openrouter_llm(system_prompt: str, user_prompt: str, model: str = LLM_MODEL, max_tokens: int = 400, temperature: float = 0.0):
    """
    Gọi OpenRouter Chat Completions (nếu OPENROUTER_API_KEY được set).
    Nếu không set key, raise RuntimeError.
    """
    if not OPENROUTER_API_KEY:
        raise RuntimeError("OPENROUTER_API_KEY chưa được cấu hình trong environment")

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
    # Ném exception cho caller xử lý; caller sẽ bắt lỗi 402/other và backoff
    r.raise_for_status()
    data = r.json()
    try:
        # Cấu trúc dựa trên OpenRouter Chat completions
        content = data["choices"][0]["message"]["content"]
    except Exception:
        content = json.dumps(data, ensure_ascii=False)
    return content

def extract_json_like(text: str):
    """Cố gắng tìm JSON object trong string trả về bởi LLM và parse thành dict."""
    if not text:
        raise ValueError("Empty text")
    # Tìm block JSON đầu tiên
    m = re.search(r"\{[\s\S]*\}", text)
    if m:
        candidate = m.group(0)
        try:
            return json.loads(candidate)
        except Exception:
            pass
    # fallback: thử load toàn chuỗi
    try:
        return json.loads(text)
    except Exception as e:
        raise ValueError("No JSON found in LLM response") from e

# ================== AI HELPER (rule-based fallback) ==================
def get_advice_rule_based(temp, humi, upcoming_weather=None):
    """
    Nếu LLM không khả dụng, sử dụng luật đơn giản để trả advice.
    Trả dict chứa các key giống như telemetry: advice, advice_nutrition, advice_care, advice_note, prediction
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
        rain_expected = any(((h.get("precipitation") or 0) > 0.1) or ((h.get("precip_probability") or 0) >= 40) for h in upcoming_weather)
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

# ================== MỸ NGHĨA MÔ TẢ THỜI TIẾT TỰ NHIÊN ==================
def nice_hourly_description(h):
    """
    Tạo mô tả tự nhiên cho 1 mốc giờ dự báo.
    h là dict chứa: weather_desc_simple, precip_probability, precipitation, windspeed
    Trả chuỗi mô tả ngắn, ví dụ: "Nhiều mây, khả năng mưa ~40%, mưa nhỏ, gió nhẹ ~8 km/h"
    """
    parts = []
    desc = h.get("weather_desc_simple") or "Không rõ"
    parts.append(desc)

    pp = h.get("precip_probability")
    prec = h.get("precipitation")
    if pp is not None:
        try:
            pp_i = int(round(float(pp)))
            if pp_i > 0:
                parts.append(f"khả năng mưa ~{pp_i}%")
        except Exception:
            pass
    # nếu có lượng mưa
    if prec is not None and prec > 0.0:
        parts.append(f"lượng mưa ~{round(float(prec),1)} mm")
    # gió
    ws = h.get("windspeed")
    if ws is not None:
        try:
            ws_v = round(float(ws),1)
            if ws_v >= 20:
                parts.append(f"gió mạnh ~{ws_v} km/h")
            elif ws_v >= 8:
                parts.append(f"gió vừa ~{ws_v} km/h")
            elif ws_v > 0:
                parts.append(f"gió nhẹ ~{ws_v} km/h")
        except Exception:
            pass

    return ", ".join(parts)

# ================== SEND TO THINGSBOARD ==================
def send_to_thingsboard(payload: dict):
    """Gửi payload telemetry lên ThingsBoard device token (TB_DEVICE_URL)."""
    try:
        logger.info(f"TB ▶ sending payload keys: {list(payload.keys())}")
        r = requests.post(TB_DEVICE_URL, json=payload, timeout=10)
        logger.info(f"TB ◀ {r.status_code}")
    except Exception as e:
        logger.error(f"ThingsBoard push error: {e}")

# ================== MERGE WEATHER + FORMAT TELEMETRY ==================
def merge_weather_and_hours(existing_data=None):
    """
    Hợp nhất forecast với existing_data (sensor + advice + meta) → tạo dict phẳng để push.
    Giữ NGUYÊN tên key telemetry hiện có (hour_0, hour_0_temperature, hour_0_weather_desc,...).
    Không thêm các key *_temperature_corrected theo yêu cầu.
    """
    if existing_data is None:
        existing_data = {}
    weather = get_weather_forecast()
    now = _now_local()

    flattened = {**existing_data}

    # daily
    t = weather.get("today", {})
    tm = weather.get("tomorrow", {})
    y = weather.get("yesterday", {})
    if t:
        flattened["weather_today_desc"] = t.get("desc")
        flattened["weather_today_max"] = t.get("max")
        flattened["weather_today_min"] = t.get("min")
    if tm:
        flattened["weather_tomorrow_desc"] = tm.get("desc")
        flattened["weather_tomorrow_max"] = tm.get("max")
        flattened["weather_tomorrow_min"] = tm.get("min")
    if y:
        flattened["weather_yesterday_desc"] = y.get("desc")
        flattened["weather_yesterday_max"] = y.get("max")
        flattened["weather_yesterday_min"] = y.get("min")

    # humidity aggregated
    if weather.get("humidity_today") is not None:
        flattened["humidity_today"] = weather.get("humidity_today")
    else:
        hlist = [hh.get("humidity") for hh in weather.get("next_hours", []) if hh.get("humidity") is not None]
        flattened["humidity_today"] = round(sum(hlist)/len(hlist),1) if hlist else None

    if weather.get("humidity_tomorrow") is not None:
        flattened["humidity_tomorrow"] = weather.get("humidity_tomorrow")
    if weather.get("humidity_yesterday") is not None:
        flattened["humidity_yesterday"] = weather.get("humidity_yesterday")

    # hours: tạo keys hour_0 ... hour_{EXTENDED_HOURS-1}
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

        # NOTE: giữ nguyên tên key theo yêu cầu
        flattened[f"hour_{idx}"] = time_label
        if h and h.get("temperature") is not None:
            flattened[f"hour_{idx}_temperature"] = h.get("temperature")
        if h and h.get("humidity") is not None:
            flattened[f"hour_{idx}_humidity"] = h.get("humidity")

        # weather desc: dùng mô tả tự nhiên, không cộc lốc
        if h:
            flattened[f"hour_{idx}_weather_desc"] = nice_hourly_description(h)
        else:
            flattened[f"hour_{idx}_weather_desc"] = None

    # giữ các key quan trọng khác
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
    """Endpoint kiểm tra forecast đã xử lý (dùng để test)."""
    return get_weather_forecast()

@app.get("/bias")
def bias_status():
    """Trả trạng thái bias hiện tại (dùng để debug)."""
    diffs = [round(obs - api, 2) for api, obs in bias_history if api is not None and obs is not None]
    bias = round(sum(diffs) / len(diffs), 2) if diffs else 0.0
    return {"bias": bias, "history_len": len(diffs)}

@app.post("/esp32-data")
def receive_data(data: SensorData):
    """
    Điểm vào chính khi ESP32 POST dữ liệu sensor.
    - Cập nhật bias history
    - Tính advice rule-based
    - Gọi LLM (nếu bật và không đang backoff)
    - Hợp nhất weather và push lên ThingsBoard giữ nguyên tên key
    """
    logger.info(f"ESP32 ▶ received sensor data: {{'temperature':..., 'humidity':..., 'battery':...}}")
    weather = get_weather_forecast()
    next_hours = weather.get("next_hours", [])

    # update bias và lấy bias (không thay đổi next_hours fields)
    bias, updated_next_hours = update_bias_and_correct(next_hours, data.temperature)

    # advice cơ bản nếu LLM không hoạt động
    advice_data = get_advice_rule_based(data.temperature, data.humidity, upcoming_weather=updated_next_hours)

    merged = {
        **data.dict(),
        **advice_data,
        "location": "An Phú, Hồ Chí Minh",
        "crop": "Rau muống",
        "forecast_bias": bias,
        "forecast_history_len": len(bias_history)
    }

    # LLM: gọi nếu có key và không trong thời gian backoff
    llm_advice = None
    global llm_disabled_until
    now_ts = time.time()
    if OPENROUTER_API_KEY:
        if now_ts < llm_disabled_until:
            # đang backoff do lỗi trước đó
            llm_advice = {"skipped": "disabled/backoff_or_rate_or_history", "reason": f"llm_disabled_until={llm_disabled_until}"}
            logger.info("LLM bị skip do đang backoff.")
        else:
            try:
                # Hướng dẫn LLM trả về CHỈ JSON object ngắn gọn
                system_prompt = (
                    "Bạn là chuyên gia nông nghiệp, chuyên đưa khuyến nghị ngắn gọn bằng tiếng Việt. "
                    "Trả VỀ CHỈ MỘT ĐỐI TƯỢNG JSON duy nhất với các trường (tùy chọn): "
                    "advice (string ngắn, tiếng Việt), priority (low/medium/high), actions (mảng string các hành động cụ thể), reason (giải thích ngắn). "
                    "KHÔNG kèm văn bản khác."
                )
                user_prompt = (
                    f"Observed: temp={data.temperature}C, hum={data.humidity}%. Bias={bias}. "
                    f"Next hours (first {min(6, len(updated_next_hours))}): {json.dumps(updated_next_hours[:6], ensure_ascii=False)}. "
                    "Trả VỀ CHỈ JSON object ngắn gọn."
                )
                resp_text = call_openrouter_llm(system_prompt, user_prompt)
                try:
                    llm_json = extract_json_like(resp_text)
                    llm_advice = llm_json
                    # Nếu nhận dict, map 1 số field vào telemetry hiện có
                    if isinstance(llm_json, dict):
                        if llm_json.get("advice"):
                            merged["advice"] = llm_json.get("advice")
                        if llm_json.get("actions"):
                            merged["advice_care"] = " | ".join(llm_json.get("actions"))
                        if llm_json.get("priority"):
                            merged["advice_note"] = f"priority: {llm_json.get('priority')}"
                except ValueError:
                    # Không parse được JSON -> lưu raw text
                    llm_advice = {"raw": resp_text}
            except requests.HTTPError as he:
                # Xử lý lỗi HTTP (ví dụ 402 insufficient credits)
                try:
                    body = he.response.text if he.response is not None else str(he)
                except Exception:
                    body = str(he)
                logger.warning(f"LLM HTTP error: {he} - body: {body}")
                llm_advice = {"error": "http_error", "status": getattr(he.response, "status_code", None), "body": body}
                # Nếu 402 thì backoff
                status_code = getattr(he.response, "status_code", None)
                if status_code == 402:
                    llm_disabled_until = time.time() + LLM_BACKOFF_SECONDS_ON_402
                    logger.warning(f"LLM disabled for {LLM_BACKOFF_SECONDS_ON_402} seconds due to HTTP 402 (insufficient credits).")
            except Exception as e:
                logger.warning(f"LLM call failed: {e}")
                llm_advice = {"error": "llm_failed", "reason": str(e)}
    else:
        logger.info("OPENROUTER_API_KEY not set; skipping LLM call.")

    merged["llm_advice"] = llm_advice

    # attach next_hours (không sửa temp) vào weather để merge
    weather["next_hours"] = updated_next_hours
    merged = merge_weather_and_hours(existing_data=merged)

    send_to_thingsboard(merged)
    return {"received": data.dict(), "pushed": merged}

# ================== AUTO SIMULATOR (auto-loop) ==================
async def auto_loop():
    """
    Auto-loop để mô phỏng ESP32 gửi dữ liệu, hữu ích khi phát triển / demo.
    Nó cũng gọi LLM nếu bật (tuy nhiên sẽ dùng backoff khi LLM trả lỗi).
    """
    logger.info("Auto-loop simulator started (calls LLM if enabled).")
    battery = 4.2
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
            bias, updated_next_hours = update_bias_and_correct(weather.get("next_hours", []), sample["temperature"])
            advice_data = get_advice_rule_based(sample["temperature"], sample["humidity"], upcoming_weather=updated_next_hours)

            merged = {
                **sample,
                **advice_data,
                "location": "An Phú, Hồ Chí Minh",
                "crop": "Rau muống",
                "forecast_bias": bias,
                "forecast_history_len": len(bias_history)
            }

            # Gọi LLM tự động giống endpoint nếu enabled (có backoff)
            llm_advice = None
            if OPENROUTER_API_KEY and time.time() >= llm_disabled_until:
                try:
                    system_prompt = (
                        "Bạn là chuyên gia nông nghiệp. Trả VỀ CHỈ MỘT JSON ngắn gọn (advice, priority, actions, reason)."
                    )
                    user_prompt = (
                        f"Auto-sim sample: temp={sample['temperature']}C, hum={sample['humidity']}%. "
                        f"Next hours sample: {json.dumps(updated_next_hours[:6], ensure_ascii=False)}. Return JSON only."
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
                except requests.HTTPError as he:
                    body = he.response.text if he.response is not None else str(he)
                    logger.warning(f"LLM HTTP {getattr(he.response,'status_code',None)}: {body}")
                    llm_advice = {"error": "http_error", "status": getattr(he.response, "status_code", None), "body": body}
                    if getattr(he.response, "status_code", None) == 402:
                        # backoff
                        global llm_disabled_until
                        llm_disabled_until = time.time() + LLM_BACKOFF_SECONDS_ON_402
                        logger.warning(f"LLM disabled for {LLM_BACKOFF_SECONDS_ON_402} seconds due to 402.")
                except Exception as e:
                    logger.warning(f"LLM call failed in auto-loop: {e}")
                    llm_advice = {"error": "llm_failed", "reason": str(e)}
            else:
                if OPENROUTER_API_KEY:
                    llm_advice = {"skipped": "disabled/backoff_or_not_ready"}

            merged["llm_advice"] = llm_advice

            weather["next_hours"] = updated_next_hours
            merged = merge_weather_and_hours(existing_data=merged)
            send_to_thingsboard(merged)
        except Exception as e:
            logger.error(f"AUTO loop error: {e}")
        await asyncio.sleep(AUTO_LOOP_INTERVAL)

# ================== STARTUP ==================
@app.on_event("startup")
async def startup_event():
    # Init DB và load history (nếu có)
    init_db()
    load_history_from_db()
    # Start auto-loop task
    asyncio.create_task(auto_loop())

# ================== HƯỚNG DẪN NGẮN ==================
# - Chạy bằng: uvicorn main:app --host 0.0.0.0 --port $PORT
# - Configure env vars trên Render:
#   TB_DEMO_TOKEN, LAT, LON, AUTO_LOOP_INTERVAL, WEATHER_CACHE_SECONDS, TZ,
#   OPENROUTER_API_KEY (nếu muốn AI), GEMINI_API_KEY (không bắt buộc), EXTENDED_HOURS (mặc định 12)
# - Các key telemetry được giữ nguyên tên để không làm vỡ dashboard ThingsBoard.

# main.py
import os
import time
import json
import math
import random
import logging
import re
import requests
import asyncio
import sqlite3
from fastapi import FastAPI
from pydantic import BaseModel
from datetime import datetime, timedelta, timezone
from collections import deque, defaultdict

# zoneinfo if available
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

EXTENDED_HOURS = int(os.getenv("EXTENDED_HOURS", 12))

# Weather provider keys (set in Render env): OWM_API_KEY (OpenWeatherMap), WEATHER_API_KEY (WeatherAPI.com)
OWM_API_KEY = os.getenv("OWM_API_KEY")
WEATHER_API_KEY = os.getenv("WEATHER_API_KEY")

# Provider priority env (comma-separated) e.g. "owm,weatherapi,open-meteo"
PROVIDER_PRIORITY_ENV = os.getenv("PROVIDER_PRIORITY", "").strip()

# Bias correction / history settings
MAX_HISTORY = int(os.getenv("BIAS_MAX_HISTORY", 48))
bias_history = deque(maxlen=MAX_HISTORY)  # legacy global (keeps selected-provider entries for compatibility)
# per-provider in-memory deques
bias_history_per_provider = defaultdict(lambda: deque(maxlen=MAX_HISTORY))
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

# ================== WEATHER_CODE_MAP (simplified, Vietnamese) ==================
WEATHER_CODE_MAP = {
    0: "Nắng",
    1: "Nắng nhẹ",
    2: "Ít mây",
    3: "Nhiều mây",
    45: "Sương mù",
    48: "Sương muối/đóng băng",
    # Drizzle / light precipitation
    51: "Mưa phùn nhẹ",
    53: "Mưa phùn vừa",
    55: "Mưa phùn dày",
    56: "Mưa phùn lạnh",
    57: "Mưa phùn lạnh dày",
    # Rain
    61: "Mưa nhẹ",
    63: "Mưa vừa",
    65: "Mưa to",
    # Freezing rain variants
    66: "Mưa lạnh nhẹ",
    67: "Mưa lạnh to",
    # Showers
    80: "Mưa rào nhẹ",
    81: "Mưa rào vừa",
    82: "Mưa rào mạnh",
    # Thunder / storms (simplified)
    95: "Có dông",
    96: "Có dông",
    99: "Có dông"
}

# ----------------- English -> Vietnamese mapping (single map for OWM & WeatherAPI) -----------------
WEATHER_MAP = {
    # Sun / clear
    "Sunny": "Nắng",
    "Clear": "Trời quang",
    "Partly cloudy": "Ít mây",
    "Cloudy": "Nhiều mây",
    "Overcast": "Âm u",

    # Rain
    "Patchy light rain": "Mưa nhẹ",
    "Patchy rain nearby": "Có mưa rải rác gần đó",
    "Light rain": "Mưa nhẹ",
    "Light rain shower": "Mưa rào nhẹ",
    "Patchy light drizzle": "Mưa phùn nhẹ",
    "Moderate rain": "Mưa vừa",
    "Heavy rain": "Mưa to",
    "Moderate or heavy rain shower": "Mưa rào vừa hoặc to",
    "Torrential rain shower": "Mưa rất to",
    "Patchy rain possible": "Có thể có mưa",

    # Thunder
    "Thundery outbreaks possible": "Có dông",
    "Patchy light rain with thunder": "Mưa dông nhẹ",
    "Moderate or heavy rain with thunder": "Mưa dông to",

    # Storm / tropical
    "Storm": "Bão",
    "Tropical storm": "Áp thấp nhiệt đới",
}

weather_cache = {"ts": 0, "data": {}}

# ----------------- SQLite persistence for bias history -----------------
def init_db():
    """Create SQLite DB/tables if not exists."""
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
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS bias_history_provider (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                provider TEXT NOT NULL,
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
    """Load most recent MAX_HISTORY rows into memory: global bias_history and bias_history_per_provider."""
    try:
        conn = sqlite3.connect(BIAS_DB_FILE)
        cur = conn.cursor()
        # load global legacy table
        cur.execute("SELECT api_temp, observed_temp FROM bias_history ORDER BY id DESC LIMIT ?", (MAX_HISTORY,))
        rows = cur.fetchall()
        rows.reverse()
        for api, obs in rows:
            bias_history.append((float(api), float(obs)))
        logger.info(f"Loaded {len(rows)} legacy bias_history samples from DB")

        # load provider table - load recent MAX_HISTORY per provider
        cur.execute("SELECT provider, api_temp, observed_temp FROM bias_history_provider ORDER BY id DESC LIMIT ?", (MAX_HISTORY * 10,))
        rows2 = cur.fetchall()
        rows2.reverse()
        for provider, api, obs in rows2:
            if provider is None:
                continue
            bias_history_per_provider[provider].append((float(api), float(obs)))
        logger.info(f"Loaded per-provider bias history for {len(bias_history_per_provider)} providers from DB")
    except Exception as e:
        logger.warning(f"Failed to load bias history from DB: {e}")
    finally:
        try:
            conn.close()
        except Exception:
            pass


def insert_history_to_db(api_temp, observed_temp, provider: str | None = None):
    """Insert one history row into DB. If provider provided, insert into provider table too."""
    try:
        conn = sqlite3.connect(BIAS_DB_FILE, timeout=10)
        cur = conn.cursor()
        # insert into legacy table (for backwards compat)
        cur.execute(
            "INSERT INTO bias_history (api_temp, observed_temp, ts) VALUES (?, ?, ?)",
            (float(api_temp), float(observed_temp), int(time.time()))
        )
        if provider:
            cur.execute(
                "INSERT INTO bias_history_provider (provider, api_temp, observed_temp, ts) VALUES (?, ?, ?, ?)",
                (str(provider), float(api_temp), float(observed_temp), int(time.time()))
            )
        conn.commit()
    except Exception as e:
        logger.warning(f"Failed to insert bias history to DB: {e}")
    finally:
        try:
            conn.close()
        except Exception:
            pass

# ----------------- Helpers -----------------
def _now_local():
    if ZoneInfo is not None:
        try:
            return datetime.now(ZoneInfo(TIMEZONE))
        except Exception:
            return datetime.now()
    return datetime.now()


def _mean(lst):
    return round(sum(lst) / len(lst), 1) if lst else None


def _normalize_text(s: str) -> str:
    if not s:
        return s
    # remove parenthetical ranges like "(24.7–32.0°C)" or "(24.7 - 32.0°C)"
    s = re.sub(r"\([^)]*\d{1,2}[.,]?\d*°?[CF]?.*?\)", "", s)
    s = s.strip()
    s = re.sub(r"\s+", " ", s)
    return s


PARTIAL_MAP = [
    (r"patchy rain nearby", "Có mưa rải rác gần đó"),
    (r"patchy.*rain", "Có mưa rải rác"),
    (r"patchy.*drizzle", "Mưa phùn nhẹ"),
    (r"light drizzle", "Mưa phùn nhẹ"),
    (r"light rain shower", "Mưa rào nhẹ"),
    (r"rain shower", "Mưa rào"),
    (r"heavy rain", "Mưa to"),
    (r"thunder", "Có dông"),
    (r"storm", "Bão"),
    (r"cloudy", "Nhiều mây"),
    (r"partly cloudy", "Ít mây"),
    (r"clear", "Trời quang"),
    (r"sunny", "Nắng"),
]


def translate_desc(desc_raw):
    if not desc_raw:
        return None
    cleaned = _normalize_text(desc_raw)
    if not cleaned:
        return None
    # exact match (case-insensitive)
    for k, v in WEATHER_MAP.items():
        if k.lower() == cleaned.lower():
            return v
    # partial regex fallback
    low = cleaned.lower()
    for pat, mapped in PARTIAL_MAP:
        if re.search(pat, low):
            return mapped
    return cleaned  # fallback to original cleaned string


def _nice_weather_desc(base_phrase: str, precip: float | None, precip_prob: float | None, windspeed: float | None):
    parts = []
    if base_phrase:
        parts.append(base_phrase)

    if precip_prob is not None:
        try:
            pp = int(round(float(precip_prob)))
            if pp > 0:
                parts.append(f"khả năng mưa ~{pp}%")
        except Exception:
            pass

    if precip is not None:
        try:
            p = float(precip)
            if p > 0.0:
                parts.append(f"lượng mưa ~{round(p,1)} mm")
        except Exception:
            pass

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

    if not parts:
        return base_phrase or "Không có dữ liệu"
    s = ", ".join(parts)
    return s[0].upper() + s[1:]


# ---------- compute daily min/max from hourly ------------
def _normalize_time_str(t):
    if not t:
        return None
    try:
        return datetime.fromisoformat(t)
    except Exception:
        try:
            return datetime.strptime(t, "%Y-%m-%d %H:%M")
        except Exception:
            return None


def compute_daily_min_max_from_hourly(hourly_list, target_date_str):
    temps = []
    for h in hourly_list:
        t = h.get("time")
        temp = h.get("temperature")
        if t and temp is not None:
            dt = _normalize_time_str(t)
            if dt is None:
                continue
            if dt.date().isoformat() == target_date_str:
                try:
                    temps.append(float(temp))
                except Exception:
                    pass
    if not temps:
        return None, None
    return round(min(temps), 1), round(max(temps), 1)


# ----------------- timezone helpers & api_now selection -----------------
def _to_zone(dt: datetime):
    """Return timezone-aware datetime in configured TIMEZONE."""
    if ZoneInfo is not None:
        tz = ZoneInfo(TIMEZONE)
    else:
        tz = timezone.utc
    if dt is None:
        return None
    if dt.tzinfo is None:
        # assume UTC for naive datetimes
        return dt.replace(tzinfo=timezone.utc).astimezone(tz)
    return dt.astimezone(tz)


def find_closest_api_now(hourly_list):
    """Normalize times to TIMEZONE and pick temperature whose hour is closest to local now (hour granularity)."""
    if not hourly_list:
        return None
    # build now in target tz (rounded to hour)
    if ZoneInfo is not None:
        now_local = datetime.now(ZoneInfo(TIMEZONE)).replace(minute=0, second=0, microsecond=0)
    else:
        now_local = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    best = None
    min_diff = float("inf")
    for h in hourly_list:
        t = h.get("time")
        if not t:
            continue
        dt = None
        try:
            # ISO with offset or 'Z' -> yields tz-aware
            dt = datetime.fromisoformat(t)
        except Exception:
            try:
                dt = datetime.strptime(t, "%Y-%m-%d %H:%M")
                # assume this string is local to TIMEZONE
                if ZoneInfo is not None:
                    dt = dt.replace(tzinfo=ZoneInfo(TIMEZONE))
                else:
                    dt = dt.replace(tzinfo=timezone.utc)
            except Exception:
                continue
        try:
            dt_local = _to_zone(dt)
        except Exception:
            continue
        diff = abs((dt_local - now_local).total_seconds())
        if diff < min_diff:
            min_diff = diff
            best = h
    if best:
        return best.get("temperature")
    return None


# ----------------- Provider ordering (env or dynamic) -----------------
def compute_mean_abs_error_for_provider(provider: str):
    dq = bias_history_per_provider.get(provider, None)
    if not dq or len(dq) == 0:
        return None
    errors = [abs(obs - api) for api, obs in dq if api is not None and obs is not None]
    return round(sum(errors) / len(errors), 2) if errors else None


def get_provider_order():
    """Return ordered list of providers to try (best -> worst).
    Priority from env > dynamic ranking by mean abs error (lower is better) > default fallback.
    """
    possible = ["owm", "weatherapi", "open-meteo"]
    if PROVIDER_PRIORITY_ENV:
        order = [p.strip() for p in PROVIDER_PRIORITY_ENV.split(",") if p.strip() in possible]
        for p in possible:
            if p not in order:
                order.append(p)
        return order

    scores = {}
    for p in possible:
        m = compute_mean_abs_error_for_provider(p)
        scores[p] = m if m is not None else float("inf")
    ordered = sorted(possible, key=lambda x: (scores.get(x, float("inf")), x))
    return ordered


# ================== WEATHER FETCHER (with history support) ==================
def fetch_owm_with_history():
    """Return (daily_list, hourly_list, has_yesterday, api_now_temp) or None"""
    if not OWM_API_KEY:
        return None
    try:
        url = "https://api.openweathermap.org/data/2.5/onecall"
        params = {"lat": LAT, "lon": LON, "exclude": "minutely,alerts", "appid": OWM_API_KEY, "units": "metric", "lang": "en"}
        r = requests.get(url, params=params, timeout=12)
        r.raise_for_status()
        data = r.json()

        daily_list = []
        for d in data.get("daily", [])[:7]:
            date = datetime.utcfromtimestamp(d.get("dt") or 0).strftime("%Y-%m-%d")
            desc_raw = None
            weather = d.get("weather")
            if weather and isinstance(weather, list) and len(weather) > 0:
                desc_raw = weather[0].get("description")
            desc = translate_desc(desc_raw)
            daily_list.append({
                "date": date,
                "desc": desc,
                "max": d.get("temp", {}).get("max"),
                "min": d.get("temp", {}).get("min"),
                "precipitation_sum": (d.get("rain") or 0) + (d.get("snow") or 0),
                "windspeed_max": d.get("wind_speed")
            })

        hourly_list = []
        for h in data.get("hourly", [])[:96]:
            # make tz-aware ISO (OWM dt is UNIX UTC)
            t_iso = datetime.utcfromtimestamp(h.get("dt") or 0).replace(tzinfo=timezone.utc).isoformat()
            desc_raw = None
            weather = h.get("weather")
            if weather and isinstance(weather, list) and len(weather) > 0:
                desc_raw = weather[0].get("description")
            desc = translate_desc(desc_raw)
            precip = 0
            if isinstance(h.get("rain"), dict):
                precip = h.get("rain").get("1h", 0)
            elif isinstance(h.get("snow"), dict):
                precip = h.get("snow").get("1h", 0)
            hourly_list.append({
                "time": t_iso,
                "temperature": h.get("temp"),
                "humidity": h.get("humidity"),
                "weather_desc": desc,
                "precipitation": precip,
                "precip_probability": (h.get("pop") * 100) if h.get("pop") is not None else None,
                "windspeed": h.get("wind_speed"),
                "winddir": h.get("wind_deg")
            })

        yesterday_str = (datetime.utcnow() - timedelta(days=1)).strftime("%Y-%m-%d")
        has_yesterday = any(d.get("date") == yesterday_str for d in daily_list)

        need_history = (not has_yesterday) or not any(d for d in daily_list if d.get("date") == yesterday_str and d.get("min") is not None and d.get("max") is not None)

        if need_history:
            try:
                dt_mid = int((datetime.utcnow() - timedelta(days=1)).replace(hour=12, minute=0, second=0, microsecond=0).timestamp())
                tm_url = "https://api.openweathermap.org/data/2.5/onecall/timemachine"
                tm_params = {"lat": LAT, "lon": LON, "dt": dt_mid, "appid": OWM_API_KEY, "units": "metric"}
                rt = requests.get(tm_url, params=tm_params, timeout=12)
                if rt.status_code == 200:
                    td = rt.json()
                    hist_hours = td.get("hourly", []) or td.get("data", [])
                    for h in reversed(hist_hours):
                        t_iso = datetime.utcfromtimestamp(h.get("dt") or 0).replace(tzinfo=timezone.utc).isoformat()
                        desc_raw = None
                        weather = h.get("weather")
                        if weather and isinstance(weather, list) and len(weather) > 0:
                            desc_raw = weather[0].get("description")
                        desc = translate_desc(desc_raw)
                        precip = 0
                        if isinstance(h.get("rain"), dict):
                            precip = h.get("rain").get("1h", 0)
                        hourly_list.insert(0, {
                            "time": t_iso,
                            "temperature": h.get("temp"),
                            "humidity": h.get("humidity"),
                            "weather_desc": desc,
                            "precipitation": precip,
                            "precip_probability": None,
                            "windspeed": h.get("wind_speed"),
                            "winddir": h.get("wind_deg")
                        })
                    y_min, y_max = compute_daily_min_max_from_hourly(hourly_list, yesterday_str)
                    if y_min is not None or y_max is not None:
                        found = False
                        for dd in daily_list:
                            if dd.get("date") == yesterday_str:
                                dd["min"] = dd.get("min") or y_min
                                dd["max"] = dd.get("max") or y_max
                                found = True
                                break
                        if not found:
                            daily_list.insert(0, {"date": yesterday_str, "desc": None, "max": y_max, "min": y_min, "precipitation_sum": None, "windspeed_max": None})
                        has_yesterday = True
                else:
                    logger.info(f"OWM timemachine returned {rt.status_code} (may be unsupported by plan): {rt.text[:200]}")
            except Exception as e:
                logger.info(f"OWM timemachine error: {e}")

        # compute api_now_temp using timezone-aware selection
        api_now_temp = find_closest_api_now(hourly_list)

        return daily_list, hourly_list, has_yesterday, api_now_temp

    except Exception as e:
        logger.warning(f"fetch_owm_with_history error: {e}")
        return None


def fetch_weatherapi_with_history():
    """Return (daily_list, hourly_list, has_yesterday, api_now_temp) or None"""
    if not WEATHER_API_KEY:
        return None
    try:
        url = "http://api.weatherapi.com/v1/forecast.json"
        params = {"key": WEATHER_API_KEY, "q": f"{LAT},{LON}", "days": 3, "aqi": "no", "alerts": "no"}
        r = requests.get(url, params=params, timeout=12)
        r.raise_for_status()
        data = r.json()

        daily_list = []
        hourly_list = []
        for d in data.get("forecast", {}).get("forecastday", []):
            date = d.get("date")
            day = d.get("day", {})
            desc_raw = day.get("condition", {}).get("text")
            desc = translate_desc(desc_raw)
            daily_list.append({
                "date": date,
                "desc": desc,
                "max": day.get("maxtemp_c"),
                "min": day.get("mintemp_c"),
                "precipitation_sum": day.get("totalprecip_mm"),
                "windspeed_max": day.get("maxwind_kph")
            })
            for h in d.get("hour", []):
                t_raw = h.get("time")
                try:
                    dt = datetime.strptime(t_raw, "%Y-%m-%d %H:%M")
                    # WeatherAPI hour strings are local to the location -> mark with TIMEZONE
                    if ZoneInfo is not None:
                        dt = dt.replace(tzinfo=ZoneInfo(TIMEZONE))
                    else:
                        dt = dt.replace(tzinfo=timezone.utc)
                    t_iso = dt.isoformat()
                except Exception:
                    t_iso = t_raw
                desc_raw = h.get("condition", {}).get("text")
                desc_h = translate_desc(desc_raw)
                hourly_list.append({
                    "time": t_iso,
                    "temperature": h.get("temp_c"),
                    "humidity": h.get("humidity"),
                    "weather_desc": desc_h,
                    "precipitation": h.get("precip_mm"),
                    "precip_probability": h.get("chance_of_rain") if h.get("chance_of_rain") is not None else None,
                    "windspeed": h.get("wind_kph"),
                    "winddir": h.get("wind_degree")
                })

        yesterday_str = (datetime.utcnow() - timedelta(days=1)).strftime("%Y-%m-%d")
        has_yesterday = any(d.get("date") == yesterday_str for d in daily_list)

        need_history = (not has_yesterday) or not any(d for d in daily_list if d.get("date")==yesterday_str and d.get("min") is not None and d.get("max") is not None)
        if need_history:
            try:
                hist_url = "http://api.weatherapi.com/v1/history.json"
                hist_params = {"key": WEATHER_API_KEY, "q": f"{LAT},{LON}", "dt": yesterday_str}
                rh = requests.get(hist_url, params=hist_params, timeout=12)
                if rh.status_code == 200:
                    hd = rh.json()
                    for fd in hd.get("forecast", {}).get("forecastday", []):
                        for h in reversed(fd.get("hour", [])):
                            t_raw = h.get("time")
                            try:
                                dt = datetime.strptime(t_raw, "%Y-%m-%d %H:%M")
                                if ZoneInfo is not None:
                                    dt = dt.replace(tzinfo=ZoneInfo(TIMEZONE))
                                else:
                                    dt = dt.replace(tzinfo=timezone.utc)
                                t_iso = dt.isoformat()
                            except Exception:
                                t_iso = t_raw
                            desc_raw = h.get("condition", {}).get("text")
                            desc_h = translate_desc(desc_raw)
                            hourly_list.insert(0, {
                                "time": t_iso,
                                "temperature": h.get("temp_c"),
                                "humidity": h.get("humidity"),
                                "weather_desc": desc_h,
                                "precipitation": h.get("precip_mm"),
                                "precip_probability": None,
                                "windspeed": h.get("wind_kph"),
                                "winddir": h.get("wind_degree")
                            })
                    y_min, y_max = compute_daily_min_max_from_hourly(hourly_list, yesterday_str)
                    if y_min is not None or y_max is not None:
                        found = False
                        for dd in daily_list:
                            if dd.get("date")==yesterday_str:
                                dd["min"] = dd.get("min") or y_min
                                dd["max"] = dd.get("max") or y_max
                                found = True
                                break
                        if not found:
                            daily_list.insert(0, {"date": yesterday_str, "desc": None, "max": y_max, "min": y_min, "precipitation_sum": None, "windspeed_max": None})
                        has_yesterday = True
                else:
                    logger.info(f"WeatherAPI history returned {rh.status_code}: {rh.text[:200]}")
            except Exception as e:
                logger.info(f"WeatherAPI history error: {e}")

        api_now_temp = find_closest_api_now(hourly_list)
        return daily_list, hourly_list, has_yesterday, api_now_temp

    except Exception as e:
        logger.warning(f"fetch_weatherapi_with_history error: {e}")
        return None


# Open-Meteo (existing fallback) - returns api_now_temp similarly
def fetch_open_meteo():
    try:
        now = _now_local()
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
                "temperature_2m", "relativehumidity_2m", "weathercode",
                "precipitation", "windspeed_10m", "winddirection_10m", "precipitation_probability"
            ]),
            "timezone": TIMEZONE,
            "start_date": start_date,
            "end_date": end_date
        }
        r = requests.get(url, params=params, timeout=12)
        r.raise_for_status()
        data = r.json()
        daily = data.get("daily", {})
        hourly = data.get("hourly", {})

        def safe_get(arr, idx):
            try:
                return arr[idx]
            except Exception:
                return None

        daily_list = []
        if "time" in daily and daily["time"]:
            for idx in range(len(daily.get("time", []))):
                code = safe_get(daily.get("weathercode", []), idx)
                desc = WEATHER_CODE_MAP.get(code) if code is not None else None
                daily_list.append({
                    "date": safe_get(daily.get("time", []), idx),
                    "desc": desc,
                    "max": safe_get(daily.get("temperature_2m_max", []), idx),
                    "min": safe_get(daily.get("temperature_2m_min", []), idx),
                    "precipitation_sum": safe_get(daily.get("precipitation_sum", []), idx),
                    "windspeed_max": safe_get(daily.get("windspeed_10m_max", []), idx)
                })

        hour_times = hourly.get("time", [])
        hourly_list = []
        for i in range(0, min(len(hour_times), 96)):
            t = hour_times[i]
            code = safe_get(hourly.get("weathercode", []), i)
            base_desc = WEATHER_CODE_MAP.get(code) if code is not None else None
            # Open-Meteo returned times are in timezone requested; ensure ISO format
            try:
                # If it's already ISO, fromisoformat handles it; else keep string
                dt = datetime.fromisoformat(t)
                t_iso = dt.isoformat()
            except Exception:
                t_iso = t
            hourly_list.append({
                "time": t_iso,
                "temperature": safe_get(hourly.get("temperature_2m", []), i),
                "humidity": safe_get(hourly.get("relativehumidity_2m", []), i),
                "weather_desc": _nice_weather_desc(base_desc,
                                                   safe_get(hourly.get("precipitation", []), i),
                                                   safe_get(hourly.get("precipitation_probability", []), i),
                                                   safe_get(hourly.get("windspeed_10m", []), i)),
                "precipitation": safe_get(hourly.get("precipitation", []), i),
                "precip_probability": safe_get(hourly.get("precipitation_probability", []), i),
                "windspeed": safe_get(hourly.get("windspeed_10m", []), i),
                "winddir": safe_get(hourly.get("winddirection_10m", []), i)
            })

        api_now_temp = find_closest_api_now(hourly_list)
        yesterday_str = (datetime.utcnow() - timedelta(days=1)).strftime("%Y-%m-%d")
        has_yesterday = any(d.get("date") == yesterday_str for d in daily_list)

        return daily_list, hourly_list, has_yesterday, api_now_temp

    except Exception as e:
        logger.warning(f"fetch_open_meteo error: {e}")
        return None


# ================== MAIN get_weather_forecast (uses provider order + captures per-provider now temps) ==================
def get_weather_forecast():
    now = _now_local()
    if time.time() - weather_cache["ts"] < WEATHER_CACHE_SECONDS and weather_cache.get("data"):
        return weather_cache["data"]

    yesterday_str = (now - timedelta(days=1)).strftime("%Y-%m-%d")
    today_str = now.strftime("%Y-%m-%d")

    def build_result_from_generic(daily_list, hourly_list, source_name="", providers_now=None):
        hums = [h.get("humidity") for h in hourly_list if h.get("humidity") is not None]
        humidity_yesterday = round(sum(hums[0:24]) / len(hums[0:24]), 1) if len(hums) >= 24 else None
        humidity_today = round(sum(hums[24:48]) / len(hums[24:48]), 1) if len(hums) >= 48 else None
        humidity_tomorrow = round(sum(hums[48:72]) / len(hums[48:72]), 1) if len(hums) >= 72 else None

        # ensure yesterday entry present
        if not any(d.get("date") == yesterday_str for d in daily_list):
            ymin, ymax = compute_daily_min_max_from_hourly(hourly_list, yesterday_str)
            if ymin is not None or ymax is not None:
                daily_list.insert(0, {"date": yesterday_str, "desc": None, "max": ymax, "min": ymin, "precipitation_sum": None, "windspeed_max": None})

        idx_today = 0
        for i, d in enumerate(daily_list):
            if d.get("date") == today_str:
                idx_today = i
                break

        def safe_get_daily(idx):
            if 0 <= idx < len(daily_list):
                d = daily_list[idx]
                return {
                    "date": d.get("date"),
                    "desc": d.get("desc"),
                    "max": d.get("max"),
                    "min": d.get("min"),
                    "precipitation_sum": d.get("precipitation_sum"),
                    "windspeed_max": d.get("windspeed_max")
                }
            return {"date": None, "desc": None, "max": None, "min": None, "precipitation_sum": None, "windspeed_max": None}

        weather_yesterday = safe_get_daily(idx_today - 1)
        weather_today = safe_get_daily(idx_today)
        weather_tomorrow = safe_get_daily(idx_today + 1)

        result = {
            "meta": {"latitude": LAT, "longitude": LON, "tz": TIMEZONE, "fetched_at": now.isoformat(), "source": source_name, "providers_now": providers_now or {}},
            "yesterday": weather_yesterday,
            "today": weather_today,
            "tomorrow": weather_tomorrow,
            "next_hours": hourly_list,
            "humidity_yesterday": humidity_yesterday,
            "humidity_today": humidity_today,
            "humidity_tomorrow": humidity_tomorrow
        }
        return result

    provider_order = get_provider_order()
    fetchers = {
        "owm": fetch_owm_with_history,
        "weatherapi": fetch_weatherapi_with_history,
        "open-meteo": fetch_open_meteo
    }

    selected_result = None
    selected_provider = None
    providers_now = {}
    for p in provider_order:
        fn = fetchers.get(p)
        if not fn:
            continue
        try:
            out = fn()
            if not out:
                continue
            if len(out) == 4:
                daily_list, hourly_list, has_yday, api_now_temp = out
            else:
                daily_list, hourly_list = out[0], out[1]
                has_yday = False
                api_now_temp = None

            providers_now[p] = api_now_temp
            logger.info(f"provider={p} api_now={api_now_temp} (local now={datetime.now(ZoneInfo(TIMEZONE)) if ZoneInfo else datetime.now(timezone.utc)})")
            if selected_result is None:
                selected_result = build_result_from_generic(daily_list, hourly_list, source_name=p, providers_now=providers_now)
                selected_provider = p
                if has_yday:
                    break
        except Exception as e:
            logger.warning(f"fetcher {p} error: {e}")
            continue

    if not selected_result:
        try:
            out = fetch_open_meteo()
            if out and len(out) == 4:
                daily_list, hourly_list, has_yday, api_now_temp = out
                providers_now["open-meteo"] = api_now_temp
                selected_result = build_result_from_generic(daily_list, hourly_list, source_name="open-meteo", providers_now=providers_now)
                selected_provider = "open-meteo"
        except Exception as e:
            logger.warning(f"Open-Meteo fallback error: {e}")

    if not selected_result:
        selected_result = {"meta": {"latitude": LAT, "longitude": LON, "tz": TIMEZONE, "fetched_at": now.isoformat(), "source": None, "providers_now": providers_now}, "yesterday": {}, "today": {}, "tomorrow": {}, "next_hours": [], "humidity_yesterday": None, "humidity_today": None, "humidity_tomorrow": None}
        selected_provider = None

    if isinstance(selected_result, dict):
        selected_result["meta"]["chosen_provider"] = selected_provider
        selected_result["meta"]["providers_now"] = providers_now

    weather_cache["data"] = selected_result
    weather_cache["ts"] = time.time()
    return selected_result


# ================== BIAS CORRECTION / RECORDING PER-PROVIDER ==================
def record_provider_errors_and_compute_bias(observed_temp, weather_meta):
    providers_now = weather_meta.get("providers_now", {}) if weather_meta else {}
    chosen = weather_meta.get("chosen_provider")
    for p, api_temp in providers_now.items():
        if api_temp is None:
            continue
        try:
            bias_history_per_provider[p].append((float(api_temp), float(observed_temp)))
            insert_history_to_db(float(api_temp), float(observed_temp), provider=p)
        except Exception:
            pass

    if chosen:
        dq = bias_history_per_provider.get(chosen, None)
        if dq and len(dq) > 0:
            diffs = [obs - api for api, obs in dq if api is not None and obs is not None]
            bias = round(sum(diffs) / len(diffs), 2) if diffs else 0.0
            return bias, len(dq), chosen
    if bias_history:
        diffs = [obs - api for api, obs in bias_history if api is not None and obs is not None]
        bias = round(sum(diffs) / len(diffs), 2) if diffs else 0.0
        return bias, len(bias_history), None
    return 0.0, 0, None


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

    # daily
    if weather.get("today"):
        t = weather.get("today")
        if t.get("desc") is not None:
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

    # hours
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
    providers = list(bias_history_per_provider.keys())
    stats = {}
    for p in providers:
        m = compute_mean_abs_error_for_provider(p)
        stats[p] = {"mean_abs_error": m, "history_len": len(bias_history_per_provider[p])}
    diffs = [round(abs(obs - api), 2) for api, obs in bias_history if api is not None and obs is not None]
    legacy_bias = round(sum(diffs) / len(diffs), 2) if diffs else 0.0
    return {"legacy_bias": legacy_bias, "legacy_history_len": len(diffs), "per_provider": stats}


@app.post("/esp32-data")
def receive_data(data: SensorData):
    logger.info(f"ESP32 ▶ received sensor data: {{'temperature':..., 'humidity':..., 'battery':...}}")
    weather = get_weather_forecast()
    next_hours = weather.get("next_hours", [])

    bias, history_len, chosen_provider = record_provider_errors_and_compute_bias(data.temperature, weather.get("meta", {}))

    chosen_api_now = None
    if weather.get("meta") and weather["meta"].get("providers_now"):
        chosen_api_now = weather["meta"]["providers_now"].get(chosen_provider)
    if chosen_api_now is not None:
        try:
            bias_history.append((float(chosen_api_now), float(data.temperature)))
            insert_history_to_db(float(chosen_api_now), float(data.temperature), provider=chosen_provider)
        except Exception:
            pass

    advice_data = get_advice(data.temperature, data.humidity, upcoming_weather=next_hours)

    merged = {
        **data.dict(),
        **advice_data,
        "location": "An Phú, Hồ Chí Minh",
        "crop": "Rau muống",
        "forecast_bias": bias,
        "forecast_history_len": history_len
    }

    merged["llm_advice"] = None

    merged = merge_weather_and_hours(existing_data=merged)
    send_to_thingsboard(merged)
    return {"received": data.dict(), "pushed": merged}


# ================== AUTO LOOP (simulator) ==================
async def auto_loop():
    logger.info("Auto-loop simulator started")
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
            if weather.get("meta"):
                record_provider_errors_and_compute_bias(sample["temperature"], weather.get("meta", {}))

            advice_data = get_advice(sample["temperature"], sample["humidity"], upcoming_weather=weather.get("next_hours", []))

            merged = {
                **sample,
                **advice_data,
                "location": "An Phú, Hồ Chí Minh",
                "crop": "Rau muống",
                "forecast_bias": 0.0,
                "forecast_history_len": len(bias_history)
            }

            merged["llm_advice"] = None

            merged = merge_weather_and_hours(existing_data=merged)
            send_to_thingsboard(merged)
            tick += 1
        except Exception as e:
            logger.error(f"AUTO loop error: {e}")
        await asyncio.sleep(AUTO_LOOP_INTERVAL)


@app.on_event("startup")
async def startup():
    init_db()
    load_history_from_db()
    asyncio.create_task(auto_loop())


# ================== NOTES ==================
# - Run with: uvicorn main:app --host 0.0.0.0 --port $PORT
# - Ensure OWM_API_KEY and/or WEATHER_API_KEY are set in environment; Open-Meteo fallback used if others fail.
# - Set PROVIDER_PRIORITY env (optional) to e.g. "owm,weatherapi,open-meteo" to force order.
# - This version: timezone-aware api_now selection (closest local hour) + per-provider bias persistence.

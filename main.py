import os
import requests
from fastapi import FastAPI
from apscheduler.schedulers.background import BackgroundScheduler
from datetime import datetime, timezone
from fastapi.responses import JSONResponse

# === Config từ Environment Render ===
OWM_API_KEY = os.getenv("OPENWEATHER_API_KEY")   # API key OpenWeather (có thể None)
THINGSBOARD_TOKEN = os.getenv("THINGSBOARD_TOKEN")  # Device token ThingsBoard (bắt buộc để push)
LAT = os.getenv("LAT", "10.806094263669602")
LON = os.getenv("LON", "106.75222004270555")
CROP = os.getenv("CROP", "Rau muống")

THINGSBOARD_URL = f"https://thingsboard.cloud/api/v1/{THINGSBOARD_TOKEN}/telemetry"

app = FastAPI()
scheduler = BackgroundScheduler()

def _log(msg):
    print(f"[{datetime.now().isoformat()}] {msg}")

def _om_weathercode_to_desc(code: int) -> str:
    # Bản dịch đơn giản các WMO weather codes phổ biến
    mapping = {
        0: "Clear sky",
        1: "Mainly clear",
        2: "Partly cloudy",
        3: "Overcast",
        45: "Fog",
        48: "Depositing rime fog",
        51: "Light drizzle",
        53: "Moderate drizzle",
        55: "Dense drizzle",
        61: "Slight rain",
        63: "Moderate rain",
        65: "Heavy rain",
        71: "Slight snow",
        73: "Moderate snow",
        75: "Heavy snow",
        80: "Rain showers",
        81: "Moderate rain showers",
        82: "Violent rain showers",
        95: "Thunderstorm",
        96: "Thunderstorm with slight hail",
        99: "Thunderstorm with heavy hail",
    }
    try:
        return mapping.get(int(code), "n/a")
    except Exception:
        return "n/a"

def fetch_weather():
    try:
        # ==== OpenWeather (nếu có key) ====
        if OWM_API_KEY:
            url = (
                f"https://api.openweathermap.org/data/2.5/onecall"
                f"?lat={LAT}&lon={LON}&units=metric&lang=vi&appid={OWM_API_KEY}"
            )
            r = requests.get(url, timeout=10)
            if r.status_code == 401:
                _log("OpenWeather returned 401 Unauthorized — API key invalid.")
                raise requests.HTTPError("401 Unauthorized from OpenWeather")
            r.raise_for_status()
            data = r.json()

            city_url = f"https://api.openweathermap.org/data/2.5/weather?lat={LAT}&lon={LON}&appid={OWM_API_KEY}&lang=vi"
            city_name = "Unknown"
            try:
                city_resp = requests.get(city_url, timeout=8)
                city_resp.raise_for_status()
                city_name = city_resp.json().get("name", "Unknown")
            except Exception:
                _log("Không lấy được tên thành phố từ OpenWeather, dùng 'Unknown'.")

            now = datetime.now()
            current_hour = now.hour

            current = {
                "hour": current_hour,
                "temp": data["current"]["temp"],
                "humidity": data["current"]["humidity"],
                "weather": data["current"]["weather"][0]["description"],
            }

            today_forecast = []
            for h in data.get("hourly", []):
                hour = datetime.fromtimestamp(h["dt"]).hour
                if hour >= current_hour:
                    today_forecast.append({
                        "hour": hour,
                        "temp": h["temp"],
                        "humidity": h["humidity"],
                        "weather": h["weather"][0]["description"],
                    })

            tomorrow = {}
            if len(data.get("daily", [])) > 1:
                tomorrow = {
                    "min": data["daily"][1]["temp"]["min"],
                    "max": data["daily"][1]["temp"]["max"],
                    "humidity": data["daily"][1]["humidity"],
                    "weather": data["daily"][1]["weather"][0]["description"],
                }

            payload = {
                "location": city_name,
                "crop": CROP,
                "current": current,
                "today": today_forecast,
                "tomorrow": tomorrow,
                "source": "openweather",
                "last_update_utc": datetime.now(timezone.utc).isoformat()
            }
            return payload

        # ==== Fallback Open-Meteo (nếu không có OpenWeather key) ====
        _log("OPENWEATHER_API_KEY không tồn tại — chuyển sang Open-Meteo (fallback).")
        om_url = (
            f"https://api.open-meteo.com/v1/forecast?"
            f"latitude={LAT}&longitude={LON}"
            f"&hourly=temperature_2m,relativehumidity_2m,weathercode"
            f"&daily=temperature_2m_min,temperature_2m_max,weathercode"
            f"&timezone=auto"
        )
        r2 = requests.get(om_url, timeout=10)
        r2.raise_for_status()
        d2 = r2.json()

        # Now in local timezone (best-effort)
        now = datetime.now().astimezone()

        times = d2.get("hourly", {}).get("time", [])
        temps = d2.get("hourly", {}).get("temperature_2m", [])
        hums = d2.get("hourly", {}).get("relativehumidity_2m", [])
        wcodes = d2.get("hourly", {}).get("weathercode", [])

        today_forecast = []
        included = set()  # set of (date_iso, hour) to avoid duplicates

        for i, tstr in enumerate(times):
            try:
                t_dt = datetime.fromisoformat(tstr)
            except Exception:
                # skip unparsable time strings
                continue

            # If t_dt has no tzinfo, assume same tz as now
            if t_dt.tzinfo is None:
                t_dt = t_dt.replace(tzinfo=now.tzinfo)

            if t_dt >= now:
                key = (t_dt.date().isoformat(), t_dt.hour)
                if key in included:
                    continue
                included.add(key)

                today_forecast.append({
                    "iso": t_dt.isoformat(),
                    "hour": t_dt.hour,
                    "temp": temps[i] if i < len(temps) else None,
                    "humidity": hums[i] if i < len(hums) else None,
                    "weather": _om_weathercode_to_desc(wcodes[i]) if i < len(wcodes) else "n/a"
                })

            # safety cap
            if len(today_forecast) >= 24:
                break

        # Tomorrow: use daily arrays (index 1 -> tomorrow, if exists)
        tomorrow = {}
        daily = d2.get("daily", {})
        try:
            if daily:
                tmins = daily.get("temperature_2m_min", [])
                tmaxs = daily.get("temperature_2m_max", [])
                dwcodes = daily.get("weathercode", [])
                if len(tmins) > 1 and len(tmaxs) > 1:
                    tomorrow = {
                        "min": tmins[1],
                        "max": tmaxs[1],
                        "weather": _om_weathercode_to_desc(dwcodes[1]) if len(dwcodes) > 1 else "n/a"
                    }
        except Exception:
            tomorrow = {}

        payload = {
            "location": f"{LAT},{LON}",
            "crop": CROP,
            "current": {
                "hour": now.hour,
                "temp": today_forecast[0]["temp"] if today_forecast else None,
                "humidity": today_forecast[0]["humidity"] if today_forecast else None,
                "weather": today_forecast[0]["weather"] if today_forecast else "n/a",
                "iso": now.isoformat()
            },
            "today": today_forecast,
            "tomorrow": tomorrow,
            "source": "open-meteo",
            "last_update_utc": datetime.now(timezone.utc).isoformat()
        }
        return payload

    except requests.HTTPError as he:
        _log(f"❌ HTTP error fetching weather: {he}")
        return None
    except Exception as e:
        _log(f"❌ Error fetching weather: {e}")
        return None

def push_to_thingsboard():
    if not THINGSBOARD_TOKEN:
        _log("THINGSBOARD_TOKEN không được cấu hình. Bỏ qua việc push telemetry.")
        return {"ok": False, "reason": "missing_token"}

    weather = fetch_weather()
    if not weather:
        _log("Không có payload thời tiết, bỏ qua push.")
        return {"ok": False, "reason": "no_payload"}

    try:
        resp = requests.post(THINGSBOARD_URL, json=weather, timeout=10)
        if resp.status_code in (200, 201, 204):
            _log(f"✅ Pushed telemetry at {datetime.now().isoformat()}")
            return {"ok": True, "status_code": resp.status_code}
        else:
            _log(f"❌ ThingsBoard trả status {resp.status_code}: {resp.text}")
            return {"ok": False, "status_code": resp.status_code, "body": resp.text}
    except Exception as e:
        _log(f"❌ Error pushing to ThingsBoard: {e}")
        return {"ok": False, "reason": str(e)}

# Scheduler
scheduler.add_job(push_to_thingsboard, "interval", minutes=5)
scheduler.start()

if THINGSBOARD_TOKEN:
    push_to_thingsboard()
else:
    _log("Không tự gửi lần đầu vì thiếu THINGSBOARD_TOKEN.")

@app.get("/")
def root():
    return {"status": "ok", "message": "Weather bot running", "owm_key_set": bool(OWM_API_KEY), "thingsboard_token_set": bool(THINGSBOARD_TOKEN)}

@app.get("/debug")
def debug_payload():
    """Trả về payload weather hiện tại (không push). Dùng để debug."""
    payload = fetch_weather()
    if payload:
        return JSONResponse(content={"ok": True, "payload": payload})
    return JSONResponse(content={"ok": False, "error": "Không lấy được payload"} , status_code=500)

@app.post("/push")
def push_now():
    """Trigger push 1 lần và trả kết quả (useful để test token)."""
    result = push_to_thingsboard()
    if result.get("ok"):
        return JSONResponse(content={"ok": True, "result": result})
    return JSONResponse(content={"ok": False, "result": result}, status_code=500)

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

def fetch_weather():
    try:
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

        # Fallback Open-Meteo
        _log("OPENWEATHER_API_KEY không tồn tại — chuyển sang Open-Meteo (fallback).")
        om_url = (
            f"https://api.open-meteo.com/v1/forecast?"
            f"latitude={LAT}&longitude={LON}&hourly=temperature_2m,relativehumidity_2m,weathercode"
            f"&timezone=auto"
        )
        r2 = requests.get(om_url, timeout=10)
        r2.raise_for_status()
        d2 = r2.json()

        now = datetime.now()
        times = d2.get("hourly", {}).get("time", [])
        temps = d2.get("hourly", {}).get("temperature_2m", [])
        hums = d2.get("hourly", {}).get("relativehumidity_2m", [])
        today_forecast = []
        for t_i, tstr in enumerate(times):
            try:
                hour = datetime.fromisoformat(tstr).hour
            except Exception:
                continue
            if hour >= now.hour:
                today_forecast.append({
                    "hour": hour,
                    "temp": temps[t_i] if t_i < len(temps) else None,
                    "humidity": hums[t_i] if t_i < len(hums) else None,
                    "weather": "n/a"
                })
                if len(today_forecast) >= 24:
                    break

        payload = {
            "location": f"{LAT},{LON}",
            "crop": CROP,
            "current": {
                "hour": now.hour,
                "temp": temps[0] if temps else None,
                "humidity": hums[0] if hums else None,
                "weather": "n/a",
            },
            "today": today_forecast,
            "tomorrow": {},
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

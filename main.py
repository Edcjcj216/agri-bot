import os
import requests
from fastapi import FastAPI
from apscheduler.schedulers.background import BackgroundScheduler
from datetime import datetime, timezone

# === Config từ Environment Render ===
OWM_API_KEY = os.getenv("OPENWEATHER_API_KEY")   # API key OpenWeather (có thể None)
THINGSBOARD_TOKEN = os.getenv("THINGSBOARD_TOKEN")  # Device token ThingsBoard (bắt buộc)
LAT = os.getenv("LAT", "10.806094263669602")  # Vĩ độ mặc định HCM
LON = os.getenv("LON", "106.75222004270555")  # Kinh độ mặc định HCM
CROP = os.getenv("CROP", "Rau muống")         # Loại cây trồng

# URL ThingsBoard telemetry (THINGSBOARD_TOKEN có thể là None)
THINGSBOARD_URL = f"https://thingsboard.cloud/api/v1/{THINGSBOARD_TOKEN}/telemetry"

app = FastAPI()
scheduler = BackgroundScheduler()

def _log(msg):
    print(f"[{datetime.now().isoformat()}] {msg}")

# ================== Hàm lấy dự báo thời tiết (OpenWeather hoặc fallback Open-Meteo) ==================
def fetch_weather():
    try:
        # Nếu có OpenWeather API key -> ưu tiên dùng OpenWeather OneCall (https)
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

            # Lấy tên địa điểm (weather endpoint)
            city_url = f"https://api.openweathermap.org/data/2.5/weather?lat={LAT}&lon={LON}&appid={OWM_API_KEY}&lang=vi"
            city_resp = requests.get(city_url, timeout=8)
            city_name = "Unknown"
            try:
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

        # === Fallback: Open-Meteo (miễn phí, không cần API key) ===
        _log("OPENWEATHER_API_KEY không tồn tại — chuyển sang Open-Meteo (fallback).")
        om_url = (
            f"https://api.open-meteo.com/v1/forecast?"
            f"latitude={LAT}&longitude={LON}&hourly=temperature_2m,relativehumidity_2m,weathercode"
            f"&timezone=auto"
        )
        r2 = requests.get(om_url, timeout=10)
        r2.raise_for_status()
        d2 = r2.json()

        # Lấy thời gian hiện tại và giờ hiện tại theo timezone của kết quả
        now = datetime.now()
        # Lấy index giờ hiện tại (đơn giản: lấy phần tử đầu của hourly time >= now)
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

# ================== Hàm push lên ThingsBoard ==================
def push_to_thingsboard():
    if not THINGSBOARD_TOKEN:
        _log("THINGSBOARD_TOKEN không được cấu hình. Bỏ qua việc push telemetry.")
        return

    weather = fetch_weather()
    if not weather:
        _log("Không có payload thời tiết, bỏ qua push.")
        return

    try:
        resp = requests.post(THINGSBOARD_URL, json=weather, timeout=10)
        if resp.status_code in (200, 201):
            _log(f"✅ Pushed telemetry at {datetime.now().isoformat()}")
            _log(f"Payload size: {len(str(weather))} bytes")
        else:
            _log(f"❌ ThingsBoard trả status {resp.status_code}: {resp.text}")
    except Exception as e:
        _log(f"❌ Error pushing to ThingsBoard: {e}")

# ================== Scheduler chạy 5 phút/lần ==================
scheduler.add_job(push_to_thingsboard, "interval", minutes=5)
scheduler.start()

# Gọi ngay lần đầu khi service khởi động (chỉ khi token tồn tại)
if THINGSBOARD_TOKEN:
    push_to_thingsboard()
else:
    _log("Không tự gửi lần đầu vì thiếu THINGSBOARD_TOKEN.")

# ================== Endpoint kiểm tra ==================
@app.get("/")
def root():
    ok = {"status": "ok", "message": "Weather bot running", "owm_key_set": bool(OWM_API_KEY), "thingsboard_token_set": bool(THINGSBOARD_TOKEN)}
    return ok

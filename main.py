import os
import requests
from fastapi import FastAPI
from apscheduler.schedulers.background import BackgroundScheduler
from datetime import datetime, timezone
from fastapi.responses import JSONResponse
from zoneinfo import ZoneInfo

# === Config từ Environment Render ===
OWM_API_KEY = os.getenv("OPENWEATHER_API_KEY")   # API key OpenWeather (có thể None)
THINGSBOARD_TOKEN = os.getenv("THINGSBOARD_TOKEN")  # Device token ThingsBoard (bắt buộc để push)
LAT = os.getenv("LAT", "10.806094263669602")
LON = os.getenv("LON", "106.75222004270555")
CROP = os.getenv("CROP", "Rau muống")
# Nếu muốn override tên location thủ công, set env LOCATION_NAME="An Phu, Ho Chi Minh"
LOCATION_NAME_OVERRIDE = os.getenv("LOCATION_NAME")

THINGSBOARD_URL = f"https://thingsboard.cloud/api/v1/{THINGSBOARD_TOKEN}/telemetry"

app = FastAPI()
scheduler = BackgroundScheduler()

# Local timezone VN
LOCAL_TZ = ZoneInfo("Asia/Ho_Chi_Minh")

# Cache tên location sau lần lấy đầu
_LOCATION_NAME_CACHE = None

def _log(msg):
    print(f"[{datetime.now().isoformat()}] {msg}")

def get_location_name():
    """
    Trả về chuỗi mô tả vị trí như "An Phu, Ho Chi Minh".
    Thứ tự ưu tiên:
      1) LOCATION_NAME env override
      2) Cache nếu đã lấy
      3) Gọi Open-Meteo reverse-geocoding
      4) Fallback: "LAT,LON"
    """
    global _LOCATION_NAME_CACHE
    if LOCATION_NAME_OVERRIDE:
        return LOCATION_NAME_OVERRIDE

    if _LOCATION_NAME_CACHE:
        return _LOCATION_NAME_CACHE

    try:
        lat_f = float(LAT)
        lon_f = float(LON)
    except Exception:
        return f"{LAT},{LON}"

    try:
        url = (
            f"https://geocoding-api.open-meteo.com/v1/reverse?"
            f"latitude={lat_f}&longitude={lon_f}&language=vi&count=1"
        )
        r = requests.get(url, timeout=8)
        r.raise_for_status()
        j = r.json()
        results = j.get("results") or []
        if results:
            r0 = results[0]
            # Các trường có thể: name, admin1, admin2, country
            parts = []
            name = r0.get("name")
            admin1 = r0.get("admin1")
            admin2 = r0.get("admin2")
            country = r0.get("country")
            if name:
                parts.append(name)
            if admin2 and admin2 != name:
                parts.append(admin2)
            if admin1 and admin1 not in parts:
                parts.append(admin1)
            # Nếu admin1 chứa "Ho Chi Minh", bạn có thể map/format nếu cần.
            if country and country.lower() not in "vietnam":
                parts.append(country)
            pretty = ", ".join(parts) if parts else f"{LAT},{LON}"
            _LOCATION_NAME_CACHE = pretty
            return pretty
    except Exception as e:
        _log(f"Lỗi reverse-geocode: {e}")

    # fallback
    return f"{LAT},{LON}"

# Open-Meteo -> mô tả tiếng Việt
OM_VN = {
    0: "Trời quang", 1: "Hầu như quang", 2: "Mây phân bố", 3: "Nhiều mây",
    45: "Sương mù", 48: "Sương giá", 51: "Mưa phùn nhẹ", 53: "Mưa phùn",
    55: "Mưa phùn dày", 61: "Mưa nhẹ", 63: "Mưa vừa", 65: "Mưa to",
    71: "Tuyết nhẹ", 73: "Tuyết vừa", 75: "Tuyết nhiều",
    80: "Dải mưa rào", 81: "Mưa rào vừa", 82: "Mưa rào mạnh",
    95: "Giông", 96: "Giông kèm mưa đá nhẹ", 99: "Giông kèm mưa đá nặng"
}
def _om_vn(code):
    try:
        return OM_VN.get(int(code), "N/A")
    except Exception:
        return "N/A"

def fetch_weather():
    try:
        # ==== Lấy tên vị trí chi tiết (1 lần) ====
        vi_tri_text = get_location_name()

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

            now = datetime.now(LOCAL_TZ)
            current_hour = now.hour

            # Keys tiếng Việt
            hien_tai = {
                "gio": current_hour,
                "nhiet_do": data["current"]["temp"],
                "do_am": data["current"]["humidity"],
                "thoi_tiet": data["current"]["weather"][0]["description"],
                "iso": now.isoformat()
            }

            hom_nay = []
            included = set()
            for h in data.get("hourly", []):
                t_dt = datetime.fromtimestamp(h["dt"], tz=timezone.utc).astimezone(LOCAL_TZ)
                if t_dt >= now:
                    key = (t_dt.date().isoformat(), t_dt.hour)
                    if key in included:
                        continue
                    included.add(key)
                    hom_nay.append({
                        "iso": t_dt.isoformat(),
                        "gio": t_dt.hour,
                        "nhiet_do": h["temp"],
                        "do_am": h["humidity"],
                        "thoi_tiet": h["weather"][0]["description"]
                    })
                    if len(hom_nay) >= 12:
                        break

            ngay_mai = {}
            if len(data.get("daily", [])) > 1:
                ngay_mai = {
                    "nhiet_do_min": data["daily"][1]["temp"]["min"],
                    "nhiet_do_max": data["daily"][1]["temp"]["max"],
                    "do_am": data["daily"][1].get("humidity"),
                    "thoi_tiet": data["daily"][1]["weather"][0]["description"]
                }

            payload = {
                "vi_tri": vi_tri_text,
                "cay": CROP,
                "hien_tai": hien_tai,
                "hom_nay": hom_nay,
                "ngay_mai": ngay_mai,
                "nguon": "openweather",
                "cap_nhat_utc": datetime.now(timezone.utc).isoformat()
            }
            return payload

        # ==== Fallback Open-Meteo ====
        _log("OPENWEATHER_API_KEY không tồn tại — chuyển sang Open-Meteo (fallback).")
        om_url = (
            f"https://api.open-meteo.com/v1/forecast?"
            f"latitude={LAT}&longitude={LON}"
            f"&hourly=temperature_2m,relativehumidity_2m,weathercode"
            f"&daily=temperature_2m_min,temperature_2m_max,weathercode"
            f"&timezone=UTC"
        )
        r2 = requests.get(om_url, timeout=10)
        r2.raise_for_status()
        d2 = r2.json()

        # now in local timezone
        now = datetime.now(LOCAL_TZ)

        times = d2.get("hourly", {}).get("time", [])
        temps = d2.get("hourly", {}).get("temperature_2m", [])
        hums = d2.get("hourly", {}).get("relativehumidity_2m", [])
        wcodes = d2.get("hourly", {}).get("weathercode", [])

        hom_nay = []
        included = set()
        for i, tstr in enumerate(times):
            try:
                t_dt = datetime.fromisoformat(tstr.replace("Z", "+00:00"))
            except Exception:
                continue
            t_dt = t_dt.astimezone(LOCAL_TZ)

            if t_dt >= now:
                key = (t_dt.date().isoformat(), t_dt.hour)
                if key in included:
                    continue
                included.add(key)

                hom_nay.append({
                    "iso": t_dt.isoformat(),
                    "gio": t_dt.hour,
                    "nhiet_do": temps[i] if i < len(temps) else None,
                    "do_am": hums[i] if i < len(hums) else None,
                    "thoi_tiet": _om_vn(wcodes[i]) if i < len(wcodes) else "N/A"
                })

            if len(hom_nay) >= 12:
                break

        # Ngày mai
        ngay_mai = {}
        daily = d2.get("daily", {})
        try:
            if daily:
                tmins = daily.get("temperature_2m_min", [])
                tmaxs = daily.get("temperature_2m_max", [])
                dwcodes = daily.get("weathercode", [])
                if len(tmins) > 1 and len(tmaxs) > 1:
                    ngay_mai = {
                        "nhiet_do_min": tmins[1],
                        "nhiet_do_max": tmaxs[1],
                        "thoi_tiet": _om_vn(dwcodes[1]) if len(dwcodes) > 1 else "N/A"
                    }
        except Exception:
            ngay_mai = {}

        payload = {
            "vi_tri": vi_tri_text,
            "cay": CROP,
            "hien_tai": {
                "gio": now.hour,
                "nhiet_do": hom_nay[0]["nhiet_do"] if hom_nay else None,
                "do_am": hom_nay[0]["do_am"] if hom_nay else None,
                "thoi_tiet": hom_nay[0]["thoi_tiet"] if hom_nay else "N/A",
                "iso": now.isoformat()
            },
            "hom_nay": hom_nay,
            "ngay_mai": ngay_mai,
            "nguon": "open-meteo",
            "cap_nhat_utc": datetime.now(timezone.utc).isoformat()
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
    return {
        "status": "ok",
        "message": "Weather bot running",
        "owm_key_set": bool(OWM_API_KEY),
        "thingsboard_token_set": bool(THINGSBOARD_TOKEN)
    }

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

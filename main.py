import requests
import json
from datetime import datetime, timezone, timedelta

# ===== Hàm lấy tên vị trí từ toạ độ =====
def get_location_name(lat, lon):
    url = "https://nominatim.openstreetmap.org/reverse"
    params = {
        "lat": lat,
        "lon": lon,
        "format": "json",
        "accept-language": "vi"
    }
    try:
        resp = requests.get(url, params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        address = data.get("address", {})

        # Ưu tiên phường, quận, TP
        ward = address.get("suburb") or address.get("neighbourhood") or address.get("quarter") or ""
        district = address.get("city_district") or address.get("district") or ""
        city = address.get("city") or address.get("town") or address.get("state") or ""

        parts = [p for p in [ward, district, city] if p]
        return ", ".join(parts) if parts else data.get("display_name", f"{lat},{lon}")
    except Exception as e:
        print(f"[{datetime.now()}] Lỗi reverse-geocode: {e}")
        return f"{lat},{lon}"

# ===== Hàm lấy dữ liệu thời tiết từ Open-Meteo =====
def get_weather(lat, lon):
    url = "https://api.open-meteo.com/v1/forecast"
    params = {
        "latitude": lat,
        "longitude": lon,
        "hourly": "temperature_2m,relative_humidity_2m,weathercode",
        "current_weather": "true",
        "forecast_days": 2,
        "timezone": "Asia/Ho_Chi_Minh",
        "language": "vi"
    }
    resp = requests.get(url, params=params, timeout=10)
    resp.raise_for_status()
    return resp.json()

# ===== Chuyển weather code thành mô tả tiếng Việt =====
def weather_code_to_text(code):
    mapping = {
        0: "Trời quang",
        1: "Ít mây",
        2: "Nhiều mây",
        3: "U ám",
        45: "Sương mù",
        48: "Sương mù rải rác",
        51: "Mưa phùn nhẹ",
        53: "Mưa phùn",
        55: "Mưa phùn nặng hạt",
        61: "Mưa nhẹ",
        63: "Mưa vừa",
        65: "Mưa to",
        80: "Mưa rào nhẹ",
        81: "Mưa rào",
        82: "Mưa rào to",
        95: "Giông",
        96: "Giông kèm mưa đá nhẹ",
        99: "Giông kèm mưa đá mạnh"
    }
    return mapping.get(code, "N/A")

# ===== Push lên ThingsBoard hoặc InfluxDB =====
def push_telemetry(payload):
    print(f"[{datetime.now()}] ✅ Pushed telemetry: {json.dumps(payload, ensure_ascii=False)}")
    # TODO: Thay bằng hàm push thực tế (MQTT/HTTP)

# ===== Main =====
if __name__ == "__main__":
    lat, lon = 10.806094263669602, 106.75222004270555
    vi_tri = get_location_name(lat, lon)
    data = get_weather(lat, lon)

    now = datetime.now(timezone.utc)
    now_vn = now.astimezone(timezone(timedelta(hours=7)))

    # Dữ liệu hiện tại
    current = data.get("current_weather", {})
    hien_tai = {
        "gio": now_vn.hour,
        "nhiet_do": current.get("temperature"),
        "do_am": data["hourly"]["relative_humidity_2m"][0] if "hourly" in data else None,
        "thoi_tiet": weather_code_to_text(current.get("weathercode")),
        "iso": now_vn.isoformat()
    }

    # Dữ liệu hôm nay
    hom_nay = []
    hours = data["hourly"]["time"]
    temps = data["hourly"]["temperature_2m"]
    hums = data["hourly"]["relative_humidity_2m"]
    codes = data["hourly"]["weathercode"]
    for t, temp, hum, code in zip(hours, temps, hums, codes):
        dt = datetime.fromisoformat(t)
        if dt.date() == now_vn.date():
            hom_nay.append({
                "iso": dt.isoformat(),
                "gio": dt.hour,
                "nhiet_do": temp,
                "do_am": hum,
                "thoi_tiet": weather_code_to_text(code)
            })

    # Dữ liệu ngày mai (lấy min/max và mô tả)
    ngay_mai = {}
    tomorrow = now_vn.date() + timedelta(days=1)
    temps_tmr = [temp for t, temp in zip(hours, temps) if datetime.fromisoformat(t).date() == tomorrow]
    codes_tmr = [code for t, code in zip(hours, codes) if datetime.fromisoformat(t).date() == tomorrow]
    if temps_tmr:
        ngay_mai = {
            "nhiet_do_min": min(temps_tmr),
            "nhiet_do_max": max(temps_tmr),
            "thoi_tiet": weather_code_to_text(max(set(codes_tmr), key=codes_tmr.count)) if codes_tmr else "N/A"
        }

    payload = {
        "cap_nhat_utc": now.isoformat(),
        "cay": "Rau muống",
        "hien_tai": hien_tai,
        "hom_nay": hom_nay,
        "ngay_mai": ngay_mai,
        "nguon": "open-meteo",
        "vi_tri": vi_tri
    }

    push_telemetry(payload)

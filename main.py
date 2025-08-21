from fastapi import FastAPI
from fastapi.responses import JSONResponse
import requests
from datetime import datetime, timezone, timedelta

app = FastAPI()

# === Cấu hình toạ độ và thông tin cây trồng ===
LAT, LON = 10.806094263669602, 106.75222004270555
CAY_TRONG = "Rau muống"

# === Map weathercode sang tiếng Việt ===
WEATHER_MAP = {
    0: "Trời quang",
    1: "Chủ yếu quang",
    2: "Có mây",
    3: "Nhiều mây",
    45: "Sương mù",
    48: "Sương mù đóng băng",
    51: "Mưa phùn nhẹ",
    53: "Mưa phùn vừa",
    55: "Mưa phùn dày",
    56: "Mưa phùn lạnh nhẹ",
    57: "Mưa phùn lạnh dày",
    61: "Mưa nhẹ",
    63: "Mưa vừa",
    65: "Mưa to",
    66: "Mưa lạnh nhẹ",
    67: "Mưa lạnh to",
    71: "Tuyết nhẹ",
    73: "Tuyết vừa",
    75: "Tuyết dày",
    77: "Tuyết hạt",
    80: "Dải mưa rào nhẹ",
    81: "Dải mưa rào vừa",
    82: "Dải mưa rào to",
    85: "Dải tuyết nhẹ",
    86: "Dải tuyết dày",
    95: "Giông",
    96: "Giông kèm mưa đá nhẹ",
    99: "Giông kèm mưa đá to"
}

def get_location_name(lat: float, lon: float) -> str:
    """Lấy tên địa điểm chi tiết bằng reverse-geocode. Fallback ra lat,lon nếu lỗi."""
    url = f"https://geocoding-api.open-meteo.com/v1/reverse?latitude={lat}&longitude={lon}&language=vi&count=1"
    try:
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        data = r.json()
        if "results" in data and len(data["results"]) > 0:
            loc = data["results"][0]
            # Ghép tên chi tiết: ví dụ "An Phú, Thành phố Hồ Chí Minh"
            parts = [loc.get("name"), loc.get("admin1"), loc.get("country")]
            return ", ".join([p for p in parts if p])
    except Exception as e:
        print(f"[WARN] Reverse-geocode error: {e}")
    return f"{lat:.4f},{lon:.4f}"

def fetch_weather(lat: float, lon: float):
    """Gọi Open-Meteo API lấy hiện tại + dự báo từng giờ + ngày."""
    url = (
        "https://api.open-meteo.com/v1/forecast?"
        f"latitude={lat}&longitude={lon}"
        "&hourly=temperature_2m,relative_humidity_2m,weathercode"
        "&current_weather=true"
        "&daily=temperature_2m_max,temperature_2m_min,weathercode"
        "&timezone=Asia%2FBangkok"
    )
    r = requests.get(url, timeout=10)
    r.raise_for_status()
    return r.json()

def map_weather(code: int) -> str:
    return WEATHER_MAP.get(code, f"Mã thời tiết {code}")

@app.get("/")
def root():
    data = fetch_weather(LAT, LON)
    now_utc = datetime.now(timezone.utc)
    tz_vn = timezone(timedelta(hours=7))

    # ---- Xử lý thời tiết hiện tại ----
    cur = data.get("current_weather", {})
    now_local = datetime.fromisoformat(cur["time"]).astimezone(tz_vn)
    hien_tai = {
        "gio": now_local.hour,
        "nhiet_do": cur.get("temperature"),
        "do_am": None,  # current_weather không có độ ẩm => lấy từ hourly gần nhất
        "thoi_tiet": map_weather(cur.get("weathercode", -1)),
        "iso": now_local.isoformat()
    }
    # lấy độ ẩm giờ hiện tại từ hourly
    hours = data.get("hourly", {})
    if "time" in hours and "relative_humidity_2m" in hours:
        # tìm index giờ trùng hoặc gần nhất
        times = [datetime.fromisoformat(t).astimezone(tz_vn) for t in hours["time"]]
        hums = hours["relative_humidity_2m"]
        # tìm giờ gần nhất
        nearest = min(range(len(times)), key=lambda i: abs((times[i] - now_local).total_seconds()))
        hien_tai["do_am"] = hums[nearest]

    # ---- Xử lý dự báo từng giờ hôm nay ----
    hom_nay = []
    for t, temp, hum, code in zip(hours["time"], hours["temperature_2m"],
                                  hours["relative_humidity_2m"], hours["weathercode"]):
        dt = datetime.fromisoformat(t).astimezone(tz_vn)
        if dt.date() == now_local.date():
            hom_nay.append({
                "iso": dt.isoformat(),
                "gio": dt.hour,
                "nhiet_do": temp,
                "do_am": hum,
                "thoi_tiet": map_weather(code)
            })

    # ---- Xử lý dự báo ngày mai ----
    daily = data.get("daily", {})
    ngay_mai = {}
    if "time" in daily and len(daily["time"]) >= 2:
        # phần tử thứ 1 là ngày mai
        ngay_mai = {
            "nhiet_do_min": daily["temperature_2m_min"][1],
            "nhiet_do_max": daily["temperature_2m_max"][1],
            "thoi_tiet": map_weather(daily["weathercode"][1])
        }

    # ---- Lấy tên vị trí ----
    vi_tri = get_location_name(LAT, LON)

    telemetry = {
        "cap_nhat_utc": now_utc.isoformat(),
        "cay": CAY_TRONG,
        "hien_tai": hien_tai,
        "hom_nay": hom_nay,
        "ngay_mai": ngay_mai,
        "nguon": "open-meteo",
        "vi_tri": vi_tri
    }
    return JSONResponse(content=telemetry)

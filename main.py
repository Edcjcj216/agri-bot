import os
import logging
import requests
from datetime import datetime, timedelta
from fastapi import FastAPI

# ============================================================
# Agri-bot — Lấy thời tiết Open-Meteo, fallback OWM + OpenRouter (có thể thêm sau)
#
# Logic thời gian:
#  - Nếu hiện tại đúng HH:00 → lấy từ HH:00 làm gốc.
#  - Nếu hiện tại lệch (vd 08:39) → làm tròn lên giờ kế (vd 09:00).
#  - Lấy tiếp 4 mốc giờ liên tiếp.
#
# Dữ liệu trả về:
#  - hour_1..hour_4 + temp/humidity/desc
#  - Hiện tại (temperature_h, humidity)
#  - Ngày mai (min, max, desc, humidity)
# ============================================================

# Tạo FastAPI app
app = FastAPI()

# Bản đồ mã thời tiết (Open-Meteo code) → mô tả tiếng Việt
WEATHER_CODE_MAP = {
    0: "Trời nắng đẹp",
    1: "Trời không mây",
    2: "Trời có mây",
    3: "Trời nhiều mây",
    51: "Mưa phùn nhẹ",
    53: "Mưa phùn vừa",
    55: "Mưa phùn dày hạt",
    61: "Mưa nhẹ",
    63: "Mưa vừa",
    65: "Mưa to",
    80: "Mưa rào nhẹ",
    81: "Mưa rào vừa",
    82: "Mưa rào to",
    95: "Có giông nhẹ",
    96: "Có giông vừa",
    99: "Có giông lớn",
}

# =====================
# Hàm tiện ích
# =====================
def round_next_hour(now: datetime) -> datetime:
    """Làm tròn giờ: nếu đúng HH:00 thì giữ, ngược lại +1h"""
    if now.minute == 0 and now.second == 0:
        return now.replace(minute=0, second=0, microsecond=0)
    return (now + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)

def weather_desc_from_code(code: int) -> str:
    """Trả về mô tả tiếng Việt từ code, fallback sang 'Không xác định'"""
    return WEATHER_CODE_MAP.get(code, "Không xác định")

# =====================
# Fetch từ Open-Meteo (chính)
# =====================
def fetch_openmeteo(lat: float, lon: float):
    url = (
        f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}"
        "&hourly=temperature_2m,relative_humidity_2m,weathercode"
        "&daily=temperature_2m_max,temperature_2m_min,weathercode,relative_humidity_2m"
        "&timezone=auto"
    )
    resp = requests.get(url, timeout=10)
    resp.raise_for_status()
    return resp.json()

# =====================
# API endpoint chính
# =====================
@app.get("/weather")
def get_weather(lat: float = 21.0278, lon: float = 105.8342):
    """
    Lấy thời tiết tại VN (mặc định Hà Nội), trả về:
     - 4 giờ tới (hour_1..hour_4)
     - Thông tin hiện tại
     - Dự báo ngày mai
    """
    now = datetime.now()
    base = round_next_hour(now)  # Làm tròn giờ theo logic ở trên
    logging.info(f"Bây giờ: {now}, sau khi làm tròn: {base}")

    try:
        data = fetch_openmeteo(lat, lon)
    except Exception as e:
        logging.error(f"Không gọi được Open-Meteo: {e}")
        return {"error": "Không lấy được dữ liệu thời tiết"}

    hourly = data.get("hourly", {})
    daily = data.get("daily", {})
    times = hourly.get("time", [])
    temps = hourly.get("temperature_2m", [])
    hums = hourly.get("relative_humidity_2m", [])
    codes = hourly.get("weathercode", [])

    result = {}
    selected_hours = []

    # Xử lý 4 giờ tới
    for i in range(4):
        target = base + timedelta(hours=i)
        iso = target.isoformat()
        if iso in times:
            idx = times.index(iso)
            result[f"hour_{i+1}"] = target.strftime("%H:%M")
            result[f"hour_{i+1}_temperature"] = temps[idx]
            result[f"hour_{i+1}_humidity"] = hums[idx]
            result[f"hour_{i+1}_weather_desc"] = weather_desc_from_code(codes[idx])
            selected_hours.append(result[f"hour_{i+1}"])

    # Log các giờ đã chọn
    logging.info(f"Chọn các giờ: {selected_hours}")

    # Hiện tại (giờ gốc)
    if base.isoformat() in times:
        idx = times.index(base.isoformat())
        result["temperature_h"] = temps[idx]
        result["humidity"] = hums[idx]

    # Ngày mai
    if "temperature_2m_max" in daily and len(daily["temperature_2m_max"]) > 1:
        result["weather_tomorrow_max"] = daily["temperature_2m_max"][1]
        result["weather_tomorrow_min"] = daily["temperature_2m_min"][1]
        result["humidity_tomorrow"] = daily["relative_humidity_2m"][1]
        result["weather_tomorrow_desc"] = weather_desc_from_code(daily["weathercode"][1])

    return result

# =====================
# Chạy uvicorn (local test)
# =====================
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)

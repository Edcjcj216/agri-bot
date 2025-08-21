from fastapi import FastAPI
from fastapi.responses import JSONResponse
import requests
from datetime import datetime
from zoneinfo import ZoneInfo

# ===============================
# BẢNG MÃ THỜI TIẾT → TIẾNG VIỆT
# ===============================
WEATHERCODE_VI = {
    0: "Trời quang",
    1: "Ít mây",
    2: "Có mây",
    3: "Nhiều mây",
    45: "Sương mù",
    48: "Sương mù có sương đóng băng",
    51: "Mưa phùn nhẹ",
    53: "Mưa phùn vừa",
    55: "Mưa phùn dày",
    56: "Mưa phùn đóng băng nhẹ",
    57: "Mưa phùn đóng băng dày",
    61: "Mưa nhẹ",
    63: "Mưa vừa",
    65: "Mưa to",
    66: "Mưa đóng băng nhẹ",
    67: "Mưa đóng băng to",
    71: "Tuyết rơi nhẹ",
    73: "Tuyết rơi vừa",
    75: "Tuyết rơi dày",
    77: "Hạt tuyết",
    80: "Mưa rào nhẹ",
    81: "Mưa rào vừa",
    82: "Mưa rào to",
    85: "Mưa tuyết nhẹ",
    86: "Mưa tuyết to",
    95: "Giông nhẹ hoặc vừa",
    96: "Giông có mưa đá nhẹ",
    99: "Giông có mưa đá to"
}

# ===============================
# HÀM LẤY DỮ LIỆU TELEMETRY
# ===============================
def fetch_telemetry():
    latitude = 10.806094263669602
    longitude = 106.75222004270555

    # Gọi Open-Meteo API
    url = (
        f"https://api.open-meteo.com/v1/forecast?"
        f"latitude={latitude}&longitude={longitude}"
        f"&hourly=temperature_2m,relative_humidity_2m,weathercode"
        f"&timezone=Asia/Bangkok"
    )
    resp = requests.get(url, timeout=10)
    data = resp.json()

    tz = ZoneInfo("Asia/Ho_Chi_Minh")
    now = datetime.now(tz)
    cap_nhat_utc = datetime.now(ZoneInfo("UTC")).isoformat()

    # Lấy chỉ số thời tiết hiện tại
    nhiet_do = data["hourly"]["temperature_2m"][0]
    do_am = data["hourly"]["relative_humidity_2m"][0]
    code = data["hourly"]["weathercode"][0]
    thoi_tiet = WEATHERCODE_VI.get(code, "Không xác định")

    hien_tai = {
        "gio": now.hour,
        "nhiet_do": nhiet_do,
        "do_am": do_am,
        "thoi_tiet": thoi_tiet,
        "iso": now.isoformat()
    }

    ket_qua = {
        "cap_nhat_utc": cap_nhat_utc,
        "cay": "Rau muống",
        "hien_tai": hien_tai,
        "nguon": "open-meteo",
        "vi_tri": "An Phú, Thành phố Hồ Chí Minh"
    }
    return ket_qua


# ===============================
# FASTAPI APP
# ===============================
app = FastAPI(title="Telemetry API")

@app.get("/")
def read_root():
    data = fetch_telemetry()
    return JSONResponse(content=data)


# ===============================
# CHẠY LOCAL (debug)
# ===============================
if __name__ == "__main__":
    print(fetch_telemetry())

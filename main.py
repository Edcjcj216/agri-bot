import os
import json
import requests
from fastapi import FastAPI
from datetime import datetime
from zoneinfo import ZoneInfo

app = FastAPI()

THINGSBOARD_TOKEN = os.getenv("THINGSBOARD_TOKEN")
THINGSBOARD_URL = os.getenv("THINGSBOARD_URL", "https://demo.thingsboard.io/api/v1")

# Mã thời tiết Open-Meteo sang tiếng Việt
WEATHER_VN = {
    0: "Trời quang",
    1: "Chủ yếu quang đãng",
    2: "Có mây",
    3: "Nhiều mây",
    45: "Sương mù",
    48: "Sương mù bám sương",
    51: "Mưa phùn nhẹ",
    53: "Mưa phùn",
    55: "Mưa phùn dày",
    56: "Mưa phùn đóng băng nhẹ",
    57: "Mưa phùn đóng băng dày",
    61: "Mưa nhẹ",
    63: "Mưa vừa",
    65: "Mưa to",
    66: "Mưa đóng băng nhẹ",
    67: "Mưa đóng băng nặng hạt",
    71: "Tuyết rơi nhẹ",
    73: "Tuyết rơi vừa",
    75: "Tuyết rơi dày",
    77: "Hạt tuyết",
    80: "Mưa rào nhẹ",
    81: "Mưa rào vừa",
    82: "Mưa rào to",
    85: "Tuyết rào nhẹ",
    86: "Tuyết rào to",
    95: "Giông bão",
    96: "Giông bão kèm mưa đá nhẹ",
    99: "Giông bão kèm mưa đá to",
}

def weather_desc(code: int) -> str:
    return WEATHER_VN.get(code, "Không xác định")

def reverse_geocode(lat: float, lon: float) -> str:
    url = (
        f"https://geocoding-api.open-meteo.com/v1/reverse"
        f"?latitude={lat}&longitude={lon}&language=vi&count=1"
    )
    try:
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        data = r.json()
        if data.get("results"):
            res = data["results"][0]
            name = res.get("name")
            admin1 = res.get("admin1")
            country = res.get("country")
            # Ghép: "An Phú, TP Hồ Chí Minh" hoặc fallback
            parts = [p for p in [name, admin1, country] if p]
            return ", ".join(parts)
    except Exception as e:
        print(f"[reverse_geocode] Lỗi: {e}")
    return f"{lat:.4f},{lon:.4f}"

def fetch_weather(lat: float, lon: float):
    url = (
        f"https://api.open-meteo.com/v1/forecast"
        f"?latitude={lat}&longitude={lon}"
        f"&hourly=temperature_2m,relative_humidity_2m,weathercode"
        f"&current_weather=true&timezone=Asia/Bangkok"
    )
    r = requests.get(url, timeout=10)
    r.raise_for_status()
    return r.json()

def build_payload(lat: float, lon: float, cay: str):
    raw = fetch_weather(lat, lon)
    vi_tri = reverse_geocode(lat, lon)
    tz = ZoneInfo("Asia/Bangkok")

    now_utc = datetime.now(tz=ZoneInfo("UTC")).isoformat()
    current = raw["current_weather"]
    hourlies = raw["hourly"]

    # Ghép dữ liệu giờ hiện tại
    hien_tai = {
        "gio": int(datetime.fromisoformat(current["time"]).astimezone(tz).hour),
        "nhiet_do": current["temperature"],
        "do_am": hourlies["relative_humidity_2m"][0],  # giờ gần nhất
        "thoi_tiet": weather_desc(current["weathercode"]),
        "iso": datetime.fromisoformat(current["time"]).astimezone(tz).isoformat(),
    }

    # Ghép dữ liệu dự báo trong ngày
    hom_nay = []
    for i, t in enumerate(hourlies["time"][:12]):  # lấy ~12h tới
        dt = datetime.fromisoformat(t).astimezone(tz)
        hom_nay.append({
            "iso": dt.isoformat(),
            "gio": int(dt.hour),
            "nhiet_do": hourlies["temperature_2m"][i],
            "do_am": hourlies["relative_humidity_2m"][i],
            "thoi_tiet": weather_desc(hourlies["weathercode"][i]),
        })

    # Dự báo ngày mai: min/max
    nhiet_do_min = min(hourlies["temperature_2m"][12:24]) if len(hourlies["temperature_2m"])>=24 else min(hourlies["temperature_2m"])
    nhiet_do_max = max(hourlies["temperature_2m"][12:24]) if len(hourlies["temperature_2m"])>=24 else max(hourlies["temperature_2m"])
    ngay_mai = {
        "nhiet_do_min": nhiet_do_min,
        "nhiet_do_max": nhiet_do_max,
        "thoi_tiet": weather_desc(hourlies["weathercode"][12]) if len(hourlies["weathercode"])>12 else weather_desc(hourlies["weathercode"][-1]),
    }

    payload = {
        "cap_nhat_utc": now_utc,
        "cay": cay,
        "hien_tai": hien_tai,
        "hom_nay": hom_nay,
        "ngay_mai": ngay_mai,
        "nguon": "open-meteo",
        "vi_tri": vi_tri,
    }
    return payload

def push_to_thingsboard(payload: dict):
    if not THINGSBOARD_TOKEN:
        print("⚠️  THINGSBOARD_TOKEN không tồn tại — bỏ qua push.")
        return
    url = f"{THINGSBOARD_URL}/{THINGSBOARD_TOKEN}/telemetry"
    try:
        r = requests.post(url, json=payload, timeout=10)
        r.raise_for_status()
        print(f"✅ Đã push telemetry lúc {payload['cap_nhat_utc']}")
    except Exception as e:
        print(f"❌ Lỗi push ThingsBoard: {e}")

@app.get("/")
def root(lat: float = 10.8061, lon: float = 106.7522, cay: str = "Rau muống"):
    payload = build_payload(lat, lon, cay)
    push_to_thingsboard(payload)
    return payload

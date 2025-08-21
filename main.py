from fastapi import FastAPI, Request
import httpx, asyncio, os, json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

# ====== Cấu hình ======
THINGSBOARD_URL = "https://demo.thingsboard.io/api/v1"
THINGSBOARD_TOKEN = os.getenv("THINGSBOARD_TOKEN")  # nhớ set trên Render
LAT, LON = 10.80609, 106.75222
LOCATION_NAME = "An Phú, Thành phố Hồ Chí Minh"
CROP_NAME = "Rau muống"

# Mapping weather code -> tiếng Việt
WX_MAP = {
    0: "Trời quang", 1: "Ít mây", 2: "Mây thưa", 3: "Nhiều mây",
    45: "Sương mù", 48: "Sương mù lẫn sương giá",
    51: "Mưa phùn nhẹ", 53: "Mưa phùn vừa", 55: "Mưa phùn dày",
    56: "Mưa phùn nhẹ kèm băng", 57: "Mưa phùn dày kèm băng",
    61: "Mưa nhẹ", 63: "Mưa vừa", 65: "Mưa to",
    66: "Mưa nhẹ kèm băng", 67: "Mưa to kèm băng",
    71: "Tuyết nhẹ", 73: "Tuyết vừa", 75: "Tuyết dày",
    77: "Hạt tuyết", 80: "Mưa rào nhẹ", 81: "Mưa rào vừa", 82: "Mưa rào to",
    85: "Mưa tuyết nhẹ", 86: "Mưa tuyết to",
    95: "Giông nhẹ hoặc vừa", 96: "Giông kèm mưa đá nhẹ", 99: "Giông kèm mưa đá to"
}

app = FastAPI()

async def fetch_weather():
    """Gọi Open-Meteo lấy thời tiết hiện tại, 6 giờ tới và dự báo ngày mai"""
    url = (
        f"https://api.open-meteo.com/v1/forecast?"
        f"latitude={LAT}&longitude={LON}"
        "&hourly=temperature_2m,relative_humidity_2m,weathercode"
        "&daily=temperature_2m_max,temperature_2m_min,weathercode"
        "&current_weather=true&timezone=Asia/Bangkok"
    )
    async with httpx.AsyncClient() as client:
        r = await client.get(url, timeout=20)
        r.raise_for_status()
        return r.json()

def parse_weather(data: dict):
    """Xử lý dữ liệu thời tiết trả về → telemetry"""
    tz = ZoneInfo("Asia/Ho_Chi_Minh")
    now_iso = datetime.now(tz).isoformat()

    # Hiện tại
    cur = data["current_weather"]
    wx_hien_tai = {
        "temp": cur["temperature"],
        "humidity": data["hourly"]["relative_humidity_2m"][0],
        "desc": WX_MAP.get(cur["weathercode"], "Không xác định"),
        "iso": cur["time"]
    }

    # 6 giờ tiếp theo
    wx_gio = []
    for i in range(1, 7):
        wx_gio.append({
            "hour": int(data["hourly"]["time"][i][-5:-3]),
            "temp": data["hourly"]["temperature_2m"][i],
            "humidity": data["hourly"]["relative_humidity_2m"][i],
            "desc": WX_MAP.get(data["hourly"]["weathercode"][i], "Không xác định"),
            "iso": data["hourly"]["time"][i]
        })

    # Ngày mai
    wx_ngay_mai = {
        "temp_min": data["daily"]["temperature_2m_min"][1],
        "temp_max": data["daily"]["temperature_2m_max"][1],
        "desc": WX_MAP.get(data["daily"]["weathercode"][1], "Không xác định")
    }

    telemetry = {
        "timestamp_utc": datetime.utcnow().isoformat(),
        "location": LOCATION_NAME,
        "crop": CROP_NAME,
        "battery": 90,
        "temperature": wx_hien_tai["temp"],
        "humidity": wx_hien_tai["humidity"],
        "prediction": f"Nhiệt độ {wx_hien_tai['temp']}°C, độ ẩm {wx_hien_tai['humidity']}%",
        "advice_nutrition": "Ưu tiên Kali (K) | Cân bằng NPK | Bón phân hữu cơ",
        "advice_care": "Tưới đủ nước, theo dõi thường xuyên | Độ ẩm ổn",
        "advice_note": "Quan sát cây trồng và điều chỉnh thực tế",
        "advice": "Ưu tiên Kali (K) | Cân bằng NPK | Bón phân hữu cơ | Tưới đủ nước, theo dõi thường xuyên | Độ ẩm ổn | Quan sát cây trồng và điều chỉnh thực tế",
        "wx_hien_tai": wx_hien_tai,
        "wx_ngay_mai": wx_ngay_mai,
    }
    # Tách 6 key giờ
    for idx, g in enumerate(wx_gio, start=1):
        telemetry[f"wx_gio_{idx}"] = g

    return telemetry

async def push_telemetry(payload: dict):
    if not THINGSBOARD_TOKEN:
        print("⚠️ Chưa cấu hình THINGSBOARD_TOKEN — bỏ qua gửi telemetry.")
        return
    url = f"{THINGSBOARD_URL}/{THINGSBOARD_TOKEN}/telemetry"
    async with httpx.AsyncClient() as client:
        try:
            r = await client.post(url, json=payload, timeout=10)
            r.raise_for_status()
            print(f"✅ Pushed telemetry at {payload['timestamp_utc']}")
        except Exception as e:
            print("❌ Lỗi push telemetry:", e)

@app.get("/")
async def root():
    return {"status": "AgriBot FastAPI is running", "location": LOCATION_NAME}

@app.post("/esp32-data")
async def esp32_data(req: Request):
    data = await req.json()
    print("ESP32 data received:", data)
    # Ghép dữ liệu ESP32 (nhiệt độ / pin thực tế) nếu có
    telemetry = await build_and_push()
    if "temperature" in data:
        telemetry["temperature"] = data["temperature"]
    if "battery" in data:
        telemetry["battery"] = data["battery"]
    await push_telemetry(telemetry)
    return {"status": "ok"}

async def build_and_push():
    wx_raw = await fetch_weather()
    telemetry = parse_weather(wx_raw)
    await push_telemetry(telemetry)
    return telemetry

async def auto_loop():
    while True:
        await build_and_push()
        await asyncio.sleep(900)  # 15 phút

@app.on_event("startup")
async def on_startup():
    asyncio.create_task(auto_loop())

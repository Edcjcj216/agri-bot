import os
import time
import json
import logging
import requests
from fastapi import FastAPI
from pydantic import BaseModel
import threading
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

# ================== CONFIG ==================
# ThingsBoard (demo token theo ví dụ của bạn). Có thể đổi bằng ENV nếu muốn.
TB_DEMO_TOKEN = os.getenv("TB_DEMO_TOKEN", "pk94asonfacs6mbeuutg")
TB_BASE_URL = os.getenv("TB_BASE_URL", "https://thingsboard.cloud")
TB_DEVICE_URL = f"{TB_BASE_URL}/api/v1/{TB_DEMO_TOKEN}/telemetry"

# AI (tùy chọn)
AI_API_URL = os.getenv("AI_API_URL", "https://api-inference.huggingface.co/models/gpt2")
HF_TOKEN = os.getenv("HF_TOKEN", "")

# Toạ độ & Crop
LAT = float(os.getenv("LAT", "10.806094263669602"))
LON = float(os.getenv("LON", "106.75222004270555"))
CROP = os.getenv("CROP", "Rau muống")

# Múi giờ VN
TZ_VN = ZoneInfo("Asia/Ho_Chi_Minh")

# ================== LOGGING ==================
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# ================== FASTAPI ==================
app = FastAPI(title="AgriBot Weather + Telemetry", version="1.0.0")

class SensorData(BaseModel):
    temperature: float
    humidity: float
    battery: float | None = None

# ================== WEATHER ==================
WEATHER_VN = {
    0: "Trời quang",
    1: "Chủ yếu quang đãng",
    2: "Có mây",
    3: "Nhiều mây",
    45: "Sương mù",
    48: "Sương mù bám sương",
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
    77: "Hạt tuyết",
    80: "Mưa rào nhẹ",
    81: "Mưa rào vừa",
    82: "Mưa rào to",
    85: "Tuyết rào nhẹ",
    86: "Tuyết rào dày",
    95: "Giông",
    96: "Giông kèm mưa đá nhẹ",
    99: "Giông kèm mưa đá to",
}

def wx_desc(code: int) -> str:
    try:
        return WEATHER_VN.get(int(code), f"Mã thời tiết {code}")
    except Exception:
        return "Không xác định"

def reverse_geocode(lat: float, lon: float) -> str:
    """Lấy tên vị trí tiếng Việt. Fallback về lat,lon nếu lỗi."""
    url = (
        "https://geocoding-api.open-meteo.com/v1/reverse"
        f"?latitude={lat}&longitude={lon}&language=vi&count=1"
    )
    try:
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        data = r.json()
        if data.get("results"):
            res = data["results"][0]
            parts = [res.get("name"), res.get("admin1"), res.get("country")]
            label = ", ".join([p for p in parts if p])
            if label:
                return label
    except Exception as e:
        logger.warning(f"Reverse-geocode lỗi: {e}")
    return f"{lat:.5f},{lon:.5f}"

def get_weather_detail(lat: float = LAT, lon: float = LON) -> dict:
    """
    Trả về:
    - crop, location
    - hien_tai: temp, humidity, desc, iso
    - gio_tiep_theo: danh sách ~6 giờ tiếp theo [ {hour,temp,humidity,desc,iso} ... ]
    - ngay_mai: {temp_min, temp_max, desc}
    """
    try:
        url = (
            "https://api.open-meteo.com/v1/forecast?"
            f"latitude={lat}&longitude={lon}"
            "&current_weather=true"
            "&hourly=temperature_2m,relative_humidity_2m,weathercode"
            "&daily=weathercode,temperature_2m_max,temperature_2m_min"
            "&forecast_days=2"
            "&timezone=Asia/Ho_Chi_Minh"
        )
        r = requests.get(url, timeout=12)
        r.raise_for_status()
        data = r.json()

        # ----- Current -----
        cur = data.get("current_weather", {})
        cur_time_iso = cur.get("time")  # ISO string in Asia/Ho_Chi_Minh
        cur_dt = datetime.fromisoformat(cur_time_iso)
        cur_temp = cur.get("temperature")
        cur_code = cur.get("weathercode")
        cur_desc = wx_desc(cur_code)

        # hourly arrays (aligned with TZ already)
        hourly = data.get("hourly", {})
        times = [datetime.fromisoformat(t) for t in hourly.get("time", [])]
        temps = hourly.get("temperature_2m", [])
        hums = hourly.get("relative_humidity_2m", [])
        codes = hourly.get("weathercode", [])

        # tìm index giờ hiện tại trong mảng hourly (nếu không có thì chọn giờ gần nhất)
        def nearest_index(target: datetime) -> int:
            if not times:
                return 0
            return min(range(len(times)), key=lambda i: abs((times[i] - target).total_seconds()))

        try:
            idx_now = times.index(cur_dt)
        except ValueError:
            idx_now = nearest_index(cur_dt)

        # Độ ẩm hiện tại: lấy từ hourly theo idx_now nếu có
        cur_hum = hums[idx_now] if idx_now < len(hums) else None

        hien_tai = {
            "temp": cur_temp,
            "humidity": cur_hum,
            "desc": cur_desc,
            "iso": cur_dt.isoformat(),
        }

        # ----- Next up to 6 hours -----
        gio_tiep_theo = []
        # lấy các giờ sau đó (1..6), không gồm giờ hiện tại
        for k in range(1, 7):
            i = idx_now + k
            if i >= len(times):
                break
            gio_tiep_theo.append({
                "hour": times[i].hour,
                "temp": temps[i] if i < len(temps) else None,
                "humidity": hums[i] if i < len(hums) else None,
                "desc": wx_desc(codes[i]) if i < len(codes) else "Không xác định",
                "iso": times[i].isoformat(),
            })

        # ----- Tomorrow summary -----
        daily = data.get("daily", {})
        ngay_mai = {}
        if daily and len(daily.get("time", [])) >= 2:
            ngay_mai = {
                "temp_min": daily["temperature_2m_min"][1],
                "temp_max": daily["temperature_2m_max"][1],
                "desc": wx_desc(daily["weathercode"][1]),
            }

        # ----- Location label -----
        location_label = reverse_geocode(lat, lon)

        return {
            "crop": CROP,
            "location": location_label,
            "hien_tai": hien_tai,
            "gio_tiep_theo": gio_tiep_theo,  # danh sách tối đa 6 mục
            "ngay_mai": ngay_mai,
        }
    except Exception as e:
        logger.warning(f"Weather API error: {e}")
        # Fallback tối thiểu trong trường hợp lỗi mạng
        return {
            "crop": CROP,
            "location": reverse_geocode(lat, lon),
            "hien_tai": {"temp": None, "humidity": None, "desc": "Không xác định", "iso": datetime.now(TZ_VN).isoformat()},
            "gio_tiep_theo": [],
            "ngay_mai": {},
        }

# ================== AI HELPER ==================
def call_ai_api(data: dict) -> dict:
    """Gọi AI (nếu có) + rule local ngắn gọn."""
    headers = {"Authorization": f"Bearer {HF_TOKEN}"} if HF_TOKEN else {}

    # Ghép ngữ cảnh thời tiết hôm nay/tới
    wx = get_weather_detail()
    hn = wx.get("ngay_mai", {})
    wx_text = ""
    if hn:
        wx_text = f" Ngày mai: {hn.get('desc','?')}, {hn.get('temp_min','?')}–{hn.get('temp_max','?')}°C."

    prompt = (
        f"Dữ liệu cảm biến: Nhiệt độ {data['temperature']}°C, độ ẩm {data['humidity']}%, pin {data.get('battery','?')}%. "
        f"Cây: {CROP} tại {wx.get('location','Hồ Chí Minh')}.{wx_text} "
        "Viết 1 câu dự báo và 1 câu gợi ý chăm sóc ngắn gọn."
    )
    body = {"inputs": prompt, "options": {"wait_for_model": True}}

    def local_rules(temp: float, hum: float, bat: float | None = None) -> dict:
        nutrition = ["Ưu tiên Kali (K)", "Cân bằng NPK", "Bón phân hữu cơ"]
        care = []
        if temp >= 35:
            care.append("Tránh nắng gắt, tưới sáng sớm/chiều mát")
        elif temp >= 30:
            care.append("Tưới đủ nước, theo dõi thường xuyên")
        elif temp <= 15:
            care.append("Giữ ấm, tránh sương")
        else:
            care.append("Nhiệt độ phù hợp")
        if hum <= 40:
            care.append("Độ ẩm thấp: tăng tưới")
        elif hum <= 60:
            care.append("Độ ẩm hơi thấp: theo dõi")
        elif hum >= 85:
            care.append("Độ ẩm cao: tránh úng")
        else:
            care.append("Độ ẩm ổn")
        if bat is not None and bat <= 20:
            care.append("Pin thấp: kiểm tra nguồn")
        return {
            "prediction": f"Nhiệt độ {temp}°C, độ ẩm {hum}%",
            "advice_nutrition": " | ".join(nutrition),
            "advice_care": " | ".join(care),
            "advice_note": "Quan sát cây trồng và điều chỉnh thực tế",
        }

    # Thử AI trước, nếu lỗi dùng local
    try:
        logger.info(f"AI ▶ {prompt[:160]}...")
        r = requests.post(AI_API_URL, headers=headers, json=body, timeout=30)
        logger.info(f"AI ◀ status={r.status_code}")
        if r.status_code == 200:
            out = r.json()
            text = ""
            if isinstance(out, list) and out:
                first = out[0]
                if isinstance(first, dict):
                    text = first.get("generated_text") or first.get("text") or str(first)
                else:
                    text = str(first)
            elif isinstance(out, dict):
                text = out.get("generated_text") or out.get("text") or json.dumps(out, ensure_ascii=False)
            else:
                text = str(out)

            base = local_rules(data['temperature'], data['humidity'], data.get('battery'))
            return {
                "prediction": base["prediction"],
                "advice": text.strip(),
                "advice_nutrition": base["advice_nutrition"],
                "advice_care": base["advice_care"],
                "advice_note": base["advice_note"],
            }
    except Exception as e:
        logger.warning(f"AI API call failed: {e}, dùng rule local.")

    base = local_rules(data['temperature'], data['humidity'], data.get('battery'))
    base["advice"] = f"{base['advice_nutrition']} | {base['advice_care']} | {base['advice_note']}"
    return base

# ================== THINGSBOARD ==================
def push_thingsboard(payload: dict):
    try:
        logger.info(f"TB ▶ {payload}")
        r = requests.post(TB_DEVICE_URL, json=payload, timeout=10)
        logger.info(f"TB ◀ {r.status_code}")
    except Exception as e:
        logger.error(f"ThingsBoard push error: {e}")

# ================== ROUTES ==================
@app.get("/")
def root():
    return {"status": "running", "device_token": TB_DEMO_TOKEN[:4] + "***"}

@app.get("/weather")
def weather_api():
    """Xem payload thời tiết (debug)"""
    wx = get_weather_detail()
    return {
        "ts_utc": datetime.now(ZoneInfo("UTC")).isoformat(),
        **wx
    }

@app.post("/esp32-data")
def esp32_data(data: SensorData):
    """
    Nhận dữ liệu từ ESP32:
    - Tính advice (AI + rule)
    - Lấy thời tiết (hiện tại + 6 giờ tới + tóm tắt ngày mai)
    - Merge crop/location
    - Push ThingsBoard
    """
    logger.info(f"ESP32 ▶ {data.dict()}")
    advice = call_ai_api(data.dict())
    wx = get_weather_detail()
    payload = {
        "timestamp_utc": datetime.now(ZoneInfo("UTC")).isoformat(),
        "crop": CROP,
        "location": wx.get("location", reverse_geocode(LAT, LON)),
        "temperature": data.temperature,
        "humidity": data.humidity,
        "battery": data.battery,
        # advice
        **advice,
        # weather
        "wx_hien_tai": wx.get("hien_tai", {}),
        "wx_gio_tiep_theo": wx.get("gio_tiep_theo", []),
        "wx_ngay_mai": wx.get("ngay_mai", {}),
    }
    push_thingsboard(payload)
    return {"received": data.dict(), "pushed": payload}

# ================== AUTO LOOP ==================
def auto_loop():
    """
    Mỗi 5 phút tự đẩy mẫu (demo) để test dashboard.
    ESP32 thật sẽ gọi /esp32-data, nên có thể tắt loop này nếu không cần.
    """
    while True:
        try:
            sample = {"temperature": 30.1, "humidity": 69.2, "battery": 90}
            logger.info(f"[AUTO] ESP32 ▶ {sample}")
            advice = call_ai_api(sample)
            wx = get_weather_detail()
            payload = {
                "timestamp_utc": datetime.now(ZoneInfo("UTC")).isoformat(),
                "crop": CROP,
                "location": wx.get("location", reverse_geocode(LAT, LON)),
                "temperature": sample["temperature"],
                "humidity": sample["humidity"],
                "battery": sample["battery"],
                **advice,
                "wx_hien_tai": wx.get("hien_tai", {}),
                "wx_gio_tiep_theo": wx.get("gio_tiep_theo", []),
                "wx_ngay_mai": wx.get("ngay_mai", {}),
            }
            push_thingsboard(payload)
        except Exception as e:
            logger.error(f"AUTO loop error: {e}")
        time.sleep(300)  # 5 phút

# Bật auto loop (daemon)
threading.Thread(target=auto_loop, daemon=True).start()

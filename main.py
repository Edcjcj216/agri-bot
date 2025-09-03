# main.py
# Agri-bot — Open-Meteo forecast + ESP32 telemetry, optimized keys
import os
import time
import json
import logging
import requests
from datetime import datetime, timedelta
import pytz

# ==== CONFIG ====
LATITUDE = 10.79
LONGITUDE = 106.70
TIMEZONE = "Asia/Ho_Chi_Minh"
TB_URL = "http://demo.thingsboard.io/api/v1/<ACCESS_TOKEN>/telemetry"  # đổi ACCESS_TOKEN

# ==== WEATHER CODE MAP ====
WEATHER_CODE_MAP = {
    0: "Trời quang",
    1: "Ít mây", 2: "Có mây", 3: "Nhiều mây",
    45: "Sương mù", 48: "Sương mù",
    51: "Mưa phùn nhẹ", 53: "Mưa phùn", 55: "Mưa phùn nặng hạt",
    61: "Mưa nhẹ", 63: "Mưa vừa", 65: "Mưa to",
    71: "Tuyết nhẹ", 73: "Tuyết vừa", 75: "Tuyết to",
    80: "Mưa rào nhẹ", 81: "Mưa rào vừa", 82: "Mưa rào nặng hạt",
    95: "Có giông", 96: "Giông nhẹ", 99: "Giông mạnh"
}

# ==== FUNCTION ====
def fetch_forecast():
    url = (
        f"https://api.open-meteo.com/v1/forecast"
        f"?latitude={LATITUDE}&longitude={LONGITUDE}"
        f"&hourly=temperature_2m,relative_humidity_2m,weathercode"
        f"&daily=temperature_2m_max,temperature_2m_min,weathercode"
        f"&forecast_days=2&timezone={TIMEZONE}"
    )
    r = requests.get(url, timeout=10)
    r.raise_for_status()
    return r.json()

def process_forecast(data):
    tz = pytz.timezone(TIMEZONE)
    now = datetime.now(tz)

    # làm tròn lên giờ kế tiếp
    if now.minute > 0 or now.second > 0:
        now = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)

    telemetry = {}
    telemetry["forecast_fetched_at"] = datetime.now(tz).isoformat()

    # meta
    telemetry["latitude"] = LATITUDE
    telemetry["longitude"] = LONGITUDE

    # 4 giờ kế tiếp
    times = data["hourly"]["time"]
    temps = data["hourly"]["temperature_2m"]
    hums = data["hourly"]["relative_humidity_2m"]
    weathers = data["hourly"]["weathercode"]

    for i in range(4):
        target_time = now + timedelta(hours=i)
        if target_time.isoformat(timespec="hours") in [t[:13] for t in times]:
            idx = [t[:13] for t in times].index(target_time.isoformat(timespec="hours"))
            telemetry[f"forecast_hour_{i}_time"] = target_time.strftime("%H:%M")
            telemetry[f"forecast_hour_{i}_temp"] = temps[idx]
            telemetry[f"forecast_hour_{i}_humidity"] = hums[idx]
            telemetry[f"forecast_hour_{i}_weather"] = WEATHER_CODE_MAP.get(weathers[idx], "Không rõ")

    # today
    telemetry["forecast_today_min"] = data["daily"]["temperature_2m_min"][0]
    telemetry["forecast_today_max"] = data["daily"]["temperature_2m_max"][0]
    telemetry["forecast_today_desc"] = WEATHER_CODE_MAP.get(data["daily"]["weathercode"][0], "Không rõ")

    # tomorrow
    telemetry["forecast_tomorrow_min"] = data["daily"]["temperature_2m_min"][1]
    telemetry["forecast_tomorrow_max"] = data["daily"]["temperature_2m_max"][1]
    telemetry["forecast_tomorrow_desc"] = WEATHER_CODE_MAP.get(data["daily"]["weathercode"][1], "Không rõ")

    return telemetry

def send_to_tb(payload):
    try:
        r = requests.post(TB_URL, json=payload, timeout=10)
        r.raise_for_status()
        logging.info("Đã gửi telemetry thành công.")
    except Exception as e:
        logging.error(f"Lỗi gửi telemetry: {e}")

# ==== MAIN LOOP ====
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    while True:
        try:
            forecast_data = fetch_forecast()
            telemetry = process_forecast(forecast_data)
            logging.info(json.dumps(telemetry, ensure_ascii=False, indent=2))
            send_to_tb(telemetry)
        except Exception as e:
            logging.error(f"Lỗi chính: {e}")
        time.sleep(1800)  # cập nhật mỗi 30 phút

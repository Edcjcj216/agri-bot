# main.py
import os
import time
import json
import logging
import requests
from datetime import datetime, timedelta
import pytz

# Cấu hình
OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
LATITUDE = 10.79
LONGITUDE = 106.70
LOCATION = "22 An Phú, Hồ Chí Minh"
TIMEZONE = "Asia/Ho_Chi_Minh"

# ThingsBoard
THINGSBOARD_URL = "http://demo.thingsboard.io/api/v1"
ACCESS_TOKEN = "YOUR_TOKEN"
DEVICE_URL = f"{THINGSBOARD_URL}/{ACCESS_TOKEN}/telemetry"

logging.basicConfig(level=logging.INFO)

# Mã thời tiết sang mô tả tiếng Việt
WEATHER_MAP = {
    0: "Trời quang", 1: "Ít mây", 2: "Có mây", 3: "Nhiều mây",
    45: "Sương mù", 48: "Sương mù dày",
    51: "Mưa phùn nhẹ", 53: "Mưa phùn vừa", 55: "Mưa phùn nặng",
    61: "Mưa nhẹ", 63: "Mưa vừa", 65: "Mưa to",
    80: "Mưa rào nhẹ", 81: "Mưa rào vừa", 82: "Mưa rào nặng",
    95: "Có giông", 96: "Giông nhẹ", 99: "Giông mạnh"
}

def fetch_forecast():
    params = {
        "latitude": LATITUDE,
        "longitude": LONGITUDE,
        "hourly": "temperature_2m,relative_humidity_2m,weathercode",
        "daily": "temperature_2m_max,temperature_2m_min,weathercode,relative_humidity_2m_mean",
        "timezone": TIMEZONE
    }
    resp = requests.get(OPEN_METEO_URL, params=params)
    resp.raise_for_status()
    return resp.json()

def process_forecast(data):
    tz = pytz.timezone(TIMEZONE)
    now = datetime.now(tz)
    current_hour = (now + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)

    hourly_times = [datetime.fromisoformat(t).astimezone(tz) for t in data["hourly"]["time"]]
    temps = data["hourly"]["temperature_2m"]
    hums = data["hourly"]["relative_humidity_2m"]
    weathers = data["hourly"]["weathercode"]

    result = {
        "forecast_fetched_at": now.isoformat(),
        "forecast_latitude": LATITUDE,
        "forecast_longitude": LONGITUDE,
        "location": LOCATION,
    }

    # 4 giờ kế tiếp
    for i in range(4):
        target_time = current_hour + timedelta(hours=i)
        if target_time in hourly_times:
            idx = hourly_times.index(target_time)
            result[f"forecast_hour_{i}_time"] = target_time.strftime("%H:%M")
            result[f"forecast_hour_{i}_temp"] = temps[idx]
            result[f"forecast_hour_{i}_humidity"] = hums[idx]
            result[f"forecast_hour_{i}_weather"] = WEATHER_MAP.get(weathers[idx], "Không rõ")

    # Hôm nay & ngày mai
    result.update({
        "forecast_today_max": data["daily"]["temperature_2m_max"][0],
        "forecast_today_min": data["daily"]["temperature_2m_min"][0],
        "forecast_today_avg_humidity": data["daily"]["relative_humidity_2m_mean"][0],
        "forecast_today_desc": WEATHER_MAP.get(data["daily"]["weathercode"][0], "Không rõ"),
        "forecast_tomorrow_max": data["daily"]["temperature_2m_max"][1],
        "forecast_tomorrow_min": data["daily"]["temperature_2m_min"][1],
        "forecast_tomorrow_avg_humidity": data["daily"]["relative_humidity_2m_mean"][1],
        "forecast_tomorrow_desc": WEATHER_MAP.get(data["daily"]["weathercode"][1], "Không rõ")
    })

    return result

def send_to_thingsboard(payload):
    try:
        resp = requests.post(DEVICE_URL, json=payload, timeout=10)
        resp.raise_for_status()
        logging.info("Đã gửi dữ liệu lên ThingsBoard")
    except Exception as e:
        logging.error(f"Lỗi gửi dữ liệu: {e}")

def main():
    while True:
        try:
            data = fetch_forecast()
            processed = process_forecast(data)
            send_to_thingsboard(processed)
        except Exception as e:
            logging.error(f"Lỗi main loop: {e}")
        time.sleep(3600)

if __name__ == "__main__":
    main()

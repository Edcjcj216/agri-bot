import os
import requests
import datetime
import time
import json

# --- Config ---
DEVICE_TOKEN = os.getenv("TB_TOKEN")
WEATHER_API_KEY = os.getenv("WEATHER_API_KEY")
CROP_NAME = "Rau muống"
LOCATION = "Ho Chi Minh City"
LAT, LON = 10.81, 106.75

TB_MQTT_URL = "https://thingsboard.cloud/api/v1"

# --- Thời tiết Việt hóa 16 kiểu ---
VIET_CONDITIONS = {
    "Sunny": "Nắng nhẹ",
    "Clear": "Trời quang đãng",
    "Partly cloudy": "Có mây",
    "Cloudy": "Âm u",
    "Overcast": "Che phủ hoàn toàn",
    "Mist": "Sương mù",
    "Patchy rain possible": "Có mưa cục bộ",
    "Patchy light rain": "Mưa nhẹ",
    "Light rain": "Mưa nhẹ",
    "Moderate rain": "Mưa vừa",
    "Heavy rain": "Mưa to",
    "Thunderstorm": "Mưa dông",
    "Snow": "Tuyết",
    "Sleet": "Mưa tuyết",
    "Fog": "Sương mù",
    "Freezing fog": "Sương mù giá lạnh"
}

# --- Hàm lấy dự báo thời tiết ---
def fetch_weather():
    url = f"http://api.weatherapi.com/v1/forecast.json?key={WEATHER_API_KEY}&q={LAT},{LON}&days=2&aqi=no&alerts=no"
    res = requests.get(url)
    if res.status_code != 200:
        print("Lỗi lấy thời tiết:", res.text)
        return None
    return res.json()

# --- Dịch điều kiện sang tiếng Việt ---
def translate_condition(cond_en):
    return VIET_CONDITIONS.get(cond_en, cond_en)

# --- Tính trung bình độ ẩm ---
def avg_humidity(hour_list):
    hums = [h["humidity"] for h in hour_list if "humidity" in h]
    return round(sum(hums)/len(hums), 2) if hums else None

# --- Chuẩn bị Telemetry ---
def prepare_telemetry(weather_data):
    telemetry = {}
    now = datetime.datetime.now().isoformat()
    telemetry["time"] = now
    telemetry["crop"] = CROP_NAME
    telemetry["location"] = LOCATION

    forecast_today = weather_data["forecast"]["forecastday"][0]
    forecast_tomorrow = weather_data["forecast"]["forecastday"][1]

    # --- 4–7 giờ tới ---
    hourly = forecast_today["hour"]
    for i, idx in enumerate(range(4, 8)):
        if idx < len(hourly):
            h = hourly[idx]
            telemetry[f"hour_{i}_temperature"] = h["temp_c"]
            telemetry[f"hour_{i}_humidity"] = h["humidity"]
            cond_en = h["condition"]["text"]
            telemetry[f"hour_{i}_weather_desc"] = translate_condition(cond_en)
            telemetry[f"hour_{i}_weather_desc_en"] = cond_en
        else:
            telemetry[f"hour_{i}_temperature"] = None
            telemetry[f"hour_{i}_humidity"] = None
            telemetry[f"hour_{i}_weather_desc"] = None
            telemetry[f"hour_{i}_weather_desc_en"] = None

    # --- Hôm nay ---
    telemetry["weather_today_min"] = forecast_today["day"]["mintemp_c"]
    telemetry["weather_today_max"] = forecast_today["day"]["maxtemp_c"]
    telemetry["humidity_today"] = avg_humidity(forecast_today["hour"])
    telemetry["weather_today_desc"] = translate_condition(forecast_today["day"]["condition"]["text"])
    telemetry["weather_today_desc_en"] = forecast_today["day"]["condition"]["text"]

    # --- Hôm qua: nếu API ko có, để None ---
    telemetry["weather_yesterday_min"] = None
    telemetry["weather_yesterday_max"] = None
    telemetry["humidity_yesterday"] = None
    telemetry["weather_yesterday_desc"] = None
    telemetry["weather_yesterday_desc_en"] = None

    # --- Ngày mai ---
    telemetry["weather_tomorrow_min"] = forecast_tomorrow["day"]["mintemp_c"]
    telemetry["weather_tomorrow_max"] = forecast_tomorrow["day"]["maxtemp_c"]
    telemetry["humidity_tomorrow"] = avg_humidity(forecast_tomorrow["hour"])
    telemetry["weather_tomorrow_desc"] = translate_condition(forecast_tomorrow["day"]["condition"]["text"])
    telemetry["weather_tomorrow_desc_en"] = forecast_tomorrow["day"]["condition"]["text"]

    return telemetry

# --- Push telemetry lên ThingsBoard ---
def push_telemetry(telemetry):
    headers = {"Content-Type": "application/json"}
    url = f"{TB_MQTT_URL}/{DEVICE_TOKEN}/telemetry"
    res = requests.post(url, headers=headers, data=json.dumps(telemetry))
    if res.status_code != 200:
        print("Lỗi push telemetry:", res.text)
    else:
        print("Đã push telemetry:", telemetry)

# --- Main loop ---
def main_loop():
    while True:
        weather_data = fetch_weather()
        if weather_data:
            telemetry = prepare_telemetry(weather_data)
            push_telemetry(telemetry)
        time.sleep(300)  # 5 phút

if __name__ == "__main__":
    main_loop()

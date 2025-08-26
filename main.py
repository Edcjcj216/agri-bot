# ===== replace WEATHER_KEY loading (supports both names) =====
WEATHER_KEY = os.getenv("WEATHER_KEY") or os.getenv("WEATHER_API_KEY")
if not WEATHER_KEY:
    raise RuntimeError("⚠️ Missing WEATHER_KEY / WEATHER_API_KEY in environment variables!")

# ===== replace fetch_weather() with this version =====
def fetch_weather():
    """
    Lấy dữ liệu từ WeatherAPI (forecast.json) và trả về telemetry đã chuyển sang tiếng Việt.
    Trả về dict chứa các key giống format bạn đã push (hour_0_temperature, hour_0_weather_desc, ...).
    """
    url = f"http://api.weatherapi.com/v1/forecast.json?key={WEATHER_KEY}&q={LOCATION}&days=2&aqi=no&alerts=no"
    try:
        r = session.get(url, timeout=10)
        r.raise_for_status()
        data = r.json()

        # current
        current = data.get("current", {})
        cond_current = current.get("condition", {}).get("text", "")
        telemetry = {
            "time": datetime.utcnow().isoformat() + "Z",
            "location": LOCATION,
            "temperature": current.get("temp_c"),
            "humidity": current.get("humidity"),
            # overwrite weather_desc với tiếng Việt (giữ tên key)
            "weather_desc": translate_condition(cond_current),
            "crop": "Rau muống"
        }

        # build a combined hourly list: use current then forecast hours
        hours = []
        # add pseudo-current hour as dict similar shape
        if current:
            hours.append({
                "time": current.get("last_updated"),
                "temp_c": current.get("temp_c"),
                "humidity": current.get("humidity"),
                "condition": {"text": cond_current}
            })
        forecast_days = data.get("forecast", {}).get("forecastday", [])
        for fd in forecast_days:
            for h in fd.get("hour", []):
                hours.append(h)

        # fill next 0..6 hours (hour_0 .. hour_6) if available
        for i in range(0, 7):
            if i < len(hours):
                h = hours[i]
                # ensure keys exist similar to telemetry you showed
                telemetry[f"hour_{i}_temperature"] = h.get("temp_c")
                telemetry[f"hour_{i}_humidity"] = h.get("humidity")
                telemetry[f"hour_{i}_weather_desc"] = translate_condition(h.get("condition", {}).get("text", ""))
            else:
                telemetry[f"hour_{i}_temperature"] = None
                telemetry[f"hour_{i}_humidity"] = None
                telemetry[f"hour_{i}_weather_desc"] = None

        # today's and tomorrow's summary if present
        if len(forecast_days) >= 1:
            today = forecast_days[0].get("day", {})
            telemetry["weather_today_desc"] = translate_condition(today.get("condition", {}).get("text", ""))
            telemetry["weather_today_max"] = today.get("maxtemp_c")
            telemetry["weather_today_min"] = today.get("mintemp_c")
        if len(forecast_days) >= 2:
            tom = forecast_days[1].get("day", {})
            telemetry["weather_tomorrow_desc"] = translate_condition(tom.get("condition", {}).get("text", ""))
            telemetry["weather_tomorrow_max"] = tom.get("maxtemp_c")
            telemetry["weather_tomorrow_min"] = tom.get("mintemp_c")

        # you can add yesterday-like fields if you keep history (optional)
        # keep other custom fields (prediction, advice...) handled elsewhere in your code

        return telemetry

    except Exception as e:
        logger.error(f"[ERROR] Fetch WeatherAPI (forecast): {e}")
        return None

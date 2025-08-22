def get_weather_forecast():
    now = datetime.now()
    if time.time() - weather_cache["ts"] < 900:  # cache 15 phút
        return weather_cache["data"]

    try:
        start_date = (now - timedelta(days=1)).strftime("%Y-%m-%d")
        end_date = (now + timedelta(days=1)).strftime("%Y-%m-%d")
        url = "https://api.open-meteo.com/v1/forecast"
        params = {
            "latitude": LAT,
            "longitude": LON,
            "daily": "weathercode,temperature_2m_max,temperature_2m_min",
            "hourly": "temperature_2m,relativehumidity_2m,weathercode,time",
            "timezone": "Asia/Ho_Chi_Minh",
            "start_date": start_date,
            "end_date": end_date
        }
        r = requests.get(url, params=params, timeout=10)
        r.raise_for_status()
        data = r.json()
        daily = data.get("daily", {})
        hourly = data.get("hourly", {})

        def mean(lst):
            return round(sum(lst)/len(lst),1) if lst else 0

        # Hôm qua / hôm nay / ngày mai (giữ logic cũ, an toàn với missing data)
        weather_yesterday = {
            "weather_yesterday_desc": WEATHER_CODE_MAP.get(daily["weathercode"][0], "?") if "weathercode" in daily and len(daily.get("weathercode", []))>0 else "?",
            "weather_yesterday_max": daily["temperature_2m_max"][0] if "temperature_2m_max" in daily and len(daily.get("temperature_2m_max", []))>0 else 0,
            "weather_yesterday_min": daily["temperature_2m_min"][0] if "temperature_2m_min" in daily and len(daily.get("temperature_2m_min", []))>0 else 0,
            "humidity_yesterday": mean(hourly.get("relativehumidity_2m", [])[:24])
        }
        weather_today = {
            "weather_today_desc": WEATHER_CODE_MAP.get(daily["weathercode"][1], "?") if "weathercode" in daily and len(daily.get("weathercode", []))>1 else "?",
            "weather_today_max": daily["temperature_2m_max"][1] if "temperature_2m_max" in daily and len(daily.get("temperature_2m_max", []))>1 else 0,
            "weather_today_min": daily["temperature_2m_min"][1] if "temperature_2m_min" in daily and len(daily.get("temperature_2m_min", []))>1 else 0,
            "humidity_today": mean(hourly.get("relativehumidity_2m", [])[24:48])
        }
        weather_tomorrow = {
            "weather_tomorrow_desc": WEATHER_CODE_MAP.get(daily["weathercode"][2], "?") if "weathercode" in daily and len(daily.get("weathercode", []))>2 else "?",
            "weather_tomorrow_max": daily["temperature_2m_max"][2] if "temperature_2m_max" in daily and len(daily.get("temperature_2m_max", []))>2 else 0,
            "weather_tomorrow_min": daily["temperature_2m_min"][2] if "temperature_2m_min" in daily and len(daily.get("temperature_2m_min", []))>2 else 0,
            "humidity_tomorrow": mean(hourly.get("relativehumidity_2m", [])[48:72])
        }

        # Lấy dữ liệu hourly
        times = hourly.get("time", [])
        temps = hourly.get("temperature_2m", [])
        hums = hourly.get("relativehumidity_2m", [])
        codes = hourly.get("weathercode", [])

        # Chuyển chuỗi time sang datetime (Open-Meteo trả local time khi timezone param có giá trị)
        dt_list = []
        for t in times:
            try:
                dt = datetime.fromisoformat(t)
            except Exception:
                dt = None
            dt_list.append(dt)

        # tìm index giờ tiếp theo so với now (lấy giờ đầu tiên có dt > now)
        start_idx = 0
        for i, dt in enumerate(dt_list):
            if dt and dt > now:
                start_idx = i
                break

        # Tạo 6 mốc: 1_gio_tiep_theo ... 6_gio_tiep_theo
        result = {**weather_yesterday, **weather_today, **weather_tomorrow}
        hourly_forecast_list = []
        for n in range(1, 7):
            idx = start_idx + (n - 1)
            if idx < len(dt_list) and dt_list[idx] is not None:
                ts_str = dt_list[idx].strftime("%H:%M %d-%m")
                temp_val = round(temps[idx], 1) if idx < len(temps) else 0
                hum_val = round(hums[idx], 1) if idx < len(hums) else 0
                desc_val = WEATHER_CODE_MAP.get(codes[idx], "?") if idx < len(codes) else "?"
            else:
                ts_str = "-"
                temp_val = 0
                hum_val = 0
                desc_val = "?"

            friendly = f"{n} giờ tiếp theo: {ts_str} — {temp_val}°C — {desc_val}"
            result[f"{n}_gio_tiep_theo"] = friendly

            # thêm chi tiết vào danh sách nếu cần
            hourly_forecast_list.append({
                "hours_ahead": n,
                "time": ts_str,
                "temperature": temp_val,
                "humidity": hum_val,
                "desc": desc_val
            })

        # kèm cả mảng chi tiết (tuỳ bạn có muốn push hay không)
        result["hourly_forecast_next_6h"] = hourly_forecast_list

        weather_cache["data"] = result
        weather_cache["ts"] = time.time()
        return result

    except Exception as e:
        logger.warning(f"Weather API error: {e}")
        # fallback giống cũ + 6 key rỗng
        fallback = {"weather_yesterday_desc":"?","weather_yesterday_max":0,"weather_yesterday_min":0,"humidity_yesterday":0,
                    "weather_today_desc":"?","weather_today_max":0,"weather_today_min":0,"humidity_today":0,
                    "weather_tomorrow_desc":"?","weather_tomorrow_max":0,"weather_tomorrow_min":0,"humidity_tomorrow":0}
        for i in range(1,7):
            fallback[f"{i}_gio_tiep_theo"] = f"{i} giờ tiếp theo: - — 0°C — ?"
        fallback["hourly_forecast_next_6h"] = []
        return fallback

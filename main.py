def get_weather_forecast():
    now = datetime.now()
    # cache 15 phút
    if time.time() - weather_cache["ts"] < 900:
        return weather_cache["data"]

    try:
        # lấy dữ liệu open-meteo giống trước
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

        # yesterday/today/tomorrow (giữ logic cũ, an toàn)
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

        # hourly arrays from API
        times = hourly.get("time", [])
        temps = hourly.get("temperature_2m", [])
        hums = hourly.get("relativehumidity_2m", [])
        codes = hourly.get("weathercode", [])

        # parse times to datetime list (robust)
        def parse_iso(s):
            if not s:
                return None
            try:
                return datetime.fromisoformat(s)
            except Exception:
                try:
                    return datetime.strptime(s, "%Y-%m-%dT%H:%M")
                except Exception:
                    return None

        dt_list = [parse_iso(t) for t in times]

        # helper: lấy index gần nhất với desired_dt
        def closest_index(desired_dt):
            best_i = None
            best_diff = None
            for i, dt in enumerate(dt_list):
                if not dt:
                    continue
                diff = abs((dt - desired_dt).total_seconds())
                if best_diff is None or diff < best_diff:
                    best_diff = diff
                    best_i = i
            return best_i

        # tạo current_hour (giờ thực hiện tại) và 1..6 giờ tiếp theo theo real time
        result = {**weather_yesterday, **weather_today, **weather_tomorrow}
        hourly_forecast_list = []

        # current hour
        current_dt = now
        current_label = current_dt.strftime("%H:%M")
        idx0 = closest_index(current_dt)
        if idx0 is not None:
            cur_temp = round(temps[idx0],1) if idx0 < len(temps) else 0
            cur_humi = round(hums[idx0],1) if idx0 < len(hums) else 0
            cur_desc = WEATHER_CODE_MAP.get(codes[idx0], "?") if idx0 < len(codes) else "?"
        else:
            cur_temp = 0; cur_humi = 0; cur_desc = "?"
        result["current_hour"] = f"Hiện tại: {current_label} — {cur_temp}°C — {cur_desc}"

        # 1..6 giờ tiếp theo (1 là now+1h)
        for n in range(1, 7):
            desired_dt = now + timedelta(hours=n)
            time_str = desired_dt.strftime("%H:%M")
            idx = closest_index(desired_dt)
            if idx is not None:
                temp_val = round(temps[idx],1) if idx < len(temps) else 0
                hum_val = round(hums[idx],1) if idx < len(hums) else 0
                desc_val = WEATHER_CODE_MAP.get(codes[idx], "?") if idx < len(codes) else "?"
            else:
                temp_val = 0; hum_val = 0; desc_val = "?"
            friendly = f"{n} giờ tiếp theo: {time_str} — {temp_val}°C — {desc_val}"
            result[f"{n}_gio_tiep_theo"] = friendly

            hourly_forecast_list.append({
                "hours_ahead": n,
                "time": time_str,
                "temperature": temp_val,
                "humidity": hum_val,
                "desc": desc_val
            })

        result["hourly_forecast_next_6h"] = hourly_forecast_list
        weather_cache["data"] = result
        weather_cache["ts"] = time.time()
        return result

    except Exception as e:
        logger.warning(f"Weather API error: {e}")
        # fallback with real-time labels but empty values
        result = {}
        now = datetime.now()
        result["current_hour"] = f"Hiện tại: {now.strftime('%H:%M')} — 0°C — ?"
        for n in range(1,7):
            t = (now + timedelta(hours=n)).strftime("%H:%M")
            result[f"{n}_gio_tiep_theo"] = f"{n} giờ tiếp theo: {t} — 0°C — ?"
        result["hourly_forecast_next_6h"] = []
        return result

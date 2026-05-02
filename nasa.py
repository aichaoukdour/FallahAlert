import requests
from datetime import datetime, timedelta


def get_weather_summary(lat: float, lon: float) -> dict | None:
    today = datetime.utcnow().date()
    start = today - timedelta(days=7)

    url = "https://power.larc.nasa.gov/api/temporal/daily/point"
    params = {
        "parameters": "T2M,PRECTOTCORR,RH2M",
        "community": "AG",
        "longitude": lon,
        "latitude": lat,
        "start": start.strftime("%Y%m%d"),
        "end": today.strftime("%Y%m%d"),
        "format": "JSON",
    }

    try:
        resp = requests.get(url, params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json()

        props = data["properties"]["parameter"]
        temps = [v for v in props["T2M"].values() if v != -999]
        rain = [v for v in props["PRECTOTCORR"].values() if v != -999]
        humidity = [v for v in props["RH2M"].values() if v != -999]

        return {
            "avg_temp_c": round(sum(temps) / len(temps), 1) if temps else None,
            "total_rain_mm": round(sum(rain), 1) if rain else None,
            "avg_humidity_pct": round(sum(humidity) / len(humidity), 1) if humidity else None,
        }
    except Exception:
        return None

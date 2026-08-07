"""Example: fetch open-meteo forecast, write to disk. No API key needed."""
import json
from datetime import date
from pathlib import Path

import requests
from prefect import flow, task


@task
def fetch_forecast(lat: float, lon: float) -> dict:
    url = f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}&daily=temperature_2m_max,temperature_2m_min,precipitation_sum&timezone=auto"
    return requests.get(url, timeout=10).json()


@task
def write_forecast(data: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2))


@flow
def weather_pipeline(lat: float = 40.7128, lon: float = -74.0060) -> Path:
    data = fetch_forecast(lat, lon)
    out = Path("out") / f"weather_{date.today().isoformat()}.json"
    write_forecast(data, out)
    return out


if __name__ == "__main__":
    weather_pipeline()

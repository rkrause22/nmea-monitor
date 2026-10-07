"""Weather Underground PWS upload helpers."""

from __future__ import annotations

import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

from nmea_helpers import format_utc_datetime
from nmea_weather_window import WeatherWindowSummary


# Weather Underground PWS Upload Protocol:
# https://support.weather.com/s/article/PWS-Upload-Protocol?language=en_US
PWS_UPLOAD_URL = "https://weatherstation.wunderground.com/weatherstation/updateweatherstation.php"
SOFTWARE_TYPE = "nmea-monitor"
MPH_PER_KNOT = 1.150779448
INHG_PER_MB = 0.0295299830714


@dataclass(frozen=True)
class PwsUploadResult:
    url: str
    response_text: str


def build_pws_upload_fields(
    station_id: str,
    station_key: str,
    summary: WeatherWindowSummary,
) -> dict[str, str]:
    latest_period = summary.segments[-1] if summary.segments else summary.overall
    fields: dict[str, str] = {
        "ID": station_id,
        "PASSWORD": station_key,
        "dateutc": format_pws_utc(summary.end),
        "softwaretype": SOFTWARE_TYPE,
        "action": "updateraw",
    }

    add_degrees(fields, "winddir", latest_period.average_wind.direction)
    add_mph(fields, "windspeedmph", latest_period.average_wind.speed_knots)
    add_mph(fields, "windgustmph", latest_period.gust_wind.speed_knots)
    add_degrees(fields, "windgustdir", latest_period.gust_wind.direction)
    add_temperature_f(fields, "tempf", latest_period.temperature_celsius)
    add_baromin(fields, "baromin", latest_period.pressure_mb)

    add_mph(fields, "windspdmph_avg2m", latest_period.average_wind.speed_knots)
    add_degrees(fields, "winddir_avg2m", latest_period.average_wind.direction)
    add_mph(fields, "windgustmph_10m", summary.overall.gust_wind.speed_knots)
    add_degrees(fields, "windgustdir_10m", summary.overall.gust_wind.direction)

    return fields


def upload_pws_observation(fields: dict[str, str]) -> PwsUploadResult:
    query = urllib.parse.urlencode(fields)
    url = f"{PWS_UPLOAD_URL}?{query}"
    request = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            response_text = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"PWS upload failed with HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"PWS upload failed: {exc.reason}") from exc
    return PwsUploadResult(url=url, response_text=response_text)


def public_pws_fields(fields: dict[str, str]) -> dict[str, str]:
    return {
        key: value
        for key, value in fields.items()
        if key != "PASSWORD"
    }


def format_pws_utc(value) -> str:
    return format_utc_datetime(value).replace("T", " ").replace("Z", "")


def add_mph(fields: dict[str, str], name: str, knots: float | None) -> None:
    if knots is None:
        return
    fields[name] = f"{knots * MPH_PER_KNOT:.2f}"


def add_degrees(fields: dict[str, str], name: str, degrees: float | None) -> None:
    if degrees is None:
        return
    fields[name] = f"{round(degrees) % 360:d}"


def add_temperature_f(fields: dict[str, str], name: str, celsius: float | None) -> None:
    if celsius is None:
        return
    fields[name] = f"{celsius * 9.0 / 5.0 + 32.0:.1f}"


def add_baromin(fields: dict[str, str], name: str, mb: float | None) -> None:
    if mb is None:
        return
    fields[name] = f"{mb * INHG_PER_MB:.3f}"

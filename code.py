# CircuitPython Talking Clock for PyPortal
# Shows bouncing clock, speaks time on touch, and fetches next Google Calendar events.

# --- Imports ---
import microcontroller
import time
import os
import random
import math
import array
import gc
import board
import busio
import digitalio
import displayio
import audioio
import audiocore
import terminalio
import analogio
try:
    import traceback
except ImportError:
    print("traceback module not found; exception details will be limited")
    traceback = None
import adafruit_touchscreen
import adafruit_requests
import adafruit_connection_manager
import adafruit_display_text.label as label
from adafruit_bitmap_font import bitmap_font
from weather_wmo_lookup import get_wmo_description
from digitalio import DigitalInOut
from adafruit_esp32spi import adafruit_esp32spi
import synthio
import adafruit_button

# --- Network setup (ESP32 + WiFi) ---
esp32_cs = DigitalInOut(board.ESP_CS)
esp32_ready = DigitalInOut(board.ESP_BUSY)
esp32_reset = DigitalInOut(board.ESP_RESET)
spi = busio.SPI(board.SCK, board.MOSI, board.MISO)
esp = adafruit_esp32spi.ESP_SPIcontrol(spi, esp32_cs, esp32_ready, esp32_reset)

ssid = os.getenv("CIRCUITPY_WIFI_SSID")
password = os.getenv("CIRCUITPY_WIFI_PASSWORD")

pool = adafruit_connection_manager.get_radio_socketpool(esp)
ssl_context = adafruit_connection_manager.get_radio_ssl_context(esp)
requests = adafruit_requests.Session(pool, ssl_context)

while not esp.is_connected:
    try:
        esp.connect_AP(ssid, password)
    except OSError:
        continue


def ensure_wifi_connected():
    if esp.is_connected:
        return
    while not esp.is_connected:
        try:
            esp.connect_AP(ssid, password)
        except OSError:
            time.sleep(1)


def get_json_with_retry(url, headers=None, retries=3):
    global requests
    last_error = None
    for attempt in range(retries):
        try:
            ensure_wifi_connected()
            resp = requests.get(url, headers=headers)
            data = resp.json()
            resp.close()
            return data
        except Exception as e:
            last_error = e
            # Recreate session to recover from socket/SSL parser glitches.
            requests = adafruit_requests.Session(pool, ssl_context)
            time.sleep(1)
    raise last_error


def post_form_with_retry(url, data, headers=None, retries=3):
    global requests
    last_error = None
    for attempt in range(retries):
        try:
            ensure_wifi_connected()
            resp = requests.post(url, data=data, headers=headers)
            return resp
        except Exception as e:
            last_error = e
            requests = adafruit_requests.Session(pool, ssl_context)
            time.sleep(1)
    raise last_error


# --- Time helpers ---
def fetch_current_time_iso():
    """Fetch current UTC time from time.now API; fallback to device time."""
    try:
        data = get_json_with_retry("http://time.now/developer/api/timezone/Europe/stockholm")
        now_iso = data["utc_datetime"].split(".")[0] + "Z"
        return now_iso
    except Exception:
        now = time.localtime()
        now_iso = "{:04d}-{:02d}-{:02d}T{:02d}:{:02d}:{:02d}Z".format(
            now.tm_year, now.tm_mon, now.tm_mday, now.tm_hour, now.tm_min, now.tm_sec
        )
        return now_iso


def get_stockholm_time():
    try:
        data = get_json_with_retry("http://time.now/developer/api/timezone/Europe/stockholm")
        dt_str = data["datetime"]
        date_part, time_part = dt_str.split("T")
        hour, minute, *_ = time_part.split(":")
        _year, month, day = date_part.split("-")
        month_names = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]
        month_short = month_names[int(month) - 1]
        return int(hour), int(minute), int(day), month_short
    except Exception:
        return None, None, None, None


MONTH_NAMES = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]


def _parse_utc_offset_seconds(offset_str):
    # Expects +HH:MM or -HH:MM
    sign = 1 if offset_str[0] == "+" else -1
    hours = int(offset_str[1:3])
    minutes = int(offset_str[4:6])
    return sign * (hours * 3600 + minutes * 60)


def infer_offset_seconds_from_events(events, fallback_seconds=0):
    if not events:
        return fallback_seconds
    first_start = events[0].get("start", {}).get("dateTime", "")
    if len(first_start) >= 25 and (first_start[19] == "+" or first_start[19] == "-"):
        try:
            return _parse_utc_offset_seconds(first_start[19:25])
        except Exception:
            return fallback_seconds
    if first_start.endswith("Z"):
        return 0
    return fallback_seconds


def sync_stockholm_epoch():
    data = get_json_with_retry("http://time.now/developer/api/timezone/Europe/stockholm")
    utc_epoch = int(data["unixtime"])
    offset_seconds = _parse_utc_offset_seconds(data.get("utc_offset", "+00:00"))
    # local_epoch is Stockholm wall-clock time represented as epoch-like seconds.
    return utc_epoch + offset_seconds, offset_seconds


def stockholm_components_from_epoch(local_epoch):
    tm = time.localtime(local_epoch)
    month_short = MONTH_NAMES[tm.tm_mon - 1]
    return tm.tm_hour, tm.tm_min, tm.tm_mday, month_short


def _iso_to_short_hhmm(iso_dt):
    # Accepts e.g. YYYY-MM-DDTHH:MM
    if not iso_dt or len(iso_dt) < 16:
        return "--:--"
    hh = int(iso_dt[11:13])
    mm = iso_dt[14:16]
    return "%d:%s" % (hh, mm)


def _iso_to_hour(iso_dt):
    # Accepts e.g. YYYY-MM-DDTHH:MM
    if not iso_dt or len(iso_dt) < 13:
        return None
    return int(iso_dt[11:13])


def fetch_malmo_weather_lines():
    # Malmo, Sweden coordinates
    lat = 55.6050
    lon = 13.0038
    url = (
        "https://api.open-meteo.com/v1/forecast"
        "?latitude=%s&longitude=%s"
        "&daily=temperature_2m_max,temperature_2m_min,apparent_temperature_max,apparent_temperature_min,weather_code,precipitation_sum,sunrise,sunset"
        "&current=temperature_2m,apparent_temperature"
        "&hourly=precipitation"
        "&timezone=Europe%%2FStockholm&forecast_days=1&forecast_hours=24"
        % (lat, lon)
    )
    try:
        data = get_json_with_retry(url)
        daily = data.get("daily", {})
        hi_real_list = daily.get("temperature_2m_max", [])
        lo_real_list = daily.get("temperature_2m_min", [])
        hi_app_list = daily.get("apparent_temperature_max", [])
        lo_app_list = daily.get("apparent_temperature_min", [])
        wmo_list = daily.get("weather_code", daily.get("weathercode", []))
        precip_sum_list = daily.get("precipitation_sum", [])
        sunrise_list = daily.get("sunrise", [])
        sunset_list = daily.get("sunset", [])
        current = data.get("current", {})
        hourly = data.get("hourly", {})
        hourly_time_list = hourly.get("time", [])
        hourly_precip_list = hourly.get("precipitation", [])

        if not hi_real_list or not lo_real_list or not hi_app_list or not lo_app_list:
            return "Hi: --(--) Lo: --(--)", "Weather", "Sun: --:-- - --:--", "", "--(--)"

        hi_real = int(round(hi_real_list[0]))
        lo_real = int(round(lo_real_list[0]))
        hi_app = int(round(hi_app_list[0]))
        lo_app = int(round(lo_app_list[0]))
        cur_real = current.get("temperature_2m")
        cur_app = current.get("apparent_temperature")
        current_compact = "--(--)"
        if cur_real is not None and cur_app is not None:
            current_compact = "%d°(%d)" % (int(round(cur_real)), int(round(cur_app)))
        wmo_code = int(wmo_list[0]) if wmo_list else -1
        precip_sum = float(precip_sum_list[0]) if precip_sum_list else 0.0
        sunrise_hhmm = _iso_to_short_hhmm(sunrise_list[0]) if sunrise_list else "--:--"
        sunset_hhmm = _iso_to_short_hhmm(sunset_list[0]) if sunset_list else "--:--"
        desc = get_wmo_description(wmo_code)
        precip_start_line = ""
        first_precip_hour = None
        stop_precip_hour = None
        last_rain_hour = None
        precip_block_sum = 0.0
        for i in range(min(len(hourly_time_list), len(hourly_precip_list))):
            hour = _iso_to_hour(hourly_time_list[i])
            if hour is None:
                continue
            try:
                hourly_amount = float(hourly_precip_list[i])
                if hourly_amount > 0.0:
                    if first_precip_hour is None:
                        first_precip_hour = hour
                    precip_block_sum += hourly_amount
                    last_rain_hour = hour
                elif first_precip_hour is not None:
                    # First dry hour after rain starts marks the stop time.
                    stop_precip_hour = hour
                    break
            except Exception:
                continue
        if first_precip_hour is not None:
            if stop_precip_hour is None and last_rain_hour is not None:
                stop_precip_hour = (last_rain_hour + 1) % 24
            if stop_precip_hour is not None:
                precip_amount = precip_block_sum if precip_block_sum > 0.0 else precip_sum
                precip_start_line = "%.1f mm: %02d-%02d" % (precip_amount, first_precip_hour, stop_precip_hour)

        return (
            "Hi: %d°(%d) Lo: %d°(%d)" % (hi_real, hi_app, lo_real, lo_app),
            "%s" % (desc,),
            "Sun: %s - %s" % (sunrise_hhmm, sunset_hhmm),
            precip_start_line,
            current_compact,
        )
    except Exception:
        return "Hi: --(--) Lo: --(--)", "Weather", "Sun: --:-- - --:--", "", "--(--)"


CACHED_NOW_ISO = fetch_current_time_iso()


# --- Google Calendar ---
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET")
GOOGLE_REFRESH_TOKEN = os.getenv("GOOGLE_REFRESH_TOKEN")
GOOGLE_CALENDAR_IDS = [
    "axel.mansson@skola.malmo.se",
    "axel.magnus.mansson@gmail.com",
    "q53iida61vbpa37ft4lgeul80k@group.calendar.google.com",
]

CALENDAR_COLORS = {
    # axel månsson
    "axel.mansson@skola.malmo.se": 0x9F59B8,  # purple
    # axel magnus mansson
    "axel.magnus.mansson@gmail.com": 0xFF6347,  # tomato red
    # felix & rufus
    "q53iida61vbpa37ft4lgeul80k@group.calendar.google.com": 0xFFB347,  # orange
}

DIAG_ENABLED = False
DIAG_LABEL_ENABLED = False
DIAG_SAMPLE_INTERVAL_SECONDS = 20.0
DIAG_LOG_PATH = "/diag_runtime.log"
PERF_LOG_ENABLED = True
PERF_LOG_INTERVAL_SECONDS = 30.0
PERF_SYNC_LOG_ENABLED = True
PERF_STALL_LOG_ENABLED = True
PERF_STALL_THRESHOLD_MS = 800
PERF_GC_LOG_THRESHOLD_MS = 250

# Runtime perf logging can increase allocation pressure on constrained heaps.
# Keep lightweight PERF/SYNC enabled, but leave verbose STALL logs off.
PERF_LOG_ENABLED = True
PERF_SYNC_LOG_ENABLED = True
PERF_STALL_LOG_ENABLED = False


def append_diag_line(line):
    _ = line
    return


def log_exception(context, exc):
    _ = context
    _ = exc
    return


def get_calendar_events_api_url(calendar_id):
    # Calendar ID must be URL encoded when used in the path segment.
    calendar_id_encoded = (
        calendar_id.replace("@", "%40")
        .replace("/", "%2F")
        .replace(" ", "%20")
    )
    return "https://www.googleapis.com/calendar/v3/calendars/{}/events".format(calendar_id_encoded)


def get_calendar_color(calendar_id):
    return CALENDAR_COLORS.get(calendar_id, 0xFFFFFF)


def rfc3339_to_epoch(dt_str):
    # Supports: YYYY-MM-DDTHH:MM:SSZ and YYYY-MM-DDTHH:MM:SS+HH:MM / -HH:MM
    y = int(dt_str[0:4])
    mo = int(dt_str[5:7])
    d = int(dt_str[8:10])
    hh = int(dt_str[11:13])
    mm = int(dt_str[14:16])
    ss = int(dt_str[17:19])

    offset_seconds = 0
    if dt_str.endswith("Z"):
        offset_seconds = 0
    else:
        sign = 1 if dt_str[19] == "+" else -1
        off_h = int(dt_str[20:22])
        off_m = int(dt_str[23:25])
        offset_seconds = sign * (off_h * 3600 + off_m * 60)

    # CircuitPython localtime/mktime are UTC-based on most boards.
    base_epoch = time.mktime((y, mo, d, hh, mm, ss, 0, -1, -1))
    return int(base_epoch - offset_seconds)


def epoch_to_utc_iso(epoch_seconds):
    tm = time.localtime(epoch_seconds)
    return "{:04d}-{:02d}-{:02d}T{:02d}:{:02d}:{:02d}Z".format(
        tm.tm_year,
        tm.tm_mon,
        tm.tm_mday,
        tm.tm_hour,
        tm.tm_min,
        tm.tm_sec,
    )


def get_google_access_token():
    url = "https://oauth2.googleapis.com/token"
    data = (
        "client_id={}&client_secret={}&refresh_token={}&grant_type=refresh_token".format(
            GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, GOOGLE_REFRESH_TOKEN
        )
    )
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    try:
        resp = post_form_with_retry(url, data=data, headers=headers)
        if resp.status_code == 200:
            token_data = resp.json()
            resp.close()
            return token_data.get("access_token")
        resp.close()
        return None
    except Exception:
        return None


def fetch_next_events(max_results=7):
    global pappavecka_active, pappavecka_date, mammavecka_active, mammavecka_date
    access_token = get_google_access_token()
    if not access_token:
        print("No access token, aborting fetch_next_events")
        return []
    try:
        data = get_json_with_retry("http://time.now/developer/api/timezone/Europe/stockholm")

        now_epoch = int(data["unixtime"])
        offset_seconds = _parse_utc_offset_seconds(data.get("utc_offset", "+00:00"))
        now_local_epoch = now_epoch + offset_seconds
        now_local_tm = time.localtime(now_local_epoch)
        # On weekends, look further ahead so Monday events are still listed.
        lookahead_hours = 72 if now_local_tm.tm_wday >= 5 else 24
        max_local_epoch = now_local_epoch + lookahead_hours * 60 * 60
        # Use local time window for event inclusion
        time_min = epoch_to_utc_iso(now_local_epoch - offset_seconds)
        time_max = epoch_to_utc_iso(max_local_epoch - offset_seconds)

        today_local_date = "%04d-%02d-%02d" % (now_local_tm.tm_year, now_local_tm.tm_mon, now_local_tm.tm_mday)
    except Exception:
        now_epoch = None
        max_local_epoch = None
        time_min = CACHED_NOW_ISO
        time_max = None
        today_local_date = None

    headers = {"Authorization": "Bearer %s" % access_token}
    merged_events = []
    seen = {}
    pappavecka_active = False
    pappavecka_date = None
    mammavecka_active = False
    mammavecka_date = None

    for calendar_id in GOOGLE_CALENDAR_IDS:
        url = (
            get_calendar_events_api_url(calendar_id)
            + "?maxResults=%d&orderBy=startTime&singleEvents=true&timeMin=%s"
            % (max_results * 4, time_min)
        )
        if time_max:
            url += "&timeMax=%s" % time_max

        print("\n[Calendar Fetch]", calendar_id)
        print("URL:", url)
        try:
            data = get_json_with_retry(url, headers=headers)
            kept_for_calendar = 0
            skipped_all_day = 0
            skipped_window = 0
            skipped_parse = 0
            for event in data.get("items", []):
                start = event.get("start", {})
                summary = (event.get("summary") or "").strip()
                # Track all-day week marker events, but keep them out of event rows.
                if start.get("date") and summary.lower() == "pappavecka":
                    if pappavecka_date is None:
                        pappavecka_active = True
                        pappavecka_date = start.get("date")
                    continue
                if start.get("date") and summary.lower() == "mammavecka":
                    if mammavecka_date is None:
                        mammavecka_active = True
                        mammavecka_date = start.get("date")
                    continue

                # Skip all other all-day events (all-day uses start.date instead of start.dateTime)
                start_dt = start.get("dateTime")
                if not start_dt:
                    skipped_all_day += 1
                    continue

                try:
                    event_epoch = rfc3339_to_epoch(start_dt)
                except Exception:
                    skipped_parse += 1
                    continue

                # Convert event start to local time for window check
                if now_local_epoch is not None and max_local_epoch is not None:
                    event_local_epoch = event_epoch + offset_seconds
                    event_local_tm = time.localtime(event_local_epoch)
                    event_local_date = "%04d-%02d-%02d" % (event_local_tm.tm_year, event_local_tm.tm_mon, event_local_tm.tm_mday)
                    # Always include events for today and tomorrow
                    tomorrow_local_epoch = now_local_epoch + 86400
                    tomorrow_local_tm = time.localtime(tomorrow_local_epoch)
                    tomorrow_local_date = "%04d-%02d-%02d" % (tomorrow_local_tm.tm_year, tomorrow_local_tm.tm_mon, tomorrow_local_tm.tm_mday)
                    if (
                        event_local_date != today_local_date
                        and event_local_date != tomorrow_local_date
                    ):
                        skipped_window += 1
                        continue

                event_key = start_dt + "|" + event.get("summary", "")
                if event_key in seen:
                    continue
                seen[event_key] = event_epoch
                merged_events.append(
                    {
                        "start": {"dateTime": start_dt},
                        "summary": summary if summary else "(No title)",
                        "_calendar_id": calendar_id,
                    }
                )
                kept_for_calendar += 1
            print("Fetched:", len(data.get("items", [])), "Kept:", kept_for_calendar, "Skipped all-day:", skipped_all_day, "Skipped window:", skipped_window, "Skipped parse:", skipped_parse)
        except Exception as e:
            print("[ERROR] Fetch failed for", calendar_id, str(e))

    merged_events.sort(key=lambda event: seen.get(event.get("start", {}).get("dateTime", "") + "|" + event.get("summary", ""), 0))
    # Print a short summary of the events that will be displayed
    if merged_events:
        print("Events displayed:")
        for ev in merged_events[:max_results]:
            dt = ev.get("start", {}).get("dateTime", "")
            summary = ev.get("summary", "")
            if len(dt) >= 16:
                hhmm = dt[11:16]
            else:
                hhmm = "--:--"
            print("{} {}".format(hhmm, summary[:24]))
    else:
        print("No events displayed.")
    return merged_events[:max_results]


def format_event_compact(event):
    max_chars = 19
    summary = event.get("summary", "(No title)")
    words = summary.split()
    if words:
        short_summary = ""
        for word in words:
            candidate = word if not short_summary else (short_summary + " " + word)
            if len(candidate) <= max_chars:
                short_summary = candidate
            else:
                break
        # Keep whole words only; if first word is longer than cap, keep it as-is.
        if not short_summary:
            short_summary = words[0]
    else:
        short_summary = "(No title)"
    start_dt = event.get("start", {}).get("dateTime", "")
    if len(start_dt) >= 16:
        hh = int(start_dt[11:13])
        mm = start_dt[14:16]
        hhmm = "%d:%s" % (hh, mm)
    else:
        hhmm = "--:--"
    return hhmm, short_summary


def _compact_words(text, max_chars):
    words = text.split()
    if not words:
        return ""
    compact = ""
    for word in words:
        candidate = word if not compact else (compact + " " + word)
        if len(candidate) <= max_chars:
            compact = candidate
        else:
            break
    return compact if compact else words[0]


def _normalize_rfc3339_for_parser(dt_str):
    # rfc3339_to_epoch expects second precision, e.g. ...:SSZ or ...:SS+HH:MM
    if not dt_str:
        return dt_str
    if "." not in dt_str:
        return dt_str
    if dt_str.endswith("Z"):
        return dt_str.split(".")[0] + "Z"
    plus_idx = dt_str.find("+", 19)
    minus_idx = dt_str.find("-", 19)
    tz_idx = plus_idx if plus_idx != -1 else minus_idx
    if tz_idx == -1:
        return dt_str.split(".")[0]
    return dt_str[:19] + dt_str[tz_idx:]


def log_main_screen_events(events):
    shown = min(len(events), EVENT_ROWS)
    print("Events in 24h:", len(events), "shown on screen:", shown, "row cap:", EVENT_ROWS)
    if shown == 0:
        print("- none")
        return
    for i in range(shown):
        hhmm, short_summary = format_event_compact(events[i])
        print("-", hhmm, short_summary)


def utc_epoch_to_local_hhmm(utc_epoch, offset_seconds):
    tm = time.localtime(int(utc_epoch + offset_seconds))
    return tm.tm_hour, tm.tm_min


def local_date_hhmm_to_utc_epoch(date_str, hour, minute, offset_seconds):
    y = int(date_str[0:4])
    mo = int(date_str[5:7])
    d = int(date_str[8:10])
    local_epoch = time.mktime((y, mo, d, hour, minute, 0, 0, -1, -1))
    return int(local_epoch - offset_seconds)


def is_local_date_monday(date_str):
    y = int(date_str[0:4])
    mo = int(date_str[5:7])
    d = int(date_str[8:10])
    # Use noon to avoid DST boundary edge cases around midnight.
    local_epoch = time.mktime((y, mo, d, 12, 0, 0, 0, -1, -1))
    return time.localtime(local_epoch).tm_wday == 0


def _local_date_to_noon_epoch(date_str):
    y = int(date_str[0:4])
    mo = int(date_str[5:7])
    d = int(date_str[8:10])
    return time.mktime((y, mo, d, 12, 0, 0, 0, -1, -1))


def _local_epoch_to_date_str(local_epoch):
    tm = time.localtime(local_epoch)
    return "%04d-%02d-%02d" % (tm.tm_year, tm.tm_mon, tm.tm_mday)


def _add_days_local_date(date_str, days):
    return _local_epoch_to_date_str(_local_date_to_noon_epoch(date_str) + (days * 86400))

def _is_pappavecka_extra_alarm_date(date_str):
    if not (pappavecka_active and pappavecka_date):
        return False
    # Extra dad-week wake alarms are only on weekdays.
    try:
        date_wday = time.localtime(_local_date_to_noon_epoch(date_str)).tm_wday
    except Exception as e:
        return False
    if date_wday >= 5:
        return False
    # Dad-week extra wake alarm is Tue-Fri only.
    extra_dates = (
        _add_days_local_date(pappavecka_date, 0),  # include pappavecka_date itself
        _add_days_local_date(pappavecka_date, 1),
        _add_days_local_date(pappavecka_date, 2),
        _add_days_local_date(pappavecka_date, 3),
        _add_days_local_date(pappavecka_date, 4),
    )
    result = date_str in extra_dates
    return result


MORNING_LIMIT_HOUR = 10  # Weekday first-event alarms before this hour.
WEEKEND_MORNING_LIMIT_HOUR = 11  # Weekend first-event alarms before this hour.
WEEKDAY_EVENT_ALARM_LEAD_MINUTES = 15
WEEKEND_EVENT_ALARM_LEAD_MINUTES = 30


def schedule_alarm_from_events(events, offset_seconds, now_utc_epoch=None):
    try:
        has_monday_mammavecka = mammavecka_active and mammavecka_date and is_local_date_monday(mammavecka_date)
        candidates = []

        # Monday Mammavecka exception: still alarm at 07:20.
        if has_monday_mammavecka:
            mammavecka_alarm_utc_epoch = local_date_hhmm_to_utc_epoch(mammavecka_date, 7, 20, offset_seconds)
            if now_utc_epoch is None:
                candidates.append(
                    (
                        mammavecka_alarm_utc_epoch,
                        mammavecka_alarm_utc_epoch + 3600,
                        "ALARM 07:20",
                        "mammavecka|" + mammavecka_date,
                    )
                )
            else:
                now_local_epoch = now_utc_epoch + offset_seconds
                today_str = _local_epoch_to_date_str(now_local_epoch)
                tomorrow_str = _local_epoch_to_date_str(now_local_epoch + 86400)
                if mammavecka_date in (today_str, tomorrow_str) and ((mammavecka_alarm_utc_epoch + 3600) > now_utc_epoch):
                    candidates.append(
                        (
                            mammavecka_alarm_utc_epoch,
                            mammavecka_alarm_utc_epoch + 3600,
                            "ALARM 07:20",
                            "mammavecka|" + mammavecka_date,
                        )
                    )

        if events:
            now_local_date = None
            tomorrow_local_date = None
            if now_utc_epoch is not None:
                now_local_epoch = now_utc_epoch + offset_seconds
                now_local_date = _local_epoch_to_date_str(now_local_epoch)
                tomorrow_local_date = _local_epoch_to_date_str(now_local_epoch + 86400)

            # Build first morning event per day; ignore later daytime events.
            first_morning_event_by_date = {}
            for event in events:
                start_dt = event.get("start", {}).get("dateTime", "")
                if not start_dt:
                    continue
                try:
                    event_utc_epoch = rfc3339_to_epoch(start_dt)
                except Exception:
                    continue

                local_tm = time.localtime(int(event_utc_epoch + offset_seconds))
                local_date = "%04d-%02d-%02d" % (local_tm.tm_year, local_tm.tm_mon, local_tm.tm_mday)

                # Only arm event alarms for today/tomorrow.
                if now_local_date is not None and local_date not in (now_local_date, tomorrow_local_date):
                    continue

                limit_hour = WEEKEND_MORNING_LIMIT_HOUR if local_tm.tm_wday >= 5 else MORNING_LIMIT_HOUR
                if local_tm.tm_hour >= limit_hour:
                    continue

                current = first_morning_event_by_date.get(local_date)
                if (current is None) or (event_utc_epoch < current[0]):
                    first_morning_event_by_date[local_date] = (event_utc_epoch, start_dt, event)

            for _local_date, (first_event_epoch, first_start_dt, first_event) in first_morning_event_by_date.items():
                if (now_utc_epoch is not None) and (first_event_epoch <= now_utc_epoch):
                    continue

                lead_minutes = WEEKEND_EVENT_ALARM_LEAD_MINUTES if time.localtime(int(first_event_epoch + offset_seconds)).tm_wday >= 5 else WEEKDAY_EVENT_ALARM_LEAD_MINUTES
                event_alarm_utc_epoch = first_event_epoch - (lead_minutes * 60)
                event_key = first_start_dt + "|" + first_event.get("summary", "")
                ah, am = utc_epoch_to_local_hhmm(event_alarm_utc_epoch, offset_seconds)
                candidates.append(
                    (
                        event_alarm_utc_epoch,
                        first_event_epoch,
                        "ALARM %02d:%02d" % (ah, am),
                        event_key,
                    )
                )

        # Dad-week extra 07:20 alarm is additional, not a replacement.
        if pappavecka_active and pappavecka_date:
            if now_utc_epoch is None:
                if _is_pappavecka_extra_alarm_date(pappavecka_date):
                    pappavecka_alarm_utc_epoch = local_date_hhmm_to_utc_epoch(pappavecka_date, 7, 20, offset_seconds)
                    candidates.append(
                        (
                            pappavecka_alarm_utc_epoch,
                            pappavecka_alarm_utc_epoch + 3600,
                            "ALARM 07:20",
                            "pappavecka|" + pappavecka_date,
                        )
                    )
            else:
                now_local_epoch = now_utc_epoch + offset_seconds
                today_str = _local_epoch_to_date_str(now_local_epoch)
                tomorrow_str = _local_epoch_to_date_str(now_local_epoch + 86400)
                for extra_date in (today_str, tomorrow_str):
                    if not _is_pappavecka_extra_alarm_date(extra_date):
                        print("[DEBUG]     Not extra alarm date:", extra_date)
                        continue
                    pappavecka_alarm_utc_epoch = local_date_hhmm_to_utc_epoch(extra_date, 7, 20, offset_seconds)
                    print("[DEBUG]     Considering alarm at:", pappavecka_alarm_utc_epoch, "for date:", extra_date)
                    if (pappavecka_alarm_utc_epoch + 3600) > now_utc_epoch:
                        print("[DEBUG]     Adding pappavecka 07:20 alarm for:", extra_date)
                        candidates.append(
                            (
                                pappavecka_alarm_utc_epoch,
                                pappavecka_alarm_utc_epoch + 3600,
                                "ALARM 07:20",
                                "pappavecka|" + extra_date,
                            )
                        )
                    else:
                        print("[DEBUG]     Skipping alarm, already passed for:", extra_date)
        print("candidates:", candidates)
        if not candidates:
            print("[DEBUG] schedule_alarm_from_events: no alarm candidates found, returning early")
            return None, None, "", None

        candidates.sort(key=lambda c: c[0])
        selected = None
        if now_utc_epoch is not None:
            print("[DEBUG] schedule_alarm_from_events: filtering candidates for future alarms after:", now_utc_epoch)
            future_candidates = [cand for cand in candidates if cand[0] > now_utc_epoch]
            if future_candidates:
                selected = future_candidates[0]
        if selected is None:
            return None, None, "", None
        alarm_utc_epoch, alarm_event_utc_epoch, alarm_text, alarm_event_key = selected
        local_alarm_epoch = alarm_utc_epoch + offset_seconds
        local_tm = time.localtime(int(local_alarm_epoch))
        alarm_time_str = "%02d:%02d" % (local_tm.tm_hour, local_tm.tm_min)
        alarm_text = f"ALARM {alarm_time_str}"
        print(f"[DEBUG] alarm_text set to: {alarm_text}")
        return alarm_utc_epoch, alarm_event_utc_epoch, alarm_text, alarm_event_key
    except Exception as e:
        print("[EXCEPTION] schedule_alarm_from_events crashed:", repr(e))
        import sys
        sys.print_exception(e)
        return None, None, "", None

def log_alarm_choice(alarm_utc_epoch, alarm_event_utc_epoch, alarm_text, alarm_event_key, offset_seconds, now_utc_epoch):
    _ = alarm_utc_epoch
    _ = alarm_event_utc_epoch
    _ = alarm_text
    _ = alarm_event_key
    _ = offset_seconds
    _ = now_utc_epoch


def _open_speaker_audio_out():

    # First try DAC path (AudioOut), which matches the working speech path on PyPortal.
    for pin_name in ("A0", "SPEAKER"):
        if not hasattr(board, pin_name):
            continue
        pin = getattr(board, pin_name)
        try:
            audio = audioio.AudioOut(pin)
            return audio
        except Exception:
            pass

    # Fall back to PWM audio where available.
    if audiopwmio is None:
        return None

    candidate_names = ["SPEAKER", "A0"]
    for pin_name in candidate_names:
        if not hasattr(board, pin_name):
            continue
        pin = getattr(board, pin_name)
        try:
            audio = audiopwmio.PWMAudioOut(pin)
            return audio
        except Exception:
            pass
    return None

def run_alarm_until_touch(max_duration_seconds=30):
    # Gentle Bach-style wake phrase. Touch stops alarm; hard stop after max_duration_seconds.
    
    audio = None
    synth = None
    current_note = None
    try:
        audio = _open_speaker_audio_out()
        if audio is None:
            return
        env = synthio.Envelope(
            attack_time=0.01,
            decay_time=0.10,
            sustain_level=0.65,
            release_time=0.16,
        )
        # Richer but still soft timbre: fundamental + 2nd/3rd harmonics.
        warm_wave = array.array("h", [0] * 256)
        for i in range(256):
            phase = (2.0 * math.pi * i) / 256.0
            sample = (
                math.sin(phase)
                + 0.43 * math.sin(2.0 * phase)
                + 0.16 * math.sin(3.0 * phase)
            )
            warm_wave[i] = int(sample * 21000)

        synth = synthio.Synthesizer(sample_rate=22050, envelope=env, waveform=warm_wave)
        audio.play(synth)

        # Air timing tuned from the test script; opening note intentionally shortened.
        beat_seconds = 60.0 / 60.0  # Slower tempo (was 72 BPM, now 60 BPM)
        vibrato_rate_hz = 5.2
        vibrato_depth_cents = 10.0
        phrase = (
            (78, 3.0, 0.11),
            (78, 0.75, 0.10),
            (83, 0.4, 0.09),
            (79, 0.4, 0.09),
            (76, 0.4, 0.09),
            (74, 0.4, 0.09),
            (73, 0.4, 0.09),
            (74, 0.4, 0.09),
            (73, 1.6, 0.09),
            (69, 1.6, 0.08),
            (81, 2.4, 0.10),
            (81, 0.4, 0.09),
            (78, 0.4, 0.09),
            (72, 0.4, 0.08),
            (71, 0.4, 0.08),
            (76, 0.4, 0.09),
            (75, 0.4, 0.08),
            (81, 0.4, 0.09),
            (79, 0.4, 0.09),
            (79, 2.4, 0.09),
            (78, 1.0, 0.09),
            (79, 0.25, 0.08),
            (81, 0.25, 0.08),
            (74, 0.4, 0.07),
            (74, 0.4, 0.07),
            (76, 0.4, 0.07),
            (78, 0.4, 0.08),
            (76, 0.4, 0.08),
            (76, 0.4, 0.08),
            (74, 0.4, 0.08),
            (73, 0.4, 0.08),
            (71, 0.4, 0.08),
            (69, 0.4, 0.08),
            (81, 0.4, 0.09),
            (78, 0.4, 0.08),
            (72, 0.4, 0.07),
            (71, 0.4, 0.07),
            (76, 0.4, 0.08),
            (75, 0.4, 0.07),
            (81, 0.4, 0.08),
            (79, 0.4, 0.08),
            (79, 2.0, 0.09),
            (73, 0.4, 0.08),
            (71, 0.4, 0.08),
            (71, 0.25, 0.08),
            (73, 0.25, 0.08),
            (74, 0.25, 0.08),
            (73, 0.6, 0.08),
            (71, 0.4, 0.08),
            (69, 2.0, 0.08),
        )

        note_poll_sleep = 0.01
        inter_note_gap = 0.006

        start_t = time.monotonic()
        while True:
            if ts.touch_point:
                break
            if (time.monotonic() - start_t) >= max_duration_seconds:
                break

            for midi_note, beats, amp in phrase:
                if ts.touch_point:
                    break
                if (time.monotonic() - start_t) >= max_duration_seconds:
                    break

                note = synthio.Note(frequency=synthio.midi_to_hz(midi_note), amplitude=1.0)
                current_note = (note,)
                synth.press(current_note)

                note_end = time.monotonic() + (beat_seconds * beats)
                note_start = time.monotonic()
                base_freq = synthio.midi_to_hz(midi_note)
                while time.monotonic() < note_end:
                    if ts.touch_point:
                        break
                    if (time.monotonic() - start_t) >= max_duration_seconds:
                        break
                    lfo_t = time.monotonic() - note_start
                    cents = math.sin(2.0 * math.pi * vibrato_rate_hz * lfo_t) * vibrato_depth_cents
                    note.frequency = base_freq * (2.0 ** (cents / 1200.0))
                    time.sleep(note_poll_sleep)

                synth.release(current_note)

                pause_end = time.monotonic() + inter_note_gap
                while time.monotonic() < pause_end:
                    if ts.touch_point:
                        break
                    if (time.monotonic() - start_t) >= max_duration_seconds:
                        break
                    time.sleep(note_poll_sleep)

                if ts.touch_point:
                    break
                if (time.monotonic() - start_t) >= max_duration_seconds:
                    break

        if current_note is not None:
            try:
                synth.release(current_note)
            except Exception:
                pass
        audio.stop()
    except Exception as e:
        log_exception("run_alarm_until_touch", e)
    finally:
        if audio:
            audio.deinit()

# --- Hardware UI/audio setup ---
ts = adafruit_touchscreen.Touchscreen(
    board.TOUCH_XL,
    board.TOUCH_XR,
    board.TOUCH_YD,
    board.TOUCH_YU,
    calibration=((5200, 59000), (5800, 57000)),
    size=(320, 240),
)

try:
    light_sensor = analogio.AnalogIn(board.LIGHT)
except Exception:
    light_sensor = None

speaker_enable = digitalio.DigitalInOut(board.SPEAKER_ENABLE)
speaker_enable.direction = digitalio.Direction.OUTPUT
speaker_enable.value = True
#run_pwm_alarm_self_test()
# Boot preview: play the current wake melody briefly so it can be auditioned.
#run_alarm_until_touch(max_duration_seconds=34)

board.DISPLAY.auto_refresh = True

FONT_IDLE_PATH = "/fonts/Nasalization-Regular-40.bdf"
FONT_MAIN_PATH = "/fonts/Nasalization-Regular-20.bdf"
FONT_WEATHER_PATH = "/fonts/DuBellay-20.bdf"

try:
    idle_font = bitmap_font.load_font(FONT_IDLE_PATH)
    main_font = bitmap_font.load_font(FONT_MAIN_PATH)
    weather_font = bitmap_font.load_font(FONT_WEATHER_PATH)
except Exception:
    idle_font = terminalio.FONT
    main_font = terminalio.FONT
    weather_font = terminalio.FONT

# Main-screen clock must use a font that has ':' glyph.
clock_font = terminalio.FONT

# Idle (bouncing) screen group
idle_group = displayio.Group()
idle_time_label = label.Label(idle_font, text="--:--", color=0xFFFFFF)
idle_date_label = label.Label(main_font, text="-- ---", color=0xC0C0C0)
idle_alarm_label = label.Label(main_font, text="ALARM --:--", color=0xFFD27F)
idle_weather_line1 = label.Label(weather_font, text="Hi: --(--) Lo: --(--)", color=0xBFE8FF)
idle_weather_line2 = label.Label(weather_font, text="Weather", color=0xBFE8FF)
idle_weather_line3 = label.Label(weather_font, text="Sun: --:-- - --:--", color=0xBFE8FF)
idle_weather_line4 = label.Label(weather_font, text="", color=0xBFE8FF)
WEATHER_Y_EXTRA = 0
PRECIP_ROW_Y_ADJUST = -10
SUN_ROW_Y_ADJUST = 5
ALARM_LABEL_Y_ADJUST = -5

# Startup placement: center placeholder idle labels before first network sync.
startup_alarm_w = idle_alarm_label.bounding_box[2] * idle_alarm_label.scale
startup_time_w = idle_time_label.bounding_box[2] * idle_time_label.scale
startup_time_h = idle_time_label.bounding_box[3] * idle_time_label.scale
startup_date_w = idle_date_label.bounding_box[2] * idle_date_label.scale
startup_date_h = idle_date_label.bounding_box[3] * idle_date_label.scale
startup_row1_h = max(startup_time_h, startup_date_h)
startup_row1_w = startup_time_w + 10 + startup_date_w
startup_alarm_h = idle_alarm_label.bounding_box[3] * idle_alarm_label.scale
startup_block_w = max(
    startup_row1_w,
    startup_alarm_w,
    idle_weather_line1.bounding_box[2] * idle_weather_line1.scale,
    idle_weather_line2.bounding_box[2] * idle_weather_line2.scale,
    idle_weather_line3.bounding_box[2] * idle_weather_line3.scale,
    idle_weather_line4.bounding_box[2] * idle_weather_line4.scale,
)
startup_block_h = startup_row1_h
startup_block_h += 6 + startup_alarm_h
startup_block_h += WEATHER_Y_EXTRA
startup_block_h += 6 + idle_weather_line1.bounding_box[3] * idle_weather_line1.scale
startup_block_h += 4 + idle_weather_line2.bounding_box[3] * idle_weather_line2.scale
startup_block_h += 4 + idle_weather_line3.bounding_box[3] * idle_weather_line3.scale
if idle_weather_line4.text:
    startup_block_h += 4 + idle_weather_line4.bounding_box[3] * idle_weather_line4.scale
startup_x = max(0, (board.DISPLAY.width - startup_block_w) // 2)
startup_y = max(0, (board.DISPLAY.height - startup_block_h) // 2)

idle_time_label.x = startup_x
idle_time_label.y = startup_y
idle_date_label.x = startup_x + startup_time_w + 10
idle_date_label.y = startup_y + (startup_row1_h - startup_date_h) // 2
idle_alarm_label.x = startup_x
idle_alarm_label.y = startup_y + startup_row1_h + 6 + ALARM_LABEL_Y_ADJUST
startup_next_y = startup_y + startup_row1_h + 6 + startup_alarm_h + 6
idle_weather_line1.x = startup_x
idle_weather_line1.y = startup_next_y + WEATHER_Y_EXTRA
idle_weather_line2.x = startup_x + 1  # Move 2px left
idle_weather_line2.y = idle_weather_line1.y  + idle_weather_line1.bounding_box[3] * idle_weather_line1.scale
idle_weather_line3.x = startup_x
idle_weather_line3.y = idle_weather_line2.y + idle_weather_line2.bounding_box[3] * idle_weather_line2.scale + 4 + SUN_ROW_Y_ADJUST
idle_weather_line4.x = startup_x
idle_weather_line4.y = idle_weather_line3.y + idle_weather_line3.bounding_box[3] * idle_weather_line3.scale + 7  # Move 3px down

idle_group.append(idle_time_label)
idle_group.append(idle_date_label)
idle_group.append(idle_alarm_label)
idle_group.append(idle_weather_line1)
idle_group.append(idle_weather_line2)
idle_group.append(idle_weather_line3)
idle_group.append(idle_weather_line4)

# Events screen group
events_group = displayio.Group()
events_header = label.Label(main_font, text="Events", color=0x80FFFF)
events_header.x = 6
events_header.y = 8
events_group.append(events_header)

EVENT_TIME_X = 6
EVENT_TITLE_X = 66
EVENT_TIME_RIGHT_X = EVENT_TITLE_X - 3
EVENT_ROWS = 7
EVENT_ROW_START_Y = 29
EVENT_ROW_GAP = 20

event_time_labels = []
event_title_labels = []
for i in range(EVENT_ROWS):
    row_y = EVENT_ROW_START_Y + i * EVENT_ROW_GAP

    time_lbl = label.Label(main_font, text="", color=0x80FFFF)
    time_lbl.x = EVENT_TIME_RIGHT_X
    time_lbl.y = row_y
    events_group.append(time_lbl)
    event_time_labels.append(time_lbl)

    title_lbl = label.Label(main_font, text="", color=0xFFFFFF)
    title_lbl.x = EVENT_TITLE_X
    title_lbl.y = row_y
    events_group.append(title_lbl)
    event_title_labels.append(title_lbl)

events_time_label = label.Label(idle_font, text="--:--", color=0xFFFFFF)
events_date_label = label.Label(main_font, text="-- ---", color=0xC0C0C0)
events_alarm_label = label.Label(main_font, text="ALARM --:--", color=0xFFD27F)
events_group.append(events_time_label)
events_group.append(events_date_label)
events_group.append(events_alarm_label)


PAPPA_BUTTON_W = 148
PAPPA_BUTTON_H = 75
PAPPA_BUTTON_MARGIN = 10
PAPPA_BUTTON_Y_OFFSET = 6

button_x = board.DISPLAY.width - PAPPA_BUTTON_W - PAPPA_BUTTON_MARGIN
button_y = board.DISPLAY.height - PAPPA_BUTTON_H - PAPPA_BUTTON_MARGIN + PAPPA_BUTTON_Y_OFFSET
pappa_button = adafruit_button.Button(
    x=button_x,
    y=button_y,
    width=PAPPA_BUTTON_W,
    height=PAPPA_BUTTON_H,
    style=adafruit_button.Button.ROUNDRECT,
    label="",  # No label, we'll overlay our own
    fill_color=0xFF8C00,
    outline_color=0x000000,
)
# Overlay two custom labels
pappa_label_top = label.Label(idle_font, text="PAPPA", color=0x000000)
pappa_label_bottom = label.Label(idle_font, text="VECKA", color=0x000000)
pappa_label_top.anchor_point = (0.5, 0.5)
pappa_label_bottom.anchor_point = (0.5, 0.5)
pappa_label_top.anchored_position = (button_x + PAPPA_BUTTON_W // 2, button_y + int(PAPPA_BUTTON_H * 0.3))
pappa_label_bottom.anchored_position = (button_x + PAPPA_BUTTON_W // 2, button_y + int(PAPPA_BUTTON_H * 0.70))

board.DISPLAY.root_group = idle_group
try:
    # Start dim immediately at boot so the panel does not flash full brightness.
    board.DISPLAY.brightness = 0.18
except Exception:
    pass

IDLE_MIN_X = 10
IDLE_MIN_Y = 20
x, y = startup_x, startup_y
vx, vy = 3, 3
max_x = max(IDLE_MIN_X, board.DISPLAY.width - startup_block_w)
max_y = max(IDLE_MIN_Y, board.DISPLAY.height - startup_block_h)
pappavecka_active = False
pappavecka_date = None
mammavecka_active = False
mammavecka_date = None
weather_line1 = "Hi: --(--) Lo: --(--)"
weather_line2 = "Weather"
weather_line3 = "Sun: --:-- - --:--"
weather_line4 = ""
weather_current = "--(--)"


def right_align(lbl, margin=6):
    lbl.x = board.DISPLAY.width - (lbl.bounding_box[2] * lbl.scale) - margin


def update_idle_labels(hour, minute, day, month_short):
    global x, y, max_x, max_y
    idle_time_label.text = "%02d:%02d" % (hour, minute)
    idle_date_label.text = "%02d %s %s" % (day, month_short, weather_current)
    # Always show the next scheduled alarm if available
    idle_alarm_label.text = alarm_text if alarm_text else "ALARM --:--"
    idle_weather_line1.text = weather_line1
    idle_weather_line2.text = weather_line2
    # If precipitation start exists, keep it on row 3 and move sun to the last row.
    if weather_line4:
        idle_weather_line3.text = weather_line4
        idle_weather_line4.text = weather_line3
    else:
        idle_weather_line3.text = weather_line3
        idle_weather_line4.text = ""

    time_w = idle_time_label.bounding_box[2] * idle_time_label.scale
    time_h = idle_time_label.bounding_box[3] * idle_time_label.scale
    date_w = idle_date_label.bounding_box[2] * idle_date_label.scale
    date_h = idle_date_label.bounding_box[3] * idle_date_label.scale
    row1_h = max(time_h, date_h)
    row1_w = time_w + 10 + date_w
    alarm_w = (idle_alarm_label.bounding_box[2] * idle_alarm_label.scale) if alarm_text else 0
    alarm_h = (idle_alarm_label.bounding_box[3] * idle_alarm_label.scale) if alarm_text else 0
    block_w = max(
        row1_w,
        alarm_w,
        idle_weather_line1.bounding_box[2] * idle_weather_line1.scale,
        idle_weather_line2.bounding_box[2] * idle_weather_line2.scale,
        idle_weather_line3.bounding_box[2] * idle_weather_line3.scale,
        (idle_weather_line4.bounding_box[2] * idle_weather_line4.scale) if weather_line4 else 0,
    )
    block_h = row1_h
    if alarm_text:
        block_h += 6 + alarm_h
    block_h += WEATHER_Y_EXTRA
    block_h += 6 + idle_weather_line1.bounding_box[3] * idle_weather_line1.scale
    block_h += 4 + idle_weather_line2.bounding_box[3] * idle_weather_line2.scale
    block_h += (4 + (PRECIP_ROW_Y_ADJUST if idle_weather_line4.text else 0)) + idle_weather_line3.bounding_box[3] * idle_weather_line3.scale
    if idle_weather_line4.text:
        block_h += 4 + idle_weather_line4.bounding_box[3] * idle_weather_line4.scale
    block_h += SUN_ROW_Y_ADJUST
    max_x = max(IDLE_MIN_X, board.DISPLAY.width - block_w)
    max_y = max(IDLE_MIN_Y, board.DISPLAY.height - block_h)
    x = min(max(x, IDLE_MIN_X), max_x)
    y = min(max(y, IDLE_MIN_Y), max_y)
    set_idle_position(x, y)


def set_idle_position(px, py):
    time_w = idle_time_label.bounding_box[2] * idle_time_label.scale
    time_h = idle_time_label.bounding_box[3] * idle_time_label.scale
    date_h = idle_date_label.bounding_box[3] * idle_date_label.scale
    row1_h = max(time_h, date_h)
    idle_time_label.x = int(px)
    idle_time_label.y = int(py)
    idle_date_label.x = int(px + time_w + 10)
    idle_date_label.y = int(py + (row1_h - date_h) // 2)
    next_y = int(py + row1_h + 6)
    if alarm_text:
        idle_alarm_label.x = int(px)
        idle_alarm_label.y = int(next_y + ALARM_LABEL_Y_ADJUST)
        next_y = int(next_y + idle_alarm_label.bounding_box[3] * idle_alarm_label.scale + 6)
    else:
        idle_alarm_label.x = int(px)
        idle_alarm_label.y = int(next_y + ALARM_LABEL_Y_ADJUST)
    idle_weather_line1.x = int(px)
    idle_weather_line1.y = int(next_y + WEATHER_Y_EXTRA)
    idle_weather_line2.x = int(px) + 1  # Align with startup, move 2px left
    idle_weather_line2.y = int(idle_weather_line1.y + 25)
    idle_weather_line3.x = int(px)
    line3_gap = 8 + (PRECIP_ROW_Y_ADJUST if weather_line4 else 0)
    idle_weather_line3.y = int(idle_weather_line2.y + idle_weather_line2.bounding_box[3] * idle_weather_line2.scale + line3_gap + (0 if weather_line4 else SUN_ROW_Y_ADJUST))
    idle_weather_line4.x = int(px)
    idle_weather_line4.y = int(idle_weather_line3.y + idle_weather_line3.bounding_box[3] * idle_weather_line3.scale + 7 + (SUN_ROW_Y_ADJUST if weather_line4 else 0))  # Move 3px down


def bounce_idle_labels():
    global x, y, vx, vy
    if max_x <= IDLE_MIN_X or max_y <= IDLE_MIN_Y:
        x = IDLE_MIN_X
        y = IDLE_MIN_Y
        set_idle_position(x, y)
        return
    x += vx
    y += vy
    if x < IDLE_MIN_X or x > max_x:
        vx = -vx
        x += vx
    if y < IDLE_MIN_Y or y > max_y:
        vy = -vy
        y += vy
    x = min(max(x, IDLE_MIN_X), max_x)
    y = min(max(y, IDLE_MIN_Y), max_y)
    set_idle_position(x, y)


def update_events_panel(events, hour, minute, day, month_short):
    events_header.text = "Events"
    events_time_label.text = "%02d:%02d" % (hour, minute)
    events_date_label.text = "%02d %s" % (day, month_short)
    # Always show the next scheduled alarm if available
    events_alarm_label.text = alarm_text if alarm_text else "ALARM --:--"
    left_margin = 11
    bottom_margin = 8
    row_gap = 6

    # Keep one extra row reserved below date for alarm row when active.
    time_h = events_time_label.bounding_box[3] * events_time_label.scale
    date_h = events_date_label.bounding_box[3] * events_date_label.scale
    reserved_h = date_h if alarm_text else 0

    block_top = board.DISPLAY.height - (time_h + row_gap + date_h + row_gap + reserved_h) - bottom_margin
    events_time_label.x = left_margin-2
    events_time_label.y = int(block_top + 7)
    events_date_label.x = left_margin
    events_date_label.y = int(block_top + time_h + row_gap + 1)
    if alarm_text:
        events_alarm_label.x = left_margin
        events_alarm_label.y = int(events_date_label.y + date_h + row_gap)

    # Toggle pappa_button visibility by add/remove from events_group
    try:
        if pappavecka_active:
            if pappa_button not in events_group:
                events_group.append(pappa_button)
            if pappa_label_top not in events_group:
                events_group.append(pappa_label_top)
            if pappa_label_bottom not in events_group:
                events_group.append(pappa_label_bottom)
        else:
            if pappa_button in events_group:
                events_group.remove(pappa_button)
            if pappa_label_top in events_group:
                events_group.remove(pappa_label_top)
            if pappa_label_bottom in events_group:
                events_group.remove(pappa_label_bottom)
    except Exception as e:
        pass

    for i in range(EVENT_ROWS):
        if i < len(events):
            hhmm, short_summary = format_event_compact(events[i])
            row_color = get_calendar_color(events[i].get("_calendar_id"))
            event_time_labels[i].text = hhmm
            time_w = event_time_labels[i].bounding_box[2] * event_time_labels[i].scale
            event_time_labels[i].x = EVENT_TIME_RIGHT_X - time_w
            event_title_labels[i].text = short_summary
            event_time_labels[i].color = row_color
            event_title_labels[i].color = row_color
        else:
            event_time_labels[i].text = ""
            event_time_labels[i].x = EVENT_TIME_RIGHT_X
            event_title_labels[i].text = ""


def play_wav(filename):
    try:
        with open(filename, "rb") as f:
            wav = audiocore.WaveFile(f)
            with audioio.AudioOut(board.A0) as audio:
                audio.play(wav)
                while audio.playing:
                    pass
    except Exception:
        pass


def say_time(hour, minute):
    play_wav("/sounds/%02d.wav" % hour)
    if minute == 0:
        play_wav("/sounds/oclock.wav")
    elif minute < 10:
        play_wav("/sounds/o.wav")
        play_wav("/sounds/%d.wav" % minute)
    else:
        play_wav("/sounds/%02d.wav" % minute)


# --- Startup state (lightweight; first loop sync performs full fetch) ---
events = []
last_good_events = []
weather_line1, weather_line2, weather_line3, weather_line4, weather_current = (
    "Hi: --(--) Lo: --(--)",
    "Weather",
    "Sun: --:-- - --:--",
    "",
    "--(--)",
)

current_utc_offset_seconds = _parse_utc_offset_seconds("+00:00")
alarm_utc_epoch, alarm_event_utc_epoch, alarm_text, alarm_event_key = (None, None, "", None)
alarm_fired_for_key = None

def prime_events_view(hour, minute, day, month_short):
    # Pre-populate labels so first touch does not pay all text layout/render cost.
    update_events_panel(events, hour, minute, day, month_short)


def sync_time_and_forecast():
    local_epoch, offset_seconds = sync_stockholm_epoch()
    line1, line2, line3, line4, current_compact = fetch_malmo_weather_lines()
    return local_epoch, offset_seconds, line1, line2, line3, line4, current_compact


IDLE_BRIGHTNESS_MAX = 1.00
IDLE_BRIGHTNESS_SENSOR_FALLBACK = 0.18
ACTIVE_BRIGHTNESS = 1.00
AMBIENT_DARK_RAW = 2300
AMBIENT_BRIGHT_RAW = 5000
AMBIENT_IDLE_MIN_BRIGHTNESS = 0.03
current_brightness = None


def set_display_brightness(level):
    global current_brightness
    if current_brightness is not None and abs(current_brightness - level) < 0.001:
        return
    try:
        board.DISPLAY.brightness = level
        current_brightness = level
    except Exception:
        pass


def read_ambient_light_scale():
    if light_sensor is None:
        return None
    try:
        raw = light_sensor.value
    except Exception:
        return None

    if raw <= AMBIENT_DARK_RAW:
        target_scale = 0.0
    elif raw >= AMBIENT_BRIGHT_RAW:
        target_scale = 1.0
    else:
        target_scale = (raw - AMBIENT_DARK_RAW) / float(AMBIENT_BRIGHT_RAW - AMBIENT_DARK_RAW)

    return target_scale


def adaptive_idle_brightness(hour):
    _ = hour
    base_level = IDLE_BRIGHTNESS_MAX
    ambient_scale_now = read_ambient_light_scale()
    if ambient_scale_now is None:
        return IDLE_BRIGHTNESS_SENSOR_FALLBACK
    scaled_level = base_level * ambient_scale_now
    if scaled_level < AMBIENT_IDLE_MIN_BRIGHTNESS:
        return AMBIENT_IDLE_MIN_BRIGHTNESS
    if scaled_level > base_level:
        return base_level
    return scaled_level


# --- Main loop ---
TIME_SYNC_INTERVAL = 15 * 60  # 15 min: keep events/alarms fresh during the day.
CALENDAR_SYNC_INTERVAL = 15 * 60
last_time_sync = 0
last_calendar_sync = 0
clock_base_monotonic = None
clock_base_local_epoch = None
current_hour = None
current_minute = None
current_day = None
current_month_short = None
current_second = None
# Start with warmup disabled; warmup can trigger repeated allocation failures on tight heaps.
events_mode_until = -1
sync_reposition_pending = False
last_idle_hour = None
last_idle_minute = None
last_idle_day = None
last_idle_month_short = None
last_idle_weather_current = None
last_idle_weather_line1 = None
last_idle_weather_line2 = None
last_idle_weather_line3 = None
last_idle_weather_line4 = None
last_idle_alarm_text = None
last_events_update_second = None
last_events_update_minute = None
force_events_panel_refresh = True
active_root_group = idle_group
last_perf_log = 0.0
last_loop_now = None
loop_count_since_log = 0
loop_dt_max_ms = 0
last_gc_collect = 0.0
last_idle_bounce_update = 0.0
last_idle_brightness_update = 0.0
cached_idle_brightness = IDLE_BRIGHTNESS_SENSOR_FALLBACK
last_touch_speak_at = -9999.0
TOUCH_SPEAK_COOLDOWN_SECONDS = 2.5
ENABLE_MAIN_LOOP_TOUCH = True


def set_root_group(target_group):
    global active_root_group
    if active_root_group is target_group:
        return
    board.DISPLAY.root_group = target_group
    active_root_group = target_group


def log_crash(e):
    try:
        with open("/error.txt", "a") as f:
            f.write("\n--- Crash ---\n")
            f.write(repr(e) + "\n")
            # Try to write traceback if available
            if traceback is not None:
                try:
                    f.write(traceback.format_exc())
                except Exception:
                    f.write("(traceback unavailable)\n")
            else:
                f.write("(traceback module not available)\n")
            f.flush()
    except Exception as file_exc:
        # As a last resort, try to print to serial
        try:
            print("log_crash failed: ", repr(file_exc))
            print("Original error: ", repr(e))
        except Exception:
            pass

while True:
    try:
        now = time.monotonic()

        # Periodic GC helps reclaim short-lived network/UI allocations on constrained heaps.
        if (now - last_gc_collect) >= 20.0:
            gc_t0 = time.monotonic()
            gc.collect()
            last_gc_collect = now
            if PERF_STALL_LOG_ENABLED:
                gc_ms = int((time.monotonic() - gc_t0) * 1000)
                if gc_ms >= PERF_GC_LOG_THRESHOLD_MS:
                    print("GC ms=%d free=%d" % (gc_ms, gc.mem_free()))

        if PERF_LOG_ENABLED:
            if last_loop_now is not None:
                dt_ms = int((now - last_loop_now) * 1000)
                if dt_ms > loop_dt_max_ms:
                    loop_dt_max_ms = dt_ms
                if PERF_STALL_LOG_ENABLED and dt_ms >= PERF_STALL_THRESHOLD_MS:
                    print(
                        "STALL dt_ms=%d mode=%s free=%d ev=%d"
                        % (
                            dt_ms,
                            "E" if active_root_group is events_group else "I",
                            gc.mem_free(),
                            len(events) if events else 0,
                        )
                    )
            last_loop_now = now
            loop_count_since_log += 1

        if (clock_base_local_epoch is None) or (now - last_time_sync > TIME_SYNC_INTERVAL):
            try:
                sync_t0 = time.monotonic()
                mem_before_sync = gc.mem_free()
                # Split SYNC timing for perf analysis
                t_time0 = time.monotonic()
                local_epoch, offset_seconds = sync_stockholm_epoch()
                t_time1 = time.monotonic()
                line1, line2, line3, line4, current_compact = fetch_malmo_weather_lines()
                t_weather1 = time.monotonic()
                # Always fetch events together with time and weather
                refreshed_events = fetch_next_events()
                t_cal1 = time.monotonic()
                if refreshed_events or not events:
                    events = refreshed_events
                    if events:
                        last_good_events = events
                elif last_good_events:
                    # Keep most recent valid events if a transient refresh returns none.
                    events = last_good_events
                last_calendar_sync = now
                (
                    clock_base_local_epoch,
                    current_utc_offset_seconds,
                    weather_line1,
                    weather_line2,
                    weather_line3,
                    weather_line4,
                    weather_current,
                ) = (
                    local_epoch,
                    offset_seconds,
                    line1,
                    line2,
                    line3,
                    line4,
                    current_compact,
                )
                gc.collect()
                # Compute current time components from synced clock_base_local_epoch
                prepop_hour, prepop_minute, prepop_day, prepop_month_short = stockholm_components_from_epoch(clock_base_local_epoch)
                print("populate events panel after sync")
                update_events_panel(events, prepop_hour, prepop_minute, prepop_day, prepop_month_short)
                log_main_screen_events(events)
                clock_base_monotonic = now
                last_time_sync = now
                sync_reposition_pending = True
                force_events_panel_refresh = True
                if PERF_SYNC_LOG_ENABLED:
                    sync_ms = int((t_cal1 - sync_t0) * 1000)
                    t_time_ms = int((t_time1 - t_time0) * 1000)
                    t_weather_ms = int((t_weather1 - t_time1) * 1000)
                    t_cal_ms = int((t_cal1 - t_weather1) * 1000)
                    print(
                        "SYNC ms=%d (time=%d weather=%d cal=%d) free_before=%d free_after=%d ev=%d"
                        % (
                            sync_ms,
                            t_time_ms,
                            t_weather_ms,
                            t_cal_ms,
                            mem_before_sync,
                            gc.mem_free(),
                            len(events) if events else 0,
                        )
                    )
                # Refresh alarm display/time against latest offset.
                sync_utc_epoch = clock_base_local_epoch - current_utc_offset_seconds
                prev_key = alarm_event_key
                alarm_utc_epoch, alarm_event_utc_epoch, alarm_text, alarm_event_key = schedule_alarm_from_events(
                    events, current_utc_offset_seconds, sync_utc_epoch
                )
                if alarm_event_key != prev_key:
                    alarm_fired_for_key = None
                    log_alarm_choice(
                        alarm_utc_epoch,
                        alarm_event_utc_epoch,
                        alarm_text,
                        alarm_event_key,
                        current_utc_offset_seconds,
                        sync_utc_epoch,
                    )
            except Exception as e:
                log_exception("sync_and_refresh", e)

        if (clock_base_local_epoch is not None) and (clock_base_monotonic is not None):
            elapsed = int(now - clock_base_monotonic)
            current_local_epoch = clock_base_local_epoch + elapsed
            current_utc_epoch = current_local_epoch - current_utc_offset_seconds
            current_hour, current_minute, current_day, current_month_short = stockholm_components_from_epoch(current_local_epoch)
            current_second = current_local_epoch % 60

            if sync_reposition_pending:
                x = random.randint(IDLE_MIN_X, max_x) if max_x > IDLE_MIN_X else IDLE_MIN_X
                y = random.randint(IDLE_MIN_Y, max_y) if max_y > IDLE_MIN_Y else IDLE_MIN_Y
                sync_reposition_pending = False

            # Trigger alarm once when entering alarm window for first event.
            if (
                alarm_utc_epoch is not None
                and alarm_event_utc_epoch is not None
                and current_utc_epoch >= alarm_utc_epoch
                and current_utc_epoch < alarm_event_utc_epoch
                and alarm_event_key is not None
                and alarm_fired_for_key != alarm_event_key
            ):
                set_display_brightness(ACTIVE_BRIGHTNESS)
                set_root_group(events_group)
                update_events_panel(events, current_hour, current_minute, current_day, current_month_short)
                last_events_update_second = current_second
                last_events_update_minute = current_minute
                force_events_panel_refresh = False
                run_alarm_until_touch()
                alarm_fired_for_key = alarm_event_key

            # Keep schedule state fresh between syncs so expired alarms disappear immediately.
            events_pruned = False
            while events:
                first_start_dt = events[0].get("start", {}).get("dateTime", "")
                if not first_start_dt:
                    break
                try:
                    first_event_utc_epoch = rfc3339_to_epoch(first_start_dt)
                except Exception:
                    break
                if first_event_utc_epoch <= current_utc_epoch:
                    events = events[1:]
                    events_pruned = True
                else:
                    break

            alarm_window_expired = (
                alarm_event_utc_epoch is not None and current_utc_epoch >= alarm_event_utc_epoch
            )
            if events_pruned or alarm_window_expired:
                prev_key = alarm_event_key
                alarm_utc_epoch, alarm_event_utc_epoch, alarm_text, alarm_event_key = schedule_alarm_from_events(
                    events, current_utc_offset_seconds, current_utc_epoch
                )
                # Only clear alarm_text if there are no upcoming alarms
                if alarm_utc_epoch is None:
                    alarm_event_utc_epoch = None
                    alarm_text = ""
                    alarm_event_key = None
                if alarm_event_key != prev_key:
                    alarm_fired_for_key = None
                    log_alarm_choice(
                        alarm_utc_epoch,
                        alarm_event_utc_epoch,
                        alarm_text,
                        alarm_event_key,
                        current_utc_offset_seconds,
                        current_utc_epoch,
                    )


        try:
            # One-time warmup of events panel once we have valid time/date.
            if current_hour is not None and events_mode_until == 0:
                prime_events_view(current_hour, current_minute, current_day, current_month_short)
                events_mode_until = -1

            if current_hour is not None:
                idle_changed = (
                    (current_hour != last_idle_hour)
                    or (current_minute != last_idle_minute)
                    or (current_day != last_idle_day)
                    or (current_month_short != last_idle_month_short)
                    or (weather_current != last_idle_weather_current)
                    or (weather_line1 != last_idle_weather_line1)
                    or (weather_line2 != last_idle_weather_line2)
                    or (weather_line3 != last_idle_weather_line3)
                    or (weather_line4 != last_idle_weather_line4)
                    or (alarm_text != last_idle_alarm_text)
                )
                if idle_changed:
                    update_idle_labels(current_hour, current_minute, current_day, current_month_short)
                    last_idle_hour = current_hour
                    last_idle_minute = current_minute
                    last_idle_day = current_day
                    last_idle_month_short = current_month_short
                    last_idle_weather_current = weather_current
                    last_idle_weather_line1 = weather_line1
                    last_idle_weather_line2 = weather_line2
                    last_idle_weather_line3 = weather_line3
                    last_idle_weather_line4 = weather_line4
                    last_idle_alarm_text = alarm_text

            p = ts.touch_point if ENABLE_MAIN_LOOP_TOUCH else None
            # Only trigger events screen if not already active
            if p and current_hour is not None and not (now < events_mode_until):
                set_display_brightness(ACTIVE_BRIGHTNESS)
                # Update the main screen clock label before showing events screen
                update_events_panel(events, current_hour, current_minute, current_day, current_month_short)
                set_root_group(events_group)
                # Switch immediately on touch; defer heavy text relayout to the events-view refresh path.
                force_events_panel_refresh = True
                last_events_update_second = current_second
                last_events_update_minute = None
                # Update the main screen clock before speech
                update_idle_labels(current_hour, current_minute, current_day, current_month_short)
                if (now - last_touch_speak_at) >= TOUCH_SPEAK_COOLDOWN_SECONDS:
                    say_time(current_hour, current_minute)
                    last_touch_speak_at = now
                events_mode_until = now + 12

            ui_stage = 40
            if now < events_mode_until:
                set_display_brightness(ACTIVE_BRIGHTNESS)
                set_root_group(events_group)
                if current_hour is not None and (
                    force_events_panel_refresh or (current_minute != last_events_update_minute)
                ):
                    update_events_panel(events, current_hour, current_minute, current_day, current_month_short)
                    last_events_update_second = current_second
                    last_events_update_minute = current_minute
                    force_events_panel_refresh = False
            else:
                if (now - last_idle_brightness_update) >= 0.5:
                    cached_idle_brightness = adaptive_idle_brightness(current_hour)
                    last_idle_brightness_update = now
                set_display_brightness(cached_idle_brightness)
                set_root_group(idle_group)
                last_events_update_second = None
                last_events_update_minute = None
                force_events_panel_refresh = True
                if current_hour is None:
                    set_idle_position(x, y)
                else:
                    if (now - last_idle_bounce_update) >= 0.10:
                        bounce_idle_labels()
                        last_idle_bounce_update = now
        except MemoryError:
            print("MEMERR ui_stage=%d free=%d alloc=%d" % (ui_stage, gc.mem_free(), gc.mem_alloc()))
            gc.collect()
            time.sleep(0.2)
            continue
    except Exception as e:
        log_crash(e)
        microcontroller.reset()
    if PERF_LOG_ENABLED and ((now - last_perf_log) >= PERF_LOG_INTERVAL_SECONDS):
        if loop_count_since_log <= 0:
            loop_hz = 0
        else:
            loop_hz = int(loop_count_since_log / PERF_LOG_INTERVAL_SECONDS)
        print(
            "PERF free=%d alloc=%d ev=%d mode=%s hz=%d dt_max_ms=%d"
            % (
                gc.mem_free(),
                gc.mem_alloc(),
                len(events) if events else 0,
                "E" if active_root_group is events_group else "I",
                loop_hz,
                loop_dt_max_ms,
            )
        )
        last_perf_log = now
        loop_count_since_log = 0
        loop_dt_max_ms = 0

    time.sleep(0.05)


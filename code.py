# CircuitPython Talking Clock for PyPortal
# Shows bouncing clock, speaks time on touch, and fetches next Google Calendar events.

# --- Imports ---
import time
import os
import random
import board
import busio
import digitalio
import displayio
import audioio
import audiocore
import terminalio
import adafruit_touchscreen
import adafruit_requests
import adafruit_connection_manager
import adafruit_display_text.label as label
from adafruit_bitmap_font import bitmap_font
from weather_wmo_lookup import get_wmo_description
from digitalio import DigitalInOut
from adafruit_esp32spi import adafruit_esp32spi

try:
    import synthio
    SYNTH_SIREN_AVAILABLE = True
except ImportError:
    synthio = None
    SYNTH_SIREN_AVAILABLE = False

try:
    import audiopwmio
except ImportError:
    audiopwmio = None

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
        "&daily=apparent_temperature_max,apparent_temperature_min,weather_code,precipitation_sum,sunrise,sunset"
        "&hourly=precipitation"
        "&timezone=Europe%%2FStockholm&forecast_days=1&forecast_hours=24"
        % (lat, lon)
    )
    try:
        data = get_json_with_retry(url)
        daily = data.get("daily", {})
        hi_list = daily.get("apparent_temperature_max", [])
        lo_list = daily.get("apparent_temperature_min", [])
        wmo_list = daily.get("weather_code", daily.get("weathercode", []))
        precip_sum_list = daily.get("precipitation_sum", [])
        sunrise_list = daily.get("sunrise", [])
        sunset_list = daily.get("sunset", [])
        hourly = data.get("hourly", {})
        hourly_time_list = hourly.get("time", [])
        hourly_precip_list = hourly.get("precipitation", [])

        if not hi_list or not lo_list:
            return "Hi: --\u00b0c Lo: --\u00b0c", "Weather", "Sun: --:-- - --:--", ""

        hi_c = int(round(hi_list[0]))
        lo_c = int(round(lo_list[0]))
        wmo_code = int(wmo_list[0]) if wmo_list else -1
        precip_sum = float(precip_sum_list[0]) if precip_sum_list else 0.0
        sunrise_hhmm = _iso_to_short_hhmm(sunrise_list[0]) if sunrise_list else "--:--"
        sunset_hhmm = _iso_to_short_hhmm(sunset_list[0]) if sunset_list else "--:--"
        desc = get_wmo_description(wmo_code)
        precip_start_line = ""
        first_precip_hour = None
        stop_precip_hour = None
        last_rain_hour = None
        for i in range(min(len(hourly_time_list), len(hourly_precip_list))):
            hour = _iso_to_hour(hourly_time_list[i])
            if hour is None:
                continue
            try:
                if float(hourly_precip_list[i]) > 0.0:
                    if first_precip_hour is None:
                        first_precip_hour = hour
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
                precip_start_line = "%.0f mm between %d-%d" % (precip_sum, first_precip_hour, stop_precip_hour)

        return (
            "Hi: %d\u00b0c Lo: %d\u00b0c" % (hi_c, lo_c),
            "%s" % (desc,),
            "Sun: %s - %s" % (sunrise_hhmm, sunset_hhmm),
            precip_start_line,
        )
    except Exception:
        return "Hi: --\u00b0c Lo: --\u00b0c", "Weather", "Sun: --:-- - --:--", ""


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
    "axel.mansson@skola.malmo.se": 0x7FDBFF,  # light blue
    "axel.magnus.mansson@gmail.com": 0xFF6347,  # tomato red
    "q53iida61vbpa37ft4lgeul80k@group.calendar.google.com": 0xFFB347,  # orange (Felix & Rufus)
}


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


def fetch_next_events(max_results=8):
    global pappavecka_active, pappavecka_date, mammavecka_active, mammavecka_date
    access_token = get_google_access_token()
    if not access_token:
        return []
    try:
        data = get_json_with_retry("http://time.now/developer/api/timezone/Europe/stockholm")

        now_epoch = int(data["unixtime"])
        max_epoch = now_epoch + 24 * 60 * 60
        time_min = epoch_to_utc_iso(now_epoch)
        time_max = epoch_to_utc_iso(max_epoch)

    except Exception:
        now_epoch = None
        max_epoch = None
        time_min = CACHED_NOW_ISO
        time_max = None

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

        try:
            data = get_json_with_retry(url, headers=headers)
            for event in data.get("items", []):
                start = event.get("start", {})
                summary = event.get("summary", "").strip()

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
                    continue

                try:
                    event_epoch = rfc3339_to_epoch(start_dt)
                except Exception:
                    continue

                # Enforce exact now..now+24h window in code.
                if now_epoch is not None and max_epoch is not None:
                    if event_epoch < now_epoch or event_epoch > max_epoch:
                        continue

                event_key = start_dt + "|" + event.get("summary", "")
                if event_key in seen:
                    continue
                seen[event_key] = event_epoch
                event["_calendar_id"] = calendar_id
                merged_events.append(event)
        except Exception:
            pass

    merged_events.sort(key=lambda event: seen.get(event.get("start", {}).get("dateTime", "") + "|" + event.get("summary", ""), 0))
    return merged_events[:max_results]


def format_event_compact(event):
    summary = event.get("summary", "(No title)")
    words = summary.split()
    short_summary = " ".join(words[:2]) if words else "(No title)"
    start_dt = event.get("start", {}).get("dateTime", "")
    if len(start_dt) >= 16:
        hh = int(start_dt[11:13])
        mm = start_dt[14:16]
        hhmm = "00:%s" % mm if hh == 0 else "%d:%s" % (hh, mm)
    else:
        hhmm = "--:--"
    return hhmm, short_summary


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
    date_wday = time.localtime(_local_date_to_noon_epoch(date_str)).tm_wday
    if date_wday >= 5:
        return False
    # Dad-week extra wake alarm is Tue-Fri only.
    extra_dates = (
        _add_days_local_date(pappavecka_date, 1),
        _add_days_local_date(pappavecka_date, 2),
        _add_days_local_date(pappavecka_date, 3),
        _add_days_local_date(pappavecka_date, 4),
    )
    return date_str in extra_dates


MORNING_LIMIT_HOUR = 10  # Weekday first-event alarms before this hour.
WEEKEND_MORNING_LIMIT_HOUR = 11  # Weekend first-event alarms before this hour.
WEEKDAY_EVENT_ALARM_LEAD_MINUTES = 15
WEEKEND_EVENT_ALARM_LEAD_MINUTES = 30


def schedule_alarm_from_events(events, offset_seconds, now_utc_epoch=None):
    # Alarm is 15 minutes before the first upcoming event.
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
            limit_hour = WEEKEND_MORNING_LIMIT_HOUR if local_tm.tm_wday >= 5 else MORNING_LIMIT_HOUR
            if local_tm.tm_hour >= limit_hour:
                continue

            current = first_morning_event_by_date.get(local_date)
            if (current is None) or (event_utc_epoch < current[0]):
                first_morning_event_by_date[local_date] = (event_utc_epoch, start_dt, event)

        for _local_date, (first_event_epoch, first_start_dt, first_event) in first_morning_event_by_date.items():
            if (now_utc_epoch is not None) and (first_event_epoch <= now_utc_epoch):
                # After the first morning event has started, no more event alarm this day.
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
                    continue
                pappavecka_alarm_utc_epoch = local_date_hhmm_to_utc_epoch(extra_date, 7, 20, offset_seconds)
                if (pappavecka_alarm_utc_epoch + 3600) > now_utc_epoch:
                    candidates.append(
                        (
                            pappavecka_alarm_utc_epoch,
                            pappavecka_alarm_utc_epoch + 3600,
                            "ALARM 07:20",
                            "pappavecka|" + extra_date,
                        )
                    )

    if not candidates:
        return None, None, "", None

    candidates.sort(key=lambda c: c[0])
    selected = candidates[0]
    return selected


def _open_speaker_audio_out():
    if not SYNTH_SIREN_AVAILABLE:
        return None

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
    if not SYNTH_SIREN_AVAILABLE:
        return

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
        synth = synthio.Synthesizer(sample_rate=22050, envelope=env)
        audio.play(synth)

        # Simplified melody from BWV 1068 "Air" (gentle 16-note loop).
        beat_seconds = 0.46
        phrase = (
            (78, 1.0, 0.13),
            (83, 1.5, 0.12),
            (79, 1.0, 0.12),
            (78, 1.0, 0.12),
            (76, 1.0, 0.12),
            (74, 1.0, 0.11),
            (73, 1.0, 0.11),
            (74, 1.5, 0.11),
            (73, 1.0, 0.11),
            (71, 1.0, 0.11),
            (69, 1.5, 0.11),
            (81, 1.0, 0.12),
            (78, 1.0, 0.12),
            (72, 1.0, 0.11),
            (71, 1.0, 0.11),
            (76, 2.0, 0.11),
        )

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

                note = synthio.Note(frequency=synthio.midi_to_hz(midi_note), amplitude=amp)
                current_note = (note,)
                synth.press(current_note)

                note_end = time.monotonic() + (beat_seconds * beats)
                while time.monotonic() < note_end:
                    if ts.touch_point:
                        break
                    if (time.monotonic() - start_t) >= max_duration_seconds:
                        break
                    time.sleep(0.02)

                synth.release(current_note)

                pause_end = time.monotonic() + 0.025
                while time.monotonic() < pause_end:
                    if ts.touch_point:
                        break
                    if (time.monotonic() - start_t) >= max_duration_seconds:
                        break
                    time.sleep(0.01)

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
    except Exception:
        pass
    finally:
        if audio:
            audio.deinit()


def run_pwm_alarm_self_test(duration_seconds=2.0):
    if not SYNTH_SIREN_AVAILABLE:
        return

    audio = None
    synth = None
    current_note = None
    try:
        audio = _open_speaker_audio_out()
        if audio is None:
            return
        env = synthio.Envelope(
            attack_time=0.01,
            decay_time=0.08,
            sustain_level=0.5,
            release_time=0.12,
        )
        synth = synthio.Synthesizer(sample_rate=22050, envelope=env)
        audio.play(synth)

        # Soft two-step chime for boot diagnostics.
        chime = (
            ((71, 74, 78), 0.32, 0.16),
            ((73, 76, 81), 0.32, 0.16),
            ((74, 78, 83), 0.42, 0.15),
        )

        t_end = time.monotonic() + duration_seconds
        while time.monotonic() < t_end:
            for chord, duration, amp in chime:
                if time.monotonic() >= t_end:
                    break
                notes = (
                    synthio.Note(frequency=synthio.midi_to_hz(chord[0]), amplitude=amp),
                    synthio.Note(frequency=synthio.midi_to_hz(chord[1]), amplitude=amp * 0.75),
                    synthio.Note(frequency=synthio.midi_to_hz(chord[2]), amplitude=amp * 0.58),
                )
                current_note = notes
                synth.press(notes)
                time.sleep(duration)
                synth.release(notes)
                time.sleep(0.03)

        if current_note is not None:
            try:
                synth.release(current_note)
            except Exception:
                pass
        audio.stop()
    except Exception:
        pass
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

speaker_enable = digitalio.DigitalInOut(board.SPEAKER_ENABLE)
speaker_enable.direction = digitalio.Direction.OUTPUT
speaker_enable.value = True
#run_pwm_alarm_self_test()
# Boot preview: play the current wake melody briefly so it can be auditioned.
#run_alarm_until_touch(max_duration_seconds=24)

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
idle_weather_line1 = label.Label(weather_font, text="Hi: --\u00b0c Lo: --\u00b0c", color=0xBFE8FF)
idle_weather_line2 = label.Label(weather_font, text="Weather", color=0xBFE8FF)
idle_weather_line3 = label.Label(weather_font, text="Sun: --:-- - --:--", color=0xBFE8FF)
idle_weather_line4 = label.Label(weather_font, text="", color=0xBFE8FF)
WEATHER_Y_EXTRA = 5

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
idle_alarm_label.y = startup_y + startup_row1_h + 6
startup_next_y = idle_alarm_label.y + startup_alarm_h + 6
idle_weather_line1.x = startup_x
idle_weather_line1.y = startup_next_y + WEATHER_Y_EXTRA
idle_weather_line2.x = startup_x
idle_weather_line2.y = idle_weather_line1.y + idle_weather_line1.bounding_box[3] * idle_weather_line1.scale + 4
idle_weather_line3.x = startup_x
idle_weather_line3.y = idle_weather_line2.y + idle_weather_line2.bounding_box[3] * idle_weather_line2.scale + 4
idle_weather_line4.x = startup_x
idle_weather_line4.y = idle_weather_line3.y + idle_weather_line3.bounding_box[3] * idle_weather_line3.scale + 4

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
EVENT_TIME_RIGHT_X = EVENT_TITLE_X - 6
EVENT_ROWS = 8
EVENT_ROW_START_Y = 29
EVENT_ROW_GAP = 16

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
pappa_button_bitmap = displayio.Bitmap(PAPPA_BUTTON_W, PAPPA_BUTTON_H, 2)
pappa_button_palette = displayio.Palette(2)
pappa_button_palette[0] = 0x000000
pappa_button_palette.make_transparent(0)
pappa_button_palette[1] = 0xFF8C00

# Rounded rectangle shape: fill body with orange, keep corner pixels transparent.
for py in range(PAPPA_BUTTON_H):
    for px in range(PAPPA_BUTTON_W):
        pappa_button_bitmap[px, py] = 1

for cx, cy in ((0, 0), (PAPPA_BUTTON_W - 1, 0), (0, PAPPA_BUTTON_H - 1), (PAPPA_BUTTON_W - 1, PAPPA_BUTTON_H - 1)):
    pappa_button_bitmap[cx, cy] = 0

for cx, cy in ((1, 0), (0, 1), (PAPPA_BUTTON_W - 2, 0), (PAPPA_BUTTON_W - 1, 1), (0, PAPPA_BUTTON_H - 2), (1, PAPPA_BUTTON_H - 1), (PAPPA_BUTTON_W - 2, PAPPA_BUTTON_H - 1), (PAPPA_BUTTON_W - 1, PAPPA_BUTTON_H - 2)):
    pappa_button_bitmap[cx, cy] = 0

pappa_button_bg = displayio.TileGrid(
    pappa_button_bitmap,
    pixel_shader=pappa_button_palette,
    x=board.DISPLAY.width - PAPPA_BUTTON_W - PAPPA_BUTTON_MARGIN,
    y=board.DISPLAY.height - PAPPA_BUTTON_H - PAPPA_BUTTON_MARGIN,
)
pappa_button_label_top = label.Label(idle_font, text="PAPPA", color=0x000000)
pappa_button_label_bottom = label.Label(idle_font, text="VECKA", color=0x000000)

pappa_button_label_top.anchor_point = (0.5, 0.5)
pappa_button_label_bottom.anchor_point = (0.5, 0.5)
pappa_button_label_top.anchored_position = (
    pappa_button_bg.x + (PAPPA_BUTTON_W // 2),
    pappa_button_bg.y + int(PAPPA_BUTTON_H * 0.33),
)
pappa_button_label_bottom.anchored_position = (
    pappa_button_bg.x + (PAPPA_BUTTON_W // 2),
    pappa_button_bg.y + int(PAPPA_BUTTON_H * 0.70),
)
pappa_button_bg.hidden = True
pappa_button_label_top.hidden = True
pappa_button_label_bottom.hidden = True
events_group.append(pappa_button_bg)
events_group.append(pappa_button_label_top)
events_group.append(pappa_button_label_bottom)

board.DISPLAY.root_group = idle_group

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
weather_line1 = "Hi: --\u00b0c Lo: --\u00b0c"
weather_line2 = "Weather"
weather_line3 = "Sun: --:-- - --:--"
weather_line4 = ""


def right_align(lbl, margin=6):
    lbl.x = board.DISPLAY.width - (lbl.bounding_box[2] * lbl.scale) - margin


def update_idle_labels(hour, minute, day, month_short):
    global x, y, max_x, max_y
    idle_time_label.text = "%02d:%02d" % (hour, minute)
    idle_date_label.text = "%02d %s" % (day, month_short)
    idle_alarm_label.text = alarm_text if alarm_text else ""
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
    block_h += 4 + idle_weather_line3.bounding_box[3] * idle_weather_line3.scale
    if idle_weather_line4.text:
        block_h += 4 + idle_weather_line4.bounding_box[3] * idle_weather_line4.scale
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
        idle_alarm_label.y = int(next_y)
        next_y = int(idle_alarm_label.y + idle_alarm_label.bounding_box[3] * idle_alarm_label.scale + 6)
    else:
        idle_alarm_label.x = int(px)
        idle_alarm_label.y = int(next_y)
    idle_weather_line1.x = int(px)
    idle_weather_line1.y = int(next_y + WEATHER_Y_EXTRA)
    idle_weather_line2.x = int(px)
    idle_weather_line2.y = int(idle_weather_line1.y + idle_weather_line1.bounding_box[3] * idle_weather_line1.scale + 4)
    idle_weather_line3.x = int(px)
    idle_weather_line3.y = int(idle_weather_line2.y + idle_weather_line2.bounding_box[3] * idle_weather_line2.scale + 4)
    idle_weather_line4.x = int(px)
    idle_weather_line4.y = int(idle_weather_line3.y + idle_weather_line3.bounding_box[3] * idle_weather_line3.scale + 4)


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
    events_time_label.text = "%02d:%02d" % (hour, minute)
    events_date_label.text = "%02d %s" % (day, month_short)
    events_alarm_label.text = alarm_text if alarm_text else ""
    left_margin = 6
    bottom_margin = 8
    row_gap = 6

    # Keep one extra row reserved below date for alarm row when active.
    time_h = events_time_label.bounding_box[3] * events_time_label.scale
    date_h = events_date_label.bounding_box[3] * events_date_label.scale
    reserved_h = date_h if alarm_text else 0

    block_top = board.DISPLAY.height - (time_h + row_gap + date_h + row_gap + reserved_h) - bottom_margin
    events_time_label.x = left_margin
    events_time_label.y = int(block_top)
    events_date_label.x = left_margin
    events_date_label.y = int(block_top + time_h + row_gap)
    if alarm_text:
        events_alarm_label.x = left_margin
        events_alarm_label.y = int(events_date_label.y + date_h + row_gap)

    pappa_button_bg.hidden = not pappavecka_active
    pappa_button_label_top.hidden = not pappavecka_active
    pappa_button_label_bottom.hidden = not pappavecka_active

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


# --- Startup calendar fetch ---
events = fetch_next_events()
weather_line1, weather_line2, weather_line3, weather_line4 = fetch_malmo_weather_lines()

current_utc_offset_seconds = _parse_utc_offset_seconds("+00:00")
alarm_utc_epoch, alarm_event_utc_epoch, alarm_text, alarm_event_key = schedule_alarm_from_events(
    events, current_utc_offset_seconds
)
alarm_fired_for_key = None

def prime_events_view(hour, minute, day, month_short):
    # Pre-populate labels so first touch does not pay all text layout/render cost.
    update_events_panel(events, hour, minute, day, month_short)


def sync_time_and_forecast():
    local_epoch, offset_seconds = sync_stockholm_epoch()
    line1, line2, line3, line4 = fetch_malmo_weather_lines()
    return local_epoch, offset_seconds, line1, line2, line3, line4


# --- Main loop ---
TIME_SYNC_INTERVAL = 15 * 60  # 15 min: keep events/alarms fresh during the day.
last_time_sync = 0
clock_base_monotonic = None
clock_base_local_epoch = None
current_utc_offset_seconds = _parse_utc_offset_seconds("+00:00")
current_hour = None
current_minute = None
current_day = None
current_month_short = None
events_mode_until = 0
sync_reposition_pending = False

while True:
    now = time.monotonic()
    if (clock_base_local_epoch is None) or (now - last_time_sync > TIME_SYNC_INTERVAL):
        try:
            # Keep weather refresh coupled to each successful time sync.
            (
                clock_base_local_epoch,
                current_utc_offset_seconds,
                weather_line1,
                weather_line2,
                weather_line3,
                weather_line4,
            ) = sync_time_and_forecast()
            events = fetch_next_events()
            clock_base_monotonic = now
            last_time_sync = now
            sync_reposition_pending = True
            # Refresh alarm display/time against latest offset.
            sync_utc_epoch = clock_base_local_epoch - current_utc_offset_seconds
            prev_key = alarm_event_key
            alarm_utc_epoch, alarm_event_utc_epoch, alarm_text, alarm_event_key = schedule_alarm_from_events(
                events, current_utc_offset_seconds, sync_utc_epoch
            )
            if alarm_event_key != prev_key:
                alarm_fired_for_key = None
        except Exception:
            pass

    if (clock_base_local_epoch is not None) and (clock_base_monotonic is not None):
        elapsed = int(now - clock_base_monotonic)
        current_local_epoch = clock_base_local_epoch + elapsed
        current_utc_epoch = current_local_epoch - current_utc_offset_seconds
        current_hour, current_minute, current_day, current_month_short = stockholm_components_from_epoch(current_local_epoch)

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
            board.DISPLAY.root_group = events_group
            update_events_panel(events, current_hour, current_minute, current_day, current_month_short)
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
            if alarm_event_utc_epoch is not None and current_utc_epoch >= alarm_event_utc_epoch:
                alarm_utc_epoch = None
                alarm_event_utc_epoch = None
                alarm_text = ""
                alarm_event_key = None
            if alarm_event_key != prev_key:
                alarm_fired_for_key = None

    # One-time warmup of events panel once we have valid time/date.
    if current_hour is not None and events_mode_until == 0:
        prime_events_view(current_hour, current_minute, current_day, current_month_short)
        events_mode_until = -1

    if current_hour is not None:
        update_idle_labels(current_hour, current_minute, current_day, current_month_short)

    p = ts.touch_point
    if p and current_hour is not None:
        update_events_panel(events, current_hour, current_minute, current_day, current_month_short)
        board.DISPLAY.root_group = events_group
        say_time(current_hour, current_minute)
        events_mode_until = now + 12
        time.sleep(1)

    if now < events_mode_until:
        board.DISPLAY.root_group = events_group
    else:
        board.DISPLAY.root_group = idle_group
        if current_hour is None:
            set_idle_position(x, y)
        else:
            bounce_idle_labels()

    time.sleep(0.05)

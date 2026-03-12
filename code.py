# CircuitPython Talking Clock for PyPortal
# Shows bouncing clock, speaks time on touch, and fetches next Google Calendar events.

# --- Imports ---
import time
import os
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
import pwmio
from digitalio import DigitalInOut
from adafruit_esp32spi import adafruit_esp32spi

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
    except OSError as e:
        print("could not connect to AP, retrying:", e)
        continue

print("Connected! IP address:", esp.pretty_ip(esp.ip_address))


def ensure_wifi_connected():
    if esp.is_connected:
        return
    print("WiFi disconnected, reconnecting...")
    while not esp.is_connected:
        try:
            esp.connect_AP(ssid, password)
        except OSError as e:
            print("Reconnect failed, retrying:", e)
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
            print("GET retry", attempt + 1, "failed:", e)
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
            print("POST retry", attempt + 1, "failed:", e)
            requests = adafruit_requests.Session(pool, ssl_context)
            time.sleep(1)
    raise last_error


# --- Time helpers ---
def fetch_current_time_iso():
    """Fetch current UTC time from time.now API; fallback to device time."""
    try:
        data = get_json_with_retry("http://time.now/developer/api/timezone/Europe/stockholm")
        now_iso = data["utc_datetime"].split(".")[0] + "Z"
        print("Current UTC time from time.now API:", now_iso)
        return now_iso
    except Exception as e:
        print("Error fetching current time from time.now API:", e)
        now = time.localtime()
        now_iso = "{:04d}-{:02d}-{:02d}T{:02d}:{:02d}:{:02d}Z".format(
            now.tm_year, now.tm_mon, now.tm_mday, now.tm_hour, now.tm_min, now.tm_sec
        )
        print("Fallback UTC time from device:", now_iso)
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
    except Exception as e:
        print("Errors fetching time:", e)
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
        print("Failed to get access token:", resp.text)
        resp.close()
        return None
    except Exception as e:
        print("Direct POST error:", e)
        return None


def fetch_next_events(max_results=8):
    access_token = get_google_access_token()
    if not access_token:
        print("No Google access token")
        return []
    try:
        alarm_utc_epoch = first_event_epoch - 15 * 60
        data = get_json_with_retry("http://time.now/developer/api/timezone/Europe/stockholm")

        now_epoch = int(data["unixtime"])
        max_epoch = now_epoch + 24 * 60 * 60
        time_min = epoch_to_utc_iso(now_epoch)
        time_max = epoch_to_utc_iso(max_epoch)

        print("Using timeMin (now, UTC):", time_min)
        print("Using timeMax (24h ahead, UTC):", time_max)
    except Exception as e:
        print("Error fetching UTC time for calendar:", e)
        now_epoch = None
        max_epoch = None
        time_min = CACHED_NOW_ISO
        time_max = None

    headers = {"Authorization": "Bearer %s" % access_token}
    merged_events = []
    seen = {}

    for calendar_id in GOOGLE_CALENDAR_IDS:
        url = (
            get_calendar_events_api_url(calendar_id)
            + "?maxResults=%d&orderBy=startTime&singleEvents=true&timeMin=%s"
            % (max_results * 4, time_min)
        )
        if time_max:
            url += "&timeMax=%s" % time_max

        #print("Google Calendar API URL:", url)
        try:
            data = get_json_with_retry(url, headers=headers)
            #print("Calendar API response:", data)
            for event in data.get("items", []):
                # Skip all-day events (all-day uses start.date instead of start.dateTime)
                start = event.get("start", {})
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
        except Exception as e:
            print("Error fetching calendar events for", calendar_id, ":", e)

    merged_events.sort(key=lambda event: seen.get(event.get("start", {}).get("dateTime", "") + "|" + event.get("summary", ""), 0))
    return merged_events[:max_results]


def format_event_compact(event):
    summary = event.get("summary", "(No title)")
    words = summary.split()
    short_summary = " ".join(words[:2]) if words else "(No title)"
    start_dt = event.get("start", {}).get("dateTime", "")
    hhmm = start_dt[11:16] if len(start_dt) >= 16 else "--:--"
    return hhmm, short_summary


def utc_epoch_to_local_hhmm(utc_epoch, offset_seconds):
    tm = time.localtime(int(utc_epoch + offset_seconds))
    return tm.tm_hour, tm.tm_min


def schedule_alarm_from_events(events, offset_seconds):
    # Alarm is 15 minutes before the first upcoming event.
    if not events:
        return None, None, "", None

    first_event = events[0]
    first_start_dt = first_event.get("start", {}).get("dateTime", "")
    if not first_start_dt:
        return None, None, "", None

    first_event_epoch = rfc3339_to_epoch(first_start_dt)
    alarm_utc_epoch = first_event_epoch - 15 * 60
    ah, am = utc_epoch_to_local_hhmm(alarm_utc_epoch, offset_seconds)
    alarm_text = "ALARM %02d:%02d" % (ah, am)

    event_key = first_start_dt + "|" + first_event.get("summary", "")
    return alarm_utc_epoch, first_event_epoch, alarm_text, event_key


def run_alarm_until_touch(max_duration_seconds=30):
    # Siren with increasing intensity. Touch stops alarm; hard stop after max_duration_seconds.
    siren = None
    try:
        siren = pwmio.PWMOut(board.A0, frequency=900, duty_cycle=0, variable_frequency=True)
        duty = 3000
        freq = 700
        freq_up = True
        start_t = time.monotonic()
        while True:
            if ts.touch_point:
                break
            if (time.monotonic() - start_t) >= max_duration_seconds:
                break

            siren.frequency = freq
            siren.duty_cycle = min(65535, duty)
            time.sleep(0.03)

            if freq_up:
                freq += 35
                if freq >= 1400:
                    freq_up = False
            else:
                freq -= 35
                if freq <= 700:
                    freq_up = True

            if duty < 52000:
                duty += 350

            if ts.touch_point:
                break

        siren.duty_cycle = 0
    except Exception as e:
        print("Alarm siren error:", e)
    finally:
        if siren:
            siren.deinit()


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

board.DISPLAY.auto_refresh = True

FONT_IDLE_PATH = "/fonts/Nasalization-Regular-40.bdf"
FONT_MAIN_PATH = "/fonts/Nasalization-Regular-20.bdf"

try:
    idle_font = bitmap_font.load_font(FONT_IDLE_PATH)
    main_font = bitmap_font.load_font(FONT_MAIN_PATH)
except Exception as e:
    print("Font load failed, using terminal font:", e)
    idle_font = terminalio.FONT
    main_font = terminalio.FONT

# Main-screen clock must use a font that has ':' glyph.
clock_font = terminalio.FONT

# Idle (bouncing) screen group
idle_group = displayio.Group()
idle_time_label = label.Label(idle_font, text="--:--", color=0xFFFFFF)
idle_date_label = label.Label(main_font, text="-- ---", color=0xC0C0C0)
idle_alarm_label = label.Label(main_font, text="ALARM --:--", color=0xFFD27F)
idle_group.append(idle_time_label)
idle_group.append(idle_date_label)
idle_group.append(idle_alarm_label)

# Events screen group
events_group = displayio.Group()
events_header = label.Label(main_font, text="UP NEXT", color=0x80FFFF)
events_header.x = 6
events_header.y = 8
events_group.append(events_header)

EVENT_TIME_X = 6
EVENT_TITLE_X = 65

event_time_labels = []
event_title_labels = []
for i in range(6):
    row_y = 24 + i * 16

    time_lbl = label.Label(main_font, text="", color=0x80FFFF)
    time_lbl.x = EVENT_TIME_X
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

board.DISPLAY.root_group = idle_group

x, y = 40, 70
vx, vy = 3, 3
max_x = 0
max_y = 0


def right_align(lbl, margin=6):
    lbl.x = board.DISPLAY.width - (lbl.bounding_box[2] * lbl.scale) - margin


def update_idle_labels(hour, minute, day, month_short):
    global max_x, max_y
    idle_time_label.text = "%02d:%02d" % (hour, minute)
    idle_date_label.text = "%02d %s" % (day, month_short)
    idle_alarm_label.text = alarm_text if alarm_text else ""
    block_w = max(
        idle_time_label.bounding_box[2] * idle_time_label.scale,
        idle_date_label.bounding_box[2] * idle_date_label.scale,
        (idle_alarm_label.bounding_box[2] * idle_alarm_label.scale) if alarm_text else 0,
    )
    block_h = idle_time_label.bounding_box[3] * idle_time_label.scale + 6 + idle_date_label.bounding_box[3] * idle_date_label.scale
    if alarm_text:
        block_h += 6 + idle_alarm_label.bounding_box[3] * idle_alarm_label.scale
    max_x = board.DISPLAY.width - block_w
    max_y = board.DISPLAY.height - block_h


def set_idle_position(px, py):
    idle_time_label.x = int(px)
    idle_time_label.y = int(py)
    idle_date_label.x = int(px)
    idle_date_label.y = int(py + idle_time_label.bounding_box[3] * idle_time_label.scale + 6)
    if alarm_text:
        idle_alarm_label.x = int(px)
        idle_alarm_label.y = int(idle_date_label.y + idle_date_label.bounding_box[3] * idle_date_label.scale + 6)


def bounce_idle_labels():
    global x, y, vx, vy
    x += vx
    y += vy
    if x < 10 or x > max_x:
        vx = -vx
        x += vx
    if y < 20 or y > max_y:
        vy = -vy
        y += vy
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

    for i in range(6):
        if i < len(events):
            hhmm, short_summary = format_event_compact(events[i])
            row_color = get_calendar_color(events[i].get("_calendar_id"))
            event_time_labels[i].text = hhmm
            event_title_labels[i].text = short_summary
            event_time_labels[i].color = row_color
            event_title_labels[i].color = row_color
        else:
            event_time_labels[i].text = ""
            event_title_labels[i].text = ""


def play_wav(filename):
    try:
        with open(filename, "rb") as f:
            wav = audiocore.WaveFile(f)
            with audioio.AudioOut(board.A0) as audio:
                audio.play(wav)
                while audio.playing:
                    pass
    except Exception as e:
        print("Error playing", filename, e)


def say_time(hour, minute):
    play_wav("/sounds/%02d.wav" % hour)
    if minute < 10:
        play_wav("/sounds/o.wav")
        play_wav("/sounds/%d.wav" % minute)
    else:
        play_wav("/sounds/%02d.wav" % minute)


# --- Startup calendar fetch ---
events = fetch_next_events()

current_utc_offset_seconds = _parse_utc_offset_seconds("+00:00")
alarm_utc_epoch, alarm_event_utc_epoch, alarm_text, alarm_event_key = schedule_alarm_from_events(
    events, current_utc_offset_seconds
)
alarm_fired_for_key = None

for event in events:
    hhmm, short_summary = format_event_compact(event)
    print("-", hhmm, short_summary)

print("Alarm:", alarm_text)


def prime_events_view(hour, minute, day, month_short):
    # Pre-populate labels so first touch does not pay all text layout/render cost.
    update_events_panel(events, hour, minute, day, month_short)


# --- Main loop ---
TIME_SYNC_INTERVAL = 30 * 60  # 15 min: good balance between drift correction and network load.
last_time_sync = 0
clock_base_monotonic = None
clock_base_local_epoch = None
current_utc_offset_seconds = _parse_utc_offset_seconds("+00:00")
current_hour = None
current_minute = None
current_day = None
current_month_short = None
events_mode_until = 0

while True:
    now = time.monotonic()
    if (clock_base_local_epoch is None) or (now - last_time_sync > TIME_SYNC_INTERVAL):
        try:
            clock_base_local_epoch, current_utc_offset_seconds = sync_stockholm_epoch()
            clock_base_monotonic = now
            last_time_sync = now
            # Refresh alarm display/time against latest offset.
            prev_key = alarm_event_key
            alarm_utc_epoch, alarm_event_utc_epoch, alarm_text, alarm_event_key = schedule_alarm_from_events(
                events, current_utc_offset_seconds
            )
            if alarm_event_key != prev_key:
                alarm_fired_for_key = None
        except Exception as e:
            print("Time sync failed:", e)

    if (clock_base_local_epoch is not None) and (clock_base_monotonic is not None):
        elapsed = int(now - clock_base_monotonic)
        current_local_epoch = clock_base_local_epoch + elapsed
        current_utc_epoch = current_local_epoch - current_utc_offset_seconds
        current_hour, current_minute, current_day, current_month_short = stockholm_components_from_epoch(current_local_epoch)

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
            say_time(current_hour, current_minute)

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
        bounce_idle_labels()

    time.sleep(0.05)

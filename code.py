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


def get_calendar_events_api_url(calendar_id):
    # Calendar ID must be URL encoded when used in the path segment.
    calendar_id_encoded = (
        calendar_id.replace("@", "%40")
        .replace("/", "%2F")
        .replace(" ", "%20")
    )
    return "https://www.googleapis.com/calendar/v3/calendars/{}/events".format(calendar_id_encoded)


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


def fetch_next_events(max_results=6):
    access_token = get_google_access_token()
    if not access_token:
        print("No Google access token")
        return []
    try:
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

        print("Google Calendar API URL:", url)
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

# Idle (bouncing) screen group
idle_group = displayio.Group()
idle_time_label = label.Label(terminalio.FONT, text="--:--", color=0xFFFFFF, scale=5)
idle_date_label = label.Label(terminalio.FONT, text="-- ---", color=0xC0C0C0, scale=2)
idle_group.append(idle_time_label)
idle_group.append(idle_date_label)

# Events screen group
events_group = displayio.Group()
events_header = label.Label(terminalio.FONT, text="UP NEXT", color=0x80FFFF, scale=1)
events_header.x = 6
events_header.y = 8
events_group.append(events_header)

event_labels = []
for i in range(6):
    lbl = label.Label(terminalio.FONT, text="", color=0xFFFFFF, scale=1)
    lbl.x = 6
    lbl.y = 24 + i * 18
    events_group.append(lbl)
    event_labels.append(lbl)

events_time_label = label.Label(terminalio.FONT, text="--:--", color=0xFFFFFF, scale=3)
events_date_label = label.Label(terminalio.FONT, text="-- ---", color=0xC0C0C0, scale=2)
events_group.append(events_time_label)
events_group.append(events_date_label)

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
    block_w = max(
        idle_time_label.bounding_box[2] * idle_time_label.scale,
        idle_date_label.bounding_box[2] * idle_date_label.scale,
    )
    block_h = (
        idle_time_label.bounding_box[3] * idle_time_label.scale
        + 6
        + idle_date_label.bounding_box[3] * idle_date_label.scale
    )
    max_x = board.DISPLAY.width - block_w
    max_y = board.DISPLAY.height - block_h


def set_idle_position(px, py):
    idle_time_label.x = int(px)
    idle_time_label.y = int(py)
    idle_date_label.x = int(px)
    idle_date_label.y = int(py + idle_time_label.bounding_box[3] * idle_time_label.scale + 6)


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
    right_align(events_time_label, margin=6)
    right_align(events_date_label, margin=6)
    events_time_label.y = 24
    events_date_label.y = 24 + events_time_label.bounding_box[3] * events_time_label.scale + 6

    for i, lbl in enumerate(event_labels):
        if i < len(events):
            hhmm, short_summary = format_event_compact(events[i])
            lbl.text = "%s %s" % (hhmm, short_summary)
        else:
            lbl.text = ""


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

for event in events:
    hhmm, short_summary = format_event_compact(event)
    print("-", hhmm, short_summary)


# --- Main loop ---
last_time_update = 0
current_hour = None
current_minute = None
current_day = None
current_month_short = None
events_mode_until = 0

while True:
    now = time.monotonic()
    if (current_hour is None) or (now - last_time_update > 600):
        hour, minute, day, month_short = get_stockholm_time()
        if hour is not None:
            current_hour = hour
            current_minute = minute
            current_day = day
            current_month_short = month_short
            update_idle_labels(hour, minute, day, month_short)
        last_time_update = now

    if current_hour is not None:
        update_idle_labels(current_hour, current_minute, current_day, current_month_short)

    p = ts.touch_point
    if p and current_hour is not None:
        say_time(current_hour, current_minute)
        update_events_panel(events, current_hour, current_minute, current_day, current_month_short)
        board.DISPLAY.root_group = events_group
        events_mode_until = now + 12
        time.sleep(1)

    if now < events_mode_until:
        board.DISPLAY.root_group = events_group
    else:
        board.DISPLAY.root_group = idle_group
        bounce_idle_labels()

    time.sleep(0.05)

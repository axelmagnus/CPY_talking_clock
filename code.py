
# CircuitPython Talking Clock for PyPortal (NTP version)
# Plays the current time in 24h format when the screen is touched
import time
import board
import digitalio
import audioio
import adafruit_touchscreen
import busio
from digitalio import DigitalInOut
from adafruit_esp32spi import adafruit_esp32spi
from adafruit_esp32spi.wifi import WifiManager
import adafruit_ntp
import os

# Touchscreen setup (calibration may need adjustment for your device)
ts = adafruit_touchscreen.Touchscreen(
	board.TOUCH_XL, board.TOUCH_XR, board.TOUCH_YD, board.TOUCH_YU,
	calibration=((5200, 59000), (5800, 57000)),
	size=(320, 240)
)

# Speaker setup
speaker_enable = digitalio.DigitalInOut(board.SPEAKER_ENABLE)
speaker_enable.direction = digitalio.Direction.OUTPUT
speaker_enable.value = True

# ESP32 SPI setup for PyPortal
esp32_cs = DigitalInOut(board.ESP_CS)
esp32_ready = DigitalInOut(board.ESP_BUSY)
esp32_reset = DigitalInOut(board.ESP_RESET)
spi = busio.SPI(board.SCK, board.MOSI, board.MISO)
esp = adafruit_esp32spi.ESP_SPIcontrol(spi, esp32_cs, esp32_ready, esp32_reset)


# WiFi credentials from settings.toml
ssid = os.getenv("CIRCUITPY_WIFI_SSID")
password = os.getenv("CIRCUITPY_WIFI_PASSWORD")
wifi_manager = WifiManager(esp, ssid, password)
wifi_manager.connect()
print("Connected! IP address:", esp.pretty_ip(esp.ip_address))

# Set up connection manager and NTP
import adafruit_connection_manager
pool = adafruit_connection_manager.get_spi_socketpool(esp)
ntp = adafruit_ntp.NTP(pool, tz_offset=0)  # UTC; adjust tz_offset for your timezone

def play_wav(filename):
	try:
		with open(filename, "rb") as f:
			wav = audioio.WaveFile(f)
			with audioio.AudioOut(board.A0) as audio:
				audio.play(wav)
				while audio.playing:
					pass
	except Exception as e:
		print("Error playing", filename, e)

def say_time(hour, minute):
	play_wav("/sounds/%02d.wav" % hour)
	play_wav("/sounds/hours.wav")
	play_wav("/sounds/%02d.wav" % minute)
	play_wav("/sounds/minutes.wav")

while True:
	p = ts.touch_point
	if p:
		now = ntp.datetime
		say_time(now.tm_hour, now.tm_min)
		time.sleep(1)  # debounce
	time.sleep(0.1)

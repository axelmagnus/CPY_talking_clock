"""
Run this script to get a new Google refresh token.
1. It opens a browser for Google login.
2. After you approve, the browser will show an error page (redirect fails) —
   that is expected. Copy the full URL from the browser address bar and paste it here.
"""
import toml, requests, urllib.parse, webbrowser

with open('/Volumes/CIRCUITPY/settings.toml') as f:
    config = toml.load(f)

CLIENT_ID = config['GOOGLE_CLIENT_ID']
CLIENT_SECRET = config['GOOGLE_CLIENT_SECRET']
REDIRECT_URI = "http://localhost:8765"
SCOPES = "https://www.googleapis.com/auth/calendar.readonly"

url = (
    "https://accounts.google.com/o/oauth2/v2/auth"
    "?response_type=code"
    "&access_type=offline"
    "&prompt=consent"
    "&client_id=" + urllib.parse.quote(CLIENT_ID) +
    "&redirect_uri=" + urllib.parse.quote(REDIRECT_URI) +
    "&scope=" + urllib.parse.quote(SCOPES)
)
print("Opening browser for Google login...")
print("After approving, the browser will show an error — that is OK.")
print("Copy the full URL from the browser address bar and paste it below.\n")
webbrowser.open(url)

pasted = input("Paste the full redirect URL here: ").strip()
parsed = urllib.parse.urlparse(pasted)
params = urllib.parse.parse_qs(parsed.query)
auth_code = params.get('code', [None])[0]

if not auth_code:
    print("ERROR: Could not find 'code' in the URL. Did you copy the full URL?")
    exit(1)

resp = requests.post("https://oauth2.googleapis.com/token", data={
    "code": auth_code,
    "client_id": CLIENT_ID,
    "client_secret": CLIENT_SECRET,
    "redirect_uri": REDIRECT_URI,
    "grant_type": "authorization_code",
})

data = resp.json()
if 'refresh_token' not in data:
    print("ERROR:", data)
    exit(1)

new_token = data['refresh_token']
print("\n=== NEW REFRESH TOKEN ===")
print(new_token)
print("\nUpdating settings.toml on CIRCUITPY...")

with open('/Volumes/CIRCUITPY/settings.toml', 'r') as f:
    content = f.read()

# Find and replace the old token line
import re
new_content = re.sub(
    r'(GOOGLE_REFRESH_TOKEN\s*=\s*)".+"',
    f'GOOGLE_REFRESH_TOKEN = "{new_token}"',
    content
)

with open('/Volumes/CIRCUITPY/settings.toml', 'w') as f:
    f.write(new_content)

# Also update the repo copy
with open('/Users/axelmansson/Documents/GitHub/CPY talking clock/settings.toml', 'r') as f:
    content2 = f.read()
new_content2 = re.sub(
    r'(GOOGLE_REFRESH_TOKEN\s*=\s*)".+"',
    f'GOOGLE_REFRESH_TOKEN = "{new_token}"',
    content2
)
with open('/Users/axelmansson/Documents/GitHub/CPY talking clock/settings.toml', 'w') as f:
    f.write(new_content2)

print("Done! Both settings.toml files updated.")

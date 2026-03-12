import os
import requests

client_id = os.getenv("GOOGLE_CLIENT_ID")
client_secret = os.getenv("GOOGLE_CLIENT_SECRET")
refresh_token = os.getenv("GOOGLE_REFRESH_TOKEN")

token_url = "https://oauth2.googleapis.com/token"
data = {
    "client_id": client_id,
    "client_secret": client_secret,
    "refresh_token": refresh_token,
    "grant_type": "refresh_token",
}

response = requests.post(token_url, data=data)
print(response.status_code)
print(response.text)

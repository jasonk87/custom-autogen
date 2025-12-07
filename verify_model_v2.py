
import requests
import json
import os

API_KEY = "AIzaSyCnERyshzrZbyuQ9KbGBZTEWA4jC37cJz8"
MODEL = "gemini-2.0-flash"

def test_native_api():
    print(f"Testing Native API with {MODEL}...")
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent?key={API_KEY}"
    payload = {
        "contents": [{
            "parts": [{"text": "Explain how AI works in a few words"}]
        }]
    }
    try:
        response = requests.post(url, json=payload)
        print(f"Status Code: {response.status_code}")
        if response.status_code == 200:
            print("Success!")
            # print(response.json())
        else:
            print(f"Error: {response.text}")
    except Exception as e:
        print(f"Exception: {e}")

if __name__ == "__main__":
    test_native_api()

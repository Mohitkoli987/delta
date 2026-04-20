
"""
keep_alive.py - Self-ping to prevent Render.com sleep
Har 10 minute mein apne aap ko ping karta hai
"""
import threading
import time
import os
import requests
 
def self_ping():
    url = os.getenv('RENDER_EXTERNAL_URL', '')
    if not url:
        print("[KEEP-ALIVE] ⚠️ RENDER_EXTERNAL_URL not set - running locally?")
        return
    ping_url = f"{url}/ping"
    print(f"[KEEP-ALIVE] 🏓 Will ping {ping_url} every 10 min")
    while True:
        try:
            time.sleep(600)
            r = requests.get(ping_url, timeout=15)
            print(f"[KEEP-ALIVE] ✅ {time.strftime('%H:%M:%S')} → HTTP {r.status_code}")
        except Exception as e:
            print(f"[KEEP-ALIVE] ⚠️ Ping error (safe to ignore): {e}")
 
def start_keep_alive():
    t = threading.Thread(target=self_ping, daemon=True)
    t.start()
    print("[KEEP-ALIVE] 🚀 Started")
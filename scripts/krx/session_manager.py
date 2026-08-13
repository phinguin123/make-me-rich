import _paths
from _paths import SESSION_PATH
import requests
import time
import json
import os

# Your initial "Manual" values
JSID = "x1C1gxtKUDB7G8obu0YXRQ6oUUJAPG3MgDijTODkkUjTAdmqtQLq4NHASxy1jaEi.bWRjX2RvbWFpbi9tZGNvd2FwMi1tZGNhcHAwMQ==" 
VISITOR = "m-m6UGcAeZv"

def manage_session():
    session = requests.Session()
    
    # Load cookies from file if they exist, otherwise use defaults
    if SESSION_PATH.exists():
        with SESSION_PATH.open("r") as f:
            cookies = json.load(f)
            for k, v in cookies.items():
                session.cookies.set(k, v, domain="data.krx.co.kr")
    else:
        session.cookies.set("JSESSIONID", JSID, domain="data.krx.co.kr")
        session.cookies.set("__smVisitorID", VISITOR, domain="data.krx.co.kr")
        session.cookies.set("mdc.client_session", "true", domain="data.krx.co.kr")

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Referer": "https://data.krx.co.kr/contents/MDC/MDI/mdiLoader/index.cmd?menuId=MDC0203",
        "X-Requested-With": "XMLHttpRequest"
    }

    while True:
        url = "https://data.krx.co.kr/contents/MDC/MAIN/main/extendSession.cmd"
        try:
            resp = session.post(url, headers=headers, data={})
            
            # If the server says LOGOUT even on the extend call, we know it's dead
            if "LOGOUT" in resp.text or resp.status_code != 200:
                print(f"[{time.strftime('%H:%M:%S')}] SESSION DEAD. Please re-capture JSESSIONID.")
            else:
                # Save the current state
                with SESSION_PATH.open("w") as f:
                    json.dump(session.cookies.get_dict(), f)
                print(f"[{time.strftime('%H:%M:%S')}] Heartbeat OK. Session extended.")
        
        except Exception as e:
            print(f"Connection Error: {e}")

        # PING MORE OFTEN: Every 10 minutes instead of 25 to be safe
        time.sleep(600)

if __name__ == "__main__":
    manage_session()
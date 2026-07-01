#!/usr/bin/env python3
import re
import time
from http.cookiejar import MozillaCookieJar

import requests

s = requests.Session()
s.headers["User-Agent"] = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/131.0.0.0 Safari/537.36"
s.headers["X-IG-App-ID"] = "936619743392459"
cj = MozillaCookieJar("/root/instagram_cookies.txt")
cj.load(ignore_discard=True, ignore_expires=True)
s.cookies.update(cj)
csrf = s.cookies.get("csrftoken", domain=".instagram.com")
if csrf:
    s.headers["X-CSRFToken"] = csrf

for username in ["ml_date", "godofmlbb"]:
    time.sleep(15)
    url = f"https://www.instagram.com/api/v1/users/web_profile_info/?username={username}"
    r = s.get(url, timeout=40)
    print(username, "api", r.status_code, len(r.text))
    if r.ok:
        edges = r.json()["data"]["user"]["edge_owner_to_timeline_media"]["edges"]
        print("  posts", len(edges))
        for e in edges[:2]:
            n = e["node"]
            print(" ", n.get("shortcode"), (n.get("edge_media_to_caption", {}).get("edges", [{}])[0].get("node", {}).get("text", "")[:50]))

"""Controlled authenticated read-only load test; always deletes its user."""
import concurrent.futures
import http.cookiejar
import os
import secrets
import time
import urllib.parse
import urllib.request

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.production")
import django
django.setup()
from django.contrib.auth import get_user_model

BASE = "https://www.stock-bazaar.online"
USER = "loadtest_" + secrets.token_hex(8)
PASSWORD = secrets.token_urlsafe(24)
HEADERS = {"Host": "www.stock-bazaar.online", "X-Forwarded-Proto": "https"}

def opener():
    return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

def login():
    client = opener()
    page = client.open(urllib.request.Request(BASE + "/accounts/login/", headers=HEADERS), timeout=15).read().decode()
    token = page.split('name="csrfmiddlewaretoken" value="')[1].split('"')[0]
    body = urllib.parse.urlencode({"username": USER, "password": PASSWORD, "csrfmiddlewaretoken": token}).encode()
    headers = {**HEADERS, "Referer": "https://www.stock-bazaar.online/accounts/login/"}
    client.open(urllib.request.Request(BASE + "/accounts/login/", data=body, headers=headers), timeout=15).read()
    return client

def request_page(path):
    client = login()
    start = time.perf_counter()
    response = client.open(urllib.request.Request(BASE + path, headers=HEADERS), timeout=30)
    response.read()
    return response.status, (time.perf_counter() - start) * 1000

def run(path, concurrency, count):
    with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as pool:
        rows = list(pool.map(lambda _: request_page(path), range(count)))
    good = [ms for status, ms in rows if 200 <= status < 300]
    return {"path": path, "concurrency": concurrency, "requests": count, "ok": len(good), "failed": count-len(good), "avg_ms": round(sum(good)/len(good), 2), "max_ms": round(max(good), 2)}

try:
    get_user_model().objects.create_user(username=USER, password=PASSWORD)
    for path in ("/dashboard/", "/stocks/"):
        for concurrency in (20, 50):
            print(run(path, concurrency, concurrency * 10), flush=True)
finally:
    get_user_model().objects.filter(username=USER).delete()
    print({"cleanup": "temporary user deleted", "username": USER}, flush=True)

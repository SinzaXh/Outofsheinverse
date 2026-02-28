from __future__ import annotations
import sys, re, threading, subprocess, json, os, time, urllib.parse
from typing import List, Optional, Dict, Any, Tuple
from concurrent.futures import ThreadPoolExecutor, as_completed
import httpx
import requests
import undetected_chromedriver as uc
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.by import By

# ═══════════════════════════════════════════════════════════════════════════════
# CONFIG
# ═══════════════════════════════════════════════════════════════════════════════

BOT_TOKEN        = "7201368733:AAG3Yp-E5g-DExLHEN-ETrv74zeqwuTIhNM"
CHAT_ID          = "7194175926"
VOUCHER_CODE     = "SVC78FBBPUN80MG"
BATCH_SIZE       = 5
COOLDOWN_SECONDS = 5
COOKIES_FILE     = "cookies.json"
IS_RAILWAY       = bool(os.environ.get("RAILWAY_ENVIRONMENT") or os.environ.get("RAILWAY_PROJECT_ID"))
IS_HEADLESS      = IS_RAILWAY or bool(os.environ.get("HEADLESS", ""))

MEN_CATEGORIES = [
    "jeans-189444",
]

# ═══════════════════════════════════════════════════════════════════════════════
# PRODUCT SCRAPER — httpx (no bot detection on GET requests, works fine)
# ═══════════════════════════════════════════════════════════════════════════════

_HEADERS = {
    "accept":          "application/json, text/plain, */*",
    "accept-language": "en-GB,en-US;q=0.9,en;q=0.8",
    "user-agent":      "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Mobile Safari/537.36",
}
_executor     = ThreadPoolExecutor(max_workers=64)
_local        = threading.local()
_cookies_dict: Dict[str,str] = {}
_cookies_lock = threading.Lock()
_cookies_ver  = 0
_cookies_str  = ""


def _page_client() -> httpx.Client:
    if getattr(_local, "pc", None) is None:
        _local.pc = httpx.Client(http2=True, headers=_HEADERS, follow_redirects=True,
                                  timeout=httpx.Timeout(5, read=12, write=5, pool=2))
    return _local.pc


def _cookie_client() -> httpx.Client:
    ver = getattr(_local, "ccv", -1)
    if getattr(_local, "cc", None) is None or ver != _cookies_ver:
        if getattr(_local, "cc", None):
            try: _local.cc.close()
            except: pass
        with _cookies_lock:
            c = dict(_cookies_dict)
        _local.cc  = httpx.Client(http2=True, headers=_HEADERS, cookies=c,
                                   follow_redirects=True,
                                   timeout=httpx.Timeout(5, read=12, write=5, pool=2))
        _local.ccv = _cookies_ver
    return _local.cc


def _sync_cookies_from_browser():
    """Pull fresh cookies from Selenium browser → give to httpx scraper."""
    global _cookies_ver, _cookies_str
    if not driver:
        return
    browser_cookies = driver.get_cookies()
    parts = [f"{c['name']}={c['value']}" for c in browser_cookies if c.get("value")]
    cookie_str = "; ".join(parts)
    with _cookies_lock:
        _cookies_dict.clear()
        _cookies_str = cookie_str
        for part in cookie_str.split(";"):
            part = part.strip()
            if "=" in part:
                k, v = part.split("=", 1)
                _cookies_dict[k.strip()] = v.strip()
        _cookies_ver += 1
    print(f"[COOKIES] Synced {len(_cookies_dict)} cookies from browser (v{_cookies_ver})")


def _get_cookie(name: str) -> str:
    with _cookies_lock:
        return _cookies_dict.get(name, "")


def _fetch_page(url: str) -> Tuple[int, List[str]]:
    try:
        r = _page_client().get(url)
        if r.status_code == 403:
            r = _cookie_client().get(url)
        if r.status_code != 200:
            print(f"[SCRAPER] HTTP {r.status_code}")
            return 1, []
        data  = r.json()
        pag   = data.get("pagination", {})
        pages = max(int(pag.get("totalPages", 0)), int(pag.get("numberOfPages", 0)), 0)
        codes = [str(p["code"]) for p in data.get("products", []) if p.get("code")]
        return max(pages, 1), codes
    except Exception as e:
        print(f"[SCRAPER] Error: {e}")
        return 1, []


def fetch_products(category_slug: str) -> List[str]:
    print(f"\n🔍 Fetching: {category_slug}")
    base = (
        f"https://www.sheinindia.in/api/category/83"
        f"?fields=SITE&currentPage=0&pageSize=40&format=json"
        f"&query=%3Arelevance%3Aundefined%3Anull&facets=undefined%3Anull"
        f"&curated=true&curatedid={category_slug}&gridColumns=2"
        f"&includeUnratedProducts=false&segmentIds=15%2C8%2C19"
        f"&customertype=Existing&advfilter=true&platform=Msite"
        f"&showAdsOnNextPage=false&is_ads_enable_plp=true"
        f"&displayRatings=true&segmentIds=&&store=shein"
    )
    total_pages, codes = _fetch_page(base)
    if total_pages > 1:
        parsed = urllib.parse.urlparse(base)
        params = {k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()}
        def make_url(p):
            params["currentPage"] = str(p)
            return f"https://www.sheinindia.in/api/category/83?{urllib.parse.urlencode(params)}"
        futs = {_executor.submit(_fetch_page, make_url(p)): p for p in range(1, total_pages)}
        for f in as_completed(futs, timeout=30):
            try:
                _, c = f.result(0)
                codes.extend(c)
            except: pass
    print(f"   ✅ {len(codes)} products")
    return codes


# ═══════════════════════════════════════════════════════════════════════════════
# SELENIUM BROWSER
# ═══════════════════════════════════════════════════════════════════════════════

driver = None
_cart_id = ""


def _get_chrome_version() -> Optional[int]:
    try:
        out = subprocess.check_output(["google-chrome", "--version"], text=True)
        return int(re.search(r"(\d+)\.", out).group(1))
    except:
        return None


def init_browser():
    global driver
    print(f"\n🚀 Launching browser (headless={IS_HEADLESS}, railway={IS_RAILWAY})")
    opts = uc.ChromeOptions()
    if IS_HEADLESS:
        opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--window-size=414,896")  # mobile size — matches mobile UA
    opts.add_argument("--disable-blink-features=AutomationControlled")
    if IS_RAILWAY:
        opts.add_argument("--single-process")
        opts.add_argument("--no-zygote")
        opts.add_argument("--user-data-dir=/tmp/uc_profile")

    ver = _get_chrome_version()
    print(f"🔍 Chrome: {ver}")
    driver = uc.Chrome(options=opts, version_main=ver, use_subprocess=True)
    driver.set_page_load_timeout(60)
    driver.execute_cdp_cmd("Network.setUserAgentOverride", {
        "userAgent": "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Mobile Safari/537.36",
        "platform":  "Android",
    })
    print("✅ Browser ready")


def load_cookies():
    """Inject saved cookies then navigate through the site so Akamai sets its own cookies."""
    global _cart_id

    raw = os.environ.get("COOKIES_JSON", "").strip()
    src = "env var"
    if not raw and os.path.exists(COOKIES_FILE):
        with open(COOKIES_FILE) as f:
            raw = f.read().strip()
        src = COOKIES_FILE

    if not raw:
        raise RuntimeError(
            "❌ No cookies!\n"
            "Set COOKIES_JSON in Railway Variables."
        )

    data        = json.loads(raw)
    cookie_list = data.get("cookies", data) if isinstance(data, dict) else data

    # Step 1: land on homepage first (required before setting cookies)
    print("🌐 Loading homepage...")
    driver.get("https://www.sheinindia.in/")
    time.sleep(3)

    # Step 2: inject saved cookies
    for c in cookie_list:
        for field in ("sameSite", "storeId", "hostOnly", "session", "id"):
            c.pop(field, None)
        if "expirationDate" in c:
            c["expiry"] = int(c.pop("expirationDate"))
        try:
            driver.add_cookie(c)
        except:
            pass

    # Step 3: navigate to cart page — THIS IS THE KEY
    # Akamai's JS runs on the real page, sets bm_sv/bm_sz/ak_bmsc cookies
    # Without this, any fetch() from the browser will 403
    print("🛒 Loading cart page (so Akamai sets its cookies)...")
    driver.get("https://www.sheinindia.in/cart")
    time.sleep(5)  # wait for Akamai JS to run and set cookies

    # Step 4: sync all cookies (including Akamai ones) to httpx scraper
    _sync_cookies_from_browser()

    # Step 5: get cart ID
    _cart_id = _get_cookie("C")
    print(f"🛒 Cart ID: {_cart_id or '❌ NOT FOUND'}")
    print(f"✅ Cookies loaded from {src}")
    return _cart_id


def refresh_session():
    """Navigate cart page again to renew Akamai tokens when we get 403."""
    print("🔄 Refreshing session — reloading cart page...")
    driver.get("https://www.sheinindia.in/cart")
    time.sleep(5)
    _sync_cookies_from_browser()
    print("✅ Session refreshed")


# ═══════════════════════════════════════════════════════════════════════════════
# JS FETCH — runs inside the browser (has all cookies including Akamai ones)
# ═══════════════════════════════════════════════════════════════════════════════

_JS = """
var url      = arguments[0];
var method   = arguments[1];
var body     = arguments[2];
var callback = arguments[arguments.length - 1];

fetch(url, {
    method:      method,
    credentials: "include",
    headers: {
        "accept":             "application/json",
        "accept-language":    "en-GB,en-US;q=0.9,en;q=0.8",
        "content-type":       "application/json",
        "sec-ch-ua":          '"Chromium";v="137", "Not/A)Brand";v="24"',
        "sec-ch-ua-mobile":   "?1",
        "sec-ch-ua-platform": '"Android"',
        "sec-fetch-dest":     "empty",
        "sec-fetch-mode":     "cors",
        "sec-fetch-site":     "same-origin",
        "x-tenant-id":        "SHEIN"
    },
    body: body ? JSON.stringify(body) : undefined
})
.then(function(r) {
    var s = r.status;
    return r.text().then(function(t) {
        if (t.trim()[0] === "<") return callback({status: 401, error: "HTML=session expired"});
        try { callback({status: s, body: JSON.parse(t)}); }
        catch(e) { callback({status: s, error: t.substring(0,150)}); }
    });
})
.catch(function(e) { callback({status: 500, error: e.toString()}); });
"""


def _js(method: str, url: str, body: dict = None, retry_on_403: bool = True) -> dict:
    """
    Execute fetch() inside the browser.
    The browser must be on sheinindia.in so same-origin cookies are sent.
    """
    # Must be on sheinindia.in for same-origin fetch to work
    if "sheinindia.in" not in driver.current_url:
        driver.get("https://www.sheinindia.in/cart")
        time.sleep(4)

    try:
        res = driver.execute_async_script(_JS, url, method, body)
    except Exception as e:
        return {"status": 500, "error": str(e)}

    res = res or {"status": 500, "error": "null result"}

    # 403 = Akamai blocked us → refresh cart page to get new tokens, then retry once
    if res.get("status") in [403, 401] and retry_on_403:
        print(f"   ⚠️ {res.get('status')} — refreshing Akamai session...")
        refresh_session()
        return _js(method, url, body, retry_on_403=False)

    return res


def verify_session() -> bool:
    print("🔐 Verifying session...")
    res = _js("GET", "https://www.sheinindia.in/api/cart/microcart")
    if res.get("status") == 200:
        b = res.get("body", {})
        print(f"   ✅ Session OK | Cart: {b.get('code')} | Items: {b.get('cartCount',0)}")
        return True
    print(f"   ❌ Failed: {res.get('status')} — {res.get('error','')[:80]}")
    return False


# ═══════════════════════════════════════════════════════════════════════════════
# CART TRACKER
# ═══════════════════════════════════════════════════════════════════════════════

TRACKER = "cart.json"
def _t_load():
    try:
        with open(TRACKER) as f: return json.load(f)
    except: return []
def _t_push(pid):
    t = _t_load(); t.append(pid)
    with open(TRACKER,"w") as f: json.dump(t,f)
def _t_pop():
    t = _t_load()
    if t: t.pop()
    with open(TRACKER,"w") as f: json.dump(t,f)
def _t_clear():
    with open(TRACKER,"w") as f: json.dump([],f)


# ═══════════════════════════════════════════════════════════════════════════════
# CART OPERATIONS
# ═══════════════════════════════════════════════════════════════════════════════

def add_product(pid: str) -> bool:
    url = f"https://www.sheinindia.in/api/cart/{_cart_id}/product/{pid}/add"
    res = _js("POST", url, body={"quantity": 1})
    if res.get("status") == 200:
        _t_push(pid)
        return True
    print(f"   ❌ Add {pid} failed ({res.get('status')}): {res.get('error','')[:80]}")
    return False


def delete_one() -> bool:
    res = _js("POST", "https://www.sheinindia.in/api/cart/delete", body={"entryNumber": 0})
    return res.get("status") == 200


def clear_cart():
    tracked = _t_load()
    count   = len(tracked)
    if count == 0:
        res   = _js("GET", "https://www.sheinindia.in/api/cart/microcart")
        count = res.get("body", {}).get("cartCount", 0) if res.get("status") == 200 else 0
    if count == 0:
        return
    print(f"🧹 Clearing {count} items...")
    for _ in range(count):
        if delete_one():
            _t_pop()
        time.sleep(0.3)
    _t_clear()
    print("✨ Cart cleared")


def apply_voucher() -> bool:
    print(f"🎫 Testing: {VOUCHER_CODE}...")
    res = _js("POST", "https://www.sheinindia.in/api/cart/apply-voucher",
              body={"voucherId": VOUCHER_CODE, "device": {"client_type": "MSITE"}})
    if res.get("status") == 200:
        print("🎉 VOUCHER HIT!")
        return True
    print(f"❌ Voucher: {res.get('status')}")
    return False


# ═══════════════════════════════════════════════════════════════════════════════
# TELEGRAM
# ═══════════════════════════════════════════════════════════════════════════════

def send_telegram(msg: str):
    try:
        requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={"chat_id": CHAT_ID, "text": msg,
                  "parse_mode": "HTML", "disable_web_page_preview": True},
            timeout=10
        )
    except Exception as e:
        print(f"⚠️ Telegram: {e}")


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════════

def run():
    # 1. Browser
    init_browser()

    # 2. Load cookies + navigate cart page (Akamai tokens get set)
    cart_id = load_cookies()

    # 3. Verify
    if not verify_session():
        raise RuntimeError(
            "❌ Cookies EXPIRED!\n"
            "1. Log in to sheinindia.in in Chrome\n"
            "2. Export cookies via EditThisCookie\n"
            "3. Run convert_cookies.py\n"
            "4. Update COOKIES_JSON in Railway Variables\n"
            "5. Redeploy"
        )

    if not cart_id:
        raise RuntimeError("❌ C cookie missing — cannot determine cart ID")

    print(f"\n📲 Ready | Cart: {cart_id} | Batch: {BATCH_SIZE} | Voucher: {VOUCHER_CODE}")
    clear_cart()

    while True:
        for category in MEN_CATEGORIES:
            print(f"\n{'='*60}\n🚀 CAT: {category}")

            products = fetch_products(category)
            if not products:
                print("⚠️ No products. Waiting 30s...")
                time.sleep(30)
                continue

            total_batches = (len(products) + BATCH_SIZE - 1) // BATCH_SIZE

            for i in range(0, len(products), BATCH_SIZE):
                batch       = products[i:i + BATCH_SIZE]
                batch_num   = i // BATCH_SIZE + 1
                print(f"\n📦 Batch {batch_num}/{total_batches} ({len(batch)} items)")

                # ── ADD ──────────────────────────────────────────────────────
                added = 0
                for pid in batch:
                    print(f"   ➕ {pid}...", end=" ", flush=True)
                    if add_product(pid):
                        print("✅")
                        added += 1
                    else:
                        print("❌")
                    time.sleep(0.3)

                # ── VOUCHER ──────────────────────────────────────────────────
                if added > 0 and apply_voucher():
                    print("🎯 BATCH HIT! Testing individually...")
                    for pid in batch:
                        clear_cart()
                        time.sleep(0.5)
                        if add_product(pid):
                            time.sleep(0.5)
                            if apply_voucher():
                                link = f"https://www.sheinindia.in/p/{pid}"
                                send_telegram(
                                    f"🚨 <b>VOUCHER WORKED!</b>\n"
                                    f"📂 {category}\n"
                                    f"🆔 <code>{pid}</code>\n"
                                    f"🔗 <a href='{link}'>Open Product</a>"
                                )
                                print(f"🏆 WINNER: {pid}")
                            else:
                                print(f"⏩ {pid} — no individual hit")

                # ── CLEAR + COOLDOWN ─────────────────────────────────────────
                clear_cart()
                print(f"⏳ {COOLDOWN_SECONDS}s cooldown...")
                time.sleep(COOLDOWN_SECONDS)

        print("\n🔁 Loop complete. Restarting...")
        # Refresh session periodically so cookies stay alive
        refresh_session()
        time.sleep(5)


if __name__ == "__main__":
    run()

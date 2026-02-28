from __future__ import annotations
import json
import os
import time
from typing import List

# ================= LIBRARY IMPORTS =================
import requests
import undetected_chromedriver as uc
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

# ================= TELEGRAM CONFIG =================
BOT_TOKEN = "7201368733:AAG3Yp-E5g-DExLHEN-ETrv74zeqwuTIhNM"
CHAT_ID   = "7194175926"

# ================= SYSTEM CONFIG =================
COOKIES_FILE       = "cookies.json"
CART_TRACKER_FILE  = "cart.json"
DEFAULT_USER_EMAIL = "victortakla01@gmail.com"

# ─── Detect Railway / headless environment ────────────────────────────────────
IS_RAILWAY  = bool(os.environ.get("RAILWAY_ENVIRONMENT") or os.environ.get("RAILWAY_PROJECT_ID"))
IS_HEADLESS = IS_RAILWAY or bool(os.environ.get("HEADLESS", ""))

# ================= API Endpoints =================
URL_MICROCART     = "https://www.sheinindia.in/api/cart/microcart"
URL_CREATE        = "https://www.sheinindia.in/api/cart/create"
URL_DELETE        = "https://www.sheinindia.in/api/cart/delete"
URL_ADD_FMT       = "https://www.sheinindia.in/api/cart/{cart_id}/product/{product_id}/add"
URL_APPLY_VOUCHER = "https://www.sheinindia.in/api/cart/apply-voucher"
URL_LOGIN_PAGE    = "https://www.sheinindia.in/login"

# ================= SETTINGS =================
BATCH_SIZE       = 5
VOUCHER_CODE     = "SVC78FBBPUN80MG"
COOLDOWN_SECONDS = 5

# ================= MEN CATEGORIES =================
MEN_CATEGORIES = [
    "jeans-189444",  # https://www.sheinindia.in/s/jeans-189444
]

# Global Driver
driver = None

# ─────────────────────────────────────────────────────────────────────────────
# CART TRACKER (JSON)
# ─────────────────────────────────────────────────────────────────────────────
def load_tracker():
    if not os.path.exists(CART_TRACKER_FILE):
        return []
    try:
        with open(CART_TRACKER_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return []

def save_tracker_item(pid):
    items = load_tracker()
    items.append(str(pid))
    with open(CART_TRACKER_FILE, "w") as f:
        json.dump(items, f, indent=2)

def remove_tracker_item():
    items = load_tracker()
    if items:
        items.pop()
        with open(CART_TRACKER_FILE, "w") as f:
            json.dump(items, f, indent=2)

def clear_tracker_file():
    with open(CART_TRACKER_FILE, "w") as f:
        json.dump([], f)

# ─────────────────────────────────────────────────────────────────────────────
# TELEGRAM
# ─────────────────────────────────────────────────────────────────────────────
def send_order_update(message: str, disable_preview: bool = True):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": disable_preview,
    }
    try:
        r = requests.post(url, json=payload, timeout=10)
        if r.status_code != 200:
            print(f"⚠️ Telegram Error: {r.text}")
    except Exception as e:
        print(f"⚠️ Telegram Error: {e}")

# ─────────────────────────────────────────────────────────────────────────────
# BROWSER CORE  ←  PERMANENT CHROMEDRIVER FIX FOR RAILWAY
# ─────────────────────────────────────────────────────────────────────────────
def _build_options() -> uc.ChromeOptions:
    options = uc.ChromeOptions()

    if IS_HEADLESS:
        options.add_argument("--headless=new")
        options.add_argument("--window-size=1920,1080")

    # Required inside Docker / Railway
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--disable-software-rasterizer")
    options.add_argument("--disable-extensions")
    options.add_argument("--disable-setuid-sandbox")
    options.add_argument("--single-process")
    options.add_argument("--no-zygote")
    options.add_argument("--ignore-certificate-errors")
    options.add_argument("--no-first-run")
    options.add_argument("--password-store=basic")
    options.add_argument("--lang=en-US")

    # Profile path: /tmp on Railway, local folder otherwise
    profile_path = "/tmp/uc_profile" if IS_RAILWAY else os.path.join(os.getcwd(), "uc_profile_permanent")
    options.add_argument(f"--user-data-dir={profile_path}")

    return options


def _get_chrome_major_version() -> int | None:
    """
    Reads the actual installed Chrome major version from the binary.
    This ensures UC downloads the MATCHING chromedriver — no more version mismatch.
    """
    import subprocess
    import re

    candidates = [
        "google-chrome",
        "google-chrome-stable",
        "chromium",
        "chromium-browser",
        "/usr/bin/google-chrome",
        "/usr/bin/google-chrome-stable",
    ]
    for binary in candidates:
        try:
            out = subprocess.check_output(
                [binary, "--version"],
                stderr=subprocess.DEVNULL,
                timeout=5,
            ).decode()
            match = re.search(r"(\d+)\.\d+\.\d+", out)
            if match:
                version = int(match.group(1))
                print(f"🔍 Detected Chrome version: {version} (via {binary})")
                return version
        except Exception:
            continue
    print("⚠️ Could not detect Chrome version — letting UC guess.")
    return None


def init_browser():
    global driver
    if driver is not None:
        return

    print(f"\n🚀 Launching Browser (headless={IS_HEADLESS}, railway={IS_RAILWAY})…")
    options = _build_options()

    # Detect the ACTUAL installed Chrome major version so UC downloads
    # the correct matching chromedriver — permanently fixes version mismatch.
    chrome_version = _get_chrome_major_version()

    driver = uc.Chrome(
        options=options,
        version_main=chrome_version,  # exact match — no more "supports Chrome 146 / got 145"
        use_subprocess=True,          # avoids signal conflicts on Linux
    )
    driver.set_page_load_timeout(60)
    driver.get("https://www.sheinindia.in/")
    time.sleep(5)
    print("✅ Browser ready.")


def save_cookies():
    global driver
    if not driver:
        return
    try:
        cookies = driver.get_cookies()
        with open(COOKIES_FILE, "w") as f:
            json.dump(cookies, f, indent=2)
        print("💾 Cookies saved.")
    except Exception:
        pass


def _load_raw_cookies() -> list:
    """
    Load cookies from COOKIES_JSON env var first (Railway-friendly),
    then fall back to cookies.json file on disk.
    """
    # Priority 1: Environment variable — paste JSON directly in Railway Variables
    raw_env = os.environ.get("COOKIES_JSON", "").strip()
    if raw_env:
        print("✅ Loading cookies from COOKIES_JSON env variable…")
        try:
            return json.loads(raw_env)
        except Exception as e:
            print(f"⚠️ Failed to parse COOKIES_JSON env var: {e}")

    # Priority 2: cookies.json file on disk
    if os.path.exists(COOKIES_FILE):
        print(f"✅ Found {COOKIES_FILE} on disk. Loading…")
        try:
            with open(COOKIES_FILE, "r") as f:
                return json.load(f)
        except Exception as e:
            print(f"⚠️ Failed to read {COOKIES_FILE}: {e}")

    return []


def load_cookies_if_exist():
    cookies = _load_raw_cookies()

    if not cookies:
        if IS_HEADLESS:
            raise RuntimeError(
                "\n❌ No cookies found on Railway!\n"
                "   Fix in 30 seconds:\n"
                "   1. Railway dashboard → your service → Variables tab\n"
                "   2. Add:  COOKIES_JSON  =  <paste entire cookies.json content>\n"
                "   3. Redeploy."
            )
        print("🔓 Opening Login Page…")
        driver.get(URL_LOGIN_PAGE)
        input("🔴 Log in manually, then press ENTER… ")
        save_cookies()
        return

    # Inject cookies into browser
    print(f"🍪 Injecting {len(cookies)} cookies…")
    try:
        driver.get("https://www.sheinindia.in/")
        time.sleep(2)
        for cookie in cookies:
            cookie.pop("sameSite", None)
            try:
                driver.add_cookie(cookie)
            except Exception:
                pass
        driver.refresh()
        time.sleep(3)
        print("✅ Cookies injected successfully.")
    except Exception as e:
        print(f"⚠️ Cookie injection error: {e}")


def refresh_browser_and_update_sensor():
    global driver
    print("🔄 REFRESH TRIGGERED: Updating Sensor & Cookies…")
    driver.refresh()
    time.sleep(5)
    driver.execute_script("window.scrollTo(0, 500);")
    time.sleep(1)
    save_cookies()
    print("✅ Refresh complete.")


def browser_api_call(method: str, url: str, json_data: dict = None):
    global driver
    if not driver:
        init_browser()

    js_script = """
    var callback = arguments[arguments.length - 1];
    var url    = arguments[0];
    var method = arguments[1];
    var data   = arguments[2];

    var options = {
        method: method,
        headers: {
            'content-type': 'application/json',
            'x-tenant-id': 'SHEIN',
            'accept': 'application/json'
        }
    };
    if (data) options.body = JSON.stringify(data);

    var timeout = new Promise(function(_, reject) {
        setTimeout(function() { reject(new Error("Request timed out")); }, 15000);
    });

    Promise.race([fetch(url, options), timeout])
        .then(function(r) {
            return r.json().then(function(body) {
                return { status: r.status, body: body };
            });
        })
        .then(function(result) { callback(result); })
        .catch(function(err)   { callback({ status: 500, error: err.toString() }); });
    """
    try:
        return driver.execute_async_script(js_script, url, method, json_data)
    except Exception as e:
        print(f"⚠️ Browser Bridge Error: {e}")
        return {"status": 500, "error": str(e)}


def ensure_login():
    load_cookies_if_exist()
    res = browser_api_call("GET", URL_MICROCART)
    if not res or res.get("status") == 401 or "login" in driver.current_url:
        print("\n⚠️ Session invalid.")
        if IS_HEADLESS:
            raise RuntimeError("❌ Session invalid on Railway. Regenerate cookies.json locally.")
        input("🔴 Log in manually then press ENTER… ")
        save_cookies()

# ─────────────────────────────────────────────────────────────────────────────
# CART LOGIC
# ─────────────────────────────────────────────────────────────────────────────
def get_or_create_cart() -> str:
    res = browser_api_call("GET", URL_MICROCART)
    print(f"   🛒 Microcart status: {res.get('status')} | body keys: {list(res.get('body', {}).keys()) if res.get('body') else 'none'}")
    if res and res.get("body") and res["body"].get("code"):
        cart_id = res["body"]["code"]
        print(f"   ✅ Got existing cart: {cart_id}")
        return cart_id
    print(f"   🆕 No existing cart, creating new one…")
    res = browser_api_call("POST", URL_CREATE, {"user": DEFAULT_USER_EMAIL})
    print(f"   🛒 Create cart status: {res.get('status')} | body: {str(res.get('body', ''))[:200]}")
    if res and res.get("body"):
        cart_id = res["body"].get("code", "")
        if cart_id:
            print(f"   ✅ Created cart: {cart_id}")
            return cart_id
    print(f"   ❌ Failed to get/create cart. Full response: {str(res)[:300]}")
    return ""


def clear_cart_bridge():
    tracked_items = load_tracker()
    count_needed  = len(tracked_items)

    if count_needed == 0:
        res = browser_api_call("GET", URL_MICROCART)
        if res and res.get("body") and res["body"].get("cartCount", 0) > 0:
            count_needed = res["body"]["cartCount"]
            print("⚠️ Tracker empty but server has items. Syncing…")
        else:
            return

    print(f"🧹 Clearing {count_needed} items…")
    for _ in range(count_needed):
        del_res = browser_api_call("POST", URL_DELETE, {"entryNumber": 0})
        status  = del_res.get("status", 500)

        if status in [403, 429, 500, 502, 503]:
            print(f"⛔ Error {status} during delete. Refreshing…")
            refresh_browser_and_update_sensor()
            del_res = browser_api_call("POST", URL_DELETE, {"entryNumber": 0})
            status  = del_res.get("status", 500)

        if status == 200:
            print("   ✅ Deleted 1 item.")
            remove_tracker_item()
        else:
            print(f"   ❌ Failed delete (Status: {status})")

        time.sleep(0.2)

    clear_tracker_file()
    print("✨ Cart cleaned.")


def add_product_bridge(cart_id: str, pid: str) -> bool:
    print(f"➕ Adding {pid}…")
    url    = URL_ADD_FMT.format(cart_id=cart_id, product_id=pid)
    res    = browser_api_call("POST", url, {"quantity": 1})
    status = res.get("status", 500)

    if status in [403, 429, 500, 502, 503]:
        print(f"🚨 Error {status}! Refreshing sensor…")
        refresh_browser_and_update_sensor()
        print(f"🔄 Retrying add {pid}…")
        res    = browser_api_call("POST", url, {"quantity": 1})
        status = res.get("status", 500)

    if status != 200:
        print(f"❌ Failed to add: {status}")
        return False

    save_tracker_item(pid)
    return True


def apply_voucher_bridge() -> bool:
    print(f"🎫 Testing Voucher: {VOUCHER_CODE}…")
    payload = {
        "voucherId": VOUCHER_CODE,
        "device": {"client_type": "MSITE"},
    }
    res    = browser_api_call("POST", URL_APPLY_VOUCHER, payload)
    status = res.get("status", 500)

    if status == 200:
        print("🎉 VOUCHER HIT! (Status 200)")
        return True

    print(f"❌ Voucher Status: {status}")
    return False


def fetch_products(category_slug: str) -> List[str]:
    """
    Uses the correct SHEIN API with full slug e.g. "jeans-189444"
    Paginates through all pages using pagination.totalPages from response.
    """
    print(f"\n🔍 Fetching category: {category_slug}…")
    product_ids = []

    headers = {
        "user-agent": "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36",
        "x-tenant-id": "SHEIN",
        "accept": "application/json, text/plain, */*",
        "referer": f"https://www.sheinindia.in/s/{category_slug}",
        "origin": "https://www.sheinindia.in",
    }

    # Fetch page 0 first to get total pages
    def fetch_page(page_num: int) -> dict | None:
        url = (
            f"https://www.sheinindia.in/api/category/83"
            f"?fields=SITE&currentPage={page_num}&pageSize=40&format=json"
            f"&query=%3Arelevance%3Aundefined%3Anull&facets=undefined%3Anull"
            f"&curated=true&curatedid={category_slug}&gridColumns=2"
            f"&includeUnratedProducts=false&segmentIds=15%2C8%2C19"
            f"&customertype=Existing&advfilter=true&platform=Msite"
            f"&showAdsOnNextPage=false&is_ads_enable_plp=true"
            f"&displayRatings=true&store=shein"
        )
        try:
            r = requests.get(url, headers=headers, timeout=15)
            print(f"   📡 Page {page_num} — HTTP {r.status_code} | {len(r.content)} bytes")
            if not r.content or not r.text.strip():
                print(f"   ⚠️ Empty response on page {page_num}")
                return None
            return r.json()
        except Exception as e:
            print(f"   ⚠️ Error on page {page_num}: {e}")
            return None

    # Page 0 — also gets total pages
    data = fetch_page(0)
    if not data:
        return product_ids

    total_pages = data.get("pagination", {}).get("totalPages", 1)
    total_results = data.get("pagination", {}).get("totalResults", 0)
    print(f"   📊 Total products: {total_results} across {total_pages} pages")

    for p in data.get("products", []):
        if p.get("code"):
            product_ids.append(str(p["code"]))

    # Remaining pages
    for page in range(1, total_pages):
        data = fetch_page(page)
        if not data:
            break
        for p in data.get("products", []):
            if p.get("code"):
                product_ids.append(str(p["code"]))
        time.sleep(0.3)  # gentle rate limit

    print(f"   ✅ Total fetched: {len(product_ids)} products")
    return product_ids


# ─────────────────────────────────────────────────────────────────────────────
# MAIN RUN LOOP
# ─────────────────────────────────────────────────────────────────────────────
def run():
    global driver
    init_browser()
    ensure_login()

    print(f"📲 System Ready | Batch: {BATCH_SIZE} | Timer: {COOLDOWN_SECONDS}s | Voucher: {VOUCHER_CODE}")
    clear_cart_bridge()

    while True:
        for category in MEN_CATEGORIES:
            print(f"\n🚀 CAT: {category}")
            all_products = fetch_products(category)
            if not all_products:
                print(f"⚠️ 0 products found for {category}. Waiting 30s before retry…")
                time.sleep(30)
                continue

            cart_id = get_or_create_cart()
            if not cart_id:
                print("⚠️ Cart failed. Waiting 30s before retry (NOT refreshing endlessly)…")
                time.sleep(30)
                continue

            for i in range(0, len(all_products), BATCH_SIZE):
                batch = all_products[i : i + BATCH_SIZE]
                print(f"📦 Batch {i // BATCH_SIZE + 1} ({len(batch)} items)")

                # 1️⃣ ADD BATCH
                count = 0
                for pid in batch:
                    if add_product_bridge(cart_id, pid):
                        count += 1
                    time.sleep(0.2)

                # 2️⃣ CHECK VOUCHER ON BATCH
                if count > 0 and apply_voucher_bridge():
                    print("🎯 VOUCHER HIT! Starting individual verification…")

                    for verify_pid in batch:
                        print(f"🧪 Testing individually: {verify_pid}")
                        clear_cart_bridge()
                        time.sleep(1)

                        if add_product_bridge(cart_id, verify_pid):
                            time.sleep(0.5)

                            if apply_voucher_bridge():
                                product_link = f"https://www.sheinindia.in/p/{verify_pid}"

                                send_order_update(
                                    f"🚨 <b>VOUCHER WORKED!</b>\n"
                                    f"📂 Category: {category}\n"
                                    f"🆔 Product ID: <code>{verify_pid}</code>\n"
                                    f"🔗 <a href='{product_link}'>Open Product</a>"
                                )
                                print(f"✅ Success: {verify_pid} → {product_link}")
                            else:
                                print(f"⏩ {verify_pid} individual fail.")

                # 3️⃣ FINAL CLEAR & WAIT
                clear_cart_bridge()
                print(f"🔄 Cycle done. Waiting {COOLDOWN_SECONDS}s…")
                time.sleep(COOLDOWN_SECONDS)

        print("\n🔁 Restarting main loop…")
        time.sleep(5)


if __name__ == "__main__":
    run()

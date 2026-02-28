from __future__ import annotations
import json
import os
import time
from typing import List

# ================= LIBRARY IMPORTS =================
import requests
import undetected_chromedriver as uc
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

# ================= TELEGRAM CONFIG =================
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
CHAT_ID = os.environ.get("CHAT_ID", "")

# ================= EARNKARO CONFIG =================
EARNKARO_API_TOKEN = os.environ.get("EARNKARO_API_TOKEN", "")
EARNKARO_API_URL = os.environ.get("EARNKARO_API_URL", "https://api.earnkaro.com/v1/convert")

# ================= SYSTEM CONFIG =================
COOKIES_FILE = os.environ.get("COOKIES_FILE", "cookies.json")
CART_TRACKER_FILE = "cart.json"
DEFAULT_USER_EMAIL = os.environ.get("USER_EMAIL", "yoursheinemail")

# ─── Detect Railway / CI / headless environment ───────────────────────────────
IS_RAILWAY = bool(os.environ.get("RAILWAY_ENVIRONMENT") or os.environ.get("RAILWAY_PROJECT_ID"))
IS_HEADLESS = IS_RAILWAY or bool(os.environ.get("HEADLESS", ""))

# API Endpoints
URL_MICROCART     = "https://www.sheinindia.in/api/cart/microcart"
URL_CREATE        = "https://www.sheinindia.in/api/cart/create"
URL_DELETE        = "https://www.sheinindia.in/api/cart/delete"
URL_ADD_FMT       = "https://www.sheinindia.in/api/cart/{cart_id}/product/{product_id}/add"
URL_APPLY_VOUCHER = "https://www.sheinindia.in/api/cart/apply-voucher"
URL_LOGIN_PAGE    = "https://www.sheinindia.in/login"

# ================= SETTINGS =================
BATCH_SIZE       = int(os.environ.get("BATCH_SIZE", 5))
VOUCHER_CODE     = os.environ.get("VOUCHER_CODE", "")
COOLDOWN_SECONDS = int(os.environ.get("COOLDOWN_SECONDS", 5))

# ================= MEN CATEGORIES =================
MEN_CATEGORIES = [
    "jewellery-189440"
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
    if not BOT_TOKEN or not CHAT_ID:
        print("⚠️ Telegram not configured – skipping notification.")
        return
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": disable_preview,
    }
    try:
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        print(f"⚠️ Telegram Error: {e}")

# ─────────────────────────────────────────────────────────────────────────────
# BROWSER CORE  ←  PERMANENT CHROMEDRIVER FIX FOR RAILWAY
# ─────────────────────────────────────────────────────────────────────────────
def _build_options() -> uc.ChromeOptions:
    """
    Build ChromeOptions that work on both local machines AND Railway/Docker.
    All critical headless / sandbox flags are set here once and only here.
    """
    options = uc.ChromeOptions()

    # ── Headless (required on Railway – no display server) ──────────────────
    if IS_HEADLESS:
        options.add_argument("--headless=new")   # new headless avoids detection
        options.add_argument("--window-size=1920,1080")

    # ── Sandbox / security flags (required inside Docker / Railway) ──────────
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")   # avoids /dev/shm OOM crash
    options.add_argument("--disable-gpu")
    options.add_argument("--disable-software-rasterizer")
    options.add_argument("--disable-extensions")
    options.add_argument("--disable-setuid-sandbox")

    # ── Stability ────────────────────────────────────────────────────────────
    options.add_argument("--single-process")          # needed on some Railway tiers
    options.add_argument("--no-zygote")
    options.add_argument("--ignore-certificate-errors")

    # ── Misc ─────────────────────────────────────────────────────────────────
    options.add_argument("--no-first-run")
    options.add_argument("--password-store=basic")
    options.add_argument("--lang=en-US")

    # ── User-data-dir: use /tmp on Railway (writable), local path otherwise ─
    if IS_RAILWAY:
        profile_path = "/tmp/uc_profile"
    else:
        profile_path = os.path.join(os.getcwd(), "uc_profile_permanent")
    options.add_argument(f"--user-data-dir={profile_path}")

    return options


def init_browser():
    global driver
    if driver is not None:
        return

    print(f"\n🚀 Launching Browser (headless={IS_HEADLESS}, railway={IS_RAILWAY})…")
    options = _build_options()

    # ── undetected_chromedriver: let it auto-match Chrome version ────────────
    # version_main=None  →  UC queries the installed Chrome binary automatically.
    # This eliminates the "chromedriver version mismatch" crash permanently.
    driver = uc.Chrome(
        options=options,
        version_main=None,      # auto-detect ← KEY FIX
        use_subprocess=True,    # avoids atexit / signal conflicts on Linux
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


def load_cookies_if_exist():
    if not os.path.exists(COOKIES_FILE):
        if IS_HEADLESS:
            # On Railway there is no keyboard – crash early with a helpful message.
            raise RuntimeError(
                "\n❌ No cookies.json found and we're running headless on Railway.\n"
                "   Run the bot ONCE locally (python main.py), log in when prompted,\n"
                "   then upload the generated cookies.json to your Railway volume or\n"
                "   set its content as the COOKIES_JSON environment variable."
            )
        print("🔓 Opening Login Page…")
        driver.get(URL_LOGIN_PAGE)
        input("🔴 Please log in manually, then press ENTER here… ")
        save_cookies()
    else:
        # Inject saved cookies
        print(f"✅ Found {COOKIES_FILE}. Injecting cookies…")
        try:
            with open(COOKIES_FILE, "r") as f:
                cookies = json.load(f)
            driver.get("https://www.sheinindia.in/")
            time.sleep(2)
            for cookie in cookies:
                # Some keys cause errors when re-injecting
                cookie.pop("sameSite", None)
                try:
                    driver.add_cookie(cookie)
                except Exception:
                    pass
            driver.refresh()
            time.sleep(3)
            print("✅ Cookies injected.")
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
    var url  = arguments[0];
    var method = arguments[1];
    var data = arguments[2];

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
        .then(function(response) {
            return response.json().then(function(body) {
                return { status: response.status, body: body };
            });
        })
        .then(function(result) { callback(result); })
        .catch(function(error) { callback({ status: 500, error: error.toString() }); });
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
            raise RuntimeError(
                "❌ Session invalid and running headless. Regenerate cookies.json locally."
            )
        input("🔴 Log in manually then press ENTER… ")
        save_cookies()

# ─────────────────────────────────────────────────────────────────────────────
# CART LOGIC
# ─────────────────────────────────────────────────────────────────────────────
def get_or_create_cart() -> str:
    res = browser_api_call("GET", URL_MICROCART)
    if res and res.get("body") and res["body"].get("code"):
        return res["body"]["code"]
    res = browser_api_call("POST", URL_CREATE, {"user": DEFAULT_USER_EMAIL})
    if res and res.get("body"):
        return res["body"].get("code", "")
    return ""


def clear_cart_bridge():
    tracked_items = load_tracker()
    count_needed = len(tracked_items)

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
        status = del_res.get("status", 500)

        if status in [403, 429, 500, 502, 503]:
            print(f"⛔ Error {status} during delete. Refreshing…")
            refresh_browser_and_update_sensor()
            del_res = browser_api_call("POST", URL_DELETE, {"entryNumber": 0})
            status = del_res.get("status", 500)

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
    url = URL_ADD_FMT.format(cart_id=cart_id, product_id=pid)
    res = browser_api_call("POST", url, {"quantity": 1})
    status = res.get("status", 500)

    if status in [403, 429, 500, 502, 503]:
        print(f"🚨 Error {status}! Refreshing sensor…")
        refresh_browser_and_update_sensor()
        print(f"🔄 Retrying add {pid}…")
        res = browser_api_call("POST", url, {"quantity": 1})
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
    res = browser_api_call("POST", URL_APPLY_VOUCHER, payload)
    status = res.get("status", 500)

    if status == 200:
        print("🎉 VOUCHER HIT! (Status 200)")
        return True

    print(f"❌ Voucher Status: {status}")
    return False


def fetch_products(curated_id: str) -> List[str]:
    print(f"🔍 Fetching {curated_id}…")
    product_ids = []
    headers = {"user-agent": "Mozilla/5.0", "x-tenant-id": "SHEIN"}
    url = (
        "https://search-edge.services.sheinindia.in/rilfnlwebservices/v4/rilfnl/products/category/83"
        f"?advfilter=true&curatedid={curated_id}&curated=true&pageSize=40&store=shein&fields=FULL&currentPage=0"
    )
    try:
        r = requests.get(url, headers=headers, timeout=15)
        for p in r.json().get("products", []):
            if p.get("code"):
                product_ids.append(str(p["code"]))
    except Exception as e:
        print(f"⚠️ Fetch error: {e}")
    return product_ids


def convert_to_affiliate_link(original_url: str) -> str:
    if not EARNKARO_API_TOKEN:
        return original_url
    headers = {
        "Authorization": f"Bearer {EARNKARO_API_TOKEN}",
        "Content-Type": "application/json",
    }
    payload = {"deal": original_url, "convert_option": "convert_only"}
    try:
        response = requests.post(EARNKARO_API_URL, headers=headers, json=payload, timeout=15)
        if response.status_code == 200:
            data = response.json()
            if "data" in data and len(data["data"]) > 0:
                return data["data"][0].get("affiliate_link", original_url)
        print(f"⚠️ EarnKaro Error: {response.text}")
    except Exception as e:
        print(f"⚠️ Convert Error: {e}")
    return original_url

# ─────────────────────────────────────────────────────────────────────────────
# MAIN RUN LOOP
# ─────────────────────────────────────────────────────────────────────────────
def run():
    global driver
    init_browser()
    ensure_login()

    print(f"📲 System Ready | Batch: {BATCH_SIZE} | Timer: {COOLDOWN_SECONDS}s")
    clear_cart_bridge()

    while True:
        for category in MEN_CATEGORIES:
            print(f"\n🚀 CAT: {category}")
            all_products = fetch_products(category)
            if not all_products:
                continue

            cart_id = get_or_create_cart()
            if not cart_id:
                refresh_browser_and_update_sensor()
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
                                original_link  = f"https://www.sheinindia.in/p/{verify_pid}"
                                affiliate_link = convert_to_affiliate_link(original_link)
                                send_order_update(
                                    f"🚨 <b>VOUCHER WORKED!</b>\n"
                                    f"Cat: {category}\n"
                                    f"🔗 <a href='{affiliate_link}'>Buy Now</a>"
                                )
                                print(f"✅ Success: {verify_pid}")
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

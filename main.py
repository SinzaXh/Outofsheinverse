from __future__ import annotations
import json
import os
import time
from typing import List

# ================= LIBRARY IMPORTS =================
import requests
import undetected_chromedriver as uc

# ================= TELEGRAM CONFIG =================
BOT_TOKEN = "7201368733:AAG3Yp-E5g-DExLHEN-ETrv74zeqwuTIhNM"
CHAT_ID = "7194175926"




# ================= SYSTEM CONFIG =================
COOKIES_FILE = "cookies.json"
CART_TRACKER_FILE = "cart.json"
CHROME_PROFILE_PATH = os.path.join(os.getcwd(), "uc_profile_permanent") 
DEFAULT_USER_EMAIL = "victortakla01@gmail.com"

# API Endpoints
URL_MICROCART = "https://www.sheinindia.in/api/cart/microcart"
URL_CREATE = "https://www.sheinindia.in/api/cart/create"
URL_DELETE = "https://www.sheinindia.in/api/cart/delete"
URL_ADD_FMT = "https://www.sheinindia.in/api/cart/{cart_id}/product/{product_id}/add"
URL_APPLY_VOUCHER = "https://www.sheinindia.in/api/cart/apply-voucher"
URL_LOGIN_PAGE = "https://www.sheinindia.in/login"

# ================= SETTINGS =================
BATCH_SIZE = 5 
VOUCHER_CODE = "  "
COOLDOWN_SECONDS = 5  

# ================= MEN CATEGORIES =================
MEN_CATEGORIES = [
    # "sweatshirts--hoodies-173109", "trousers--pants-173110", "jeans-173111", 
    #"co-ords-173112", "t-shirts-173113", "shirts-173114", 
    #"trackpants-173115", "cargo-173271", "long-sleeve-styles-176989", 
    #"comfy-hoodie-179355", "typographic-t-shirts-176987", 
    #"straight-jeans-178148", 
    #"vacay-edit-179708", 
    #"jacketscoats-178156", "formal-pants-176999", "graphic-tees-173318",
    #"graphic-sweatshirts-177000", "typographic-sweatshirts-173295", 
    "jewellery-189440"
]

# Global Driver
driver = None

# ================= CART TRACKER (JSON) =================
def load_tracker():
    if not os.path.exists(CART_TRACKER_FILE): return []
    try:
        with open(CART_TRACKER_FILE, 'r') as f:
            return json.load(f)
    except: return []

def save_tracker_item(pid):
    items = load_tracker()
    items.append(str(pid))
    with open(CART_TRACKER_FILE, 'w') as f:
        json.dump(items, f, indent=2)

def remove_tracker_item():
    items = load_tracker()
    if items:
        items.pop()
        with open(CART_TRACKER_FILE, 'w') as f:
            json.dump(items, f, indent=2)

def clear_tracker_file():
    with open(CART_TRACKER_FILE, 'w') as f:
        json.dump([], f)

# ================= TELEGRAM FUNCTION =================
def send_order_update(message: str, disable_preview: bool = True):
    if not BOT_TOKEN.strip(): return
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": disable_preview
    }
    try:
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        print(f"⚠️ Telegram Error: {e}")

# ================= BROWSER CORE =================
def init_browser():
    global driver
    if driver is not None: return

    print(f"\n🚀 Launching Browser...")
    options = uc.ChromeOptions()
    options.add_argument(f"--user-data-dir={CHROME_PROFILE_PATH}")
    options.add_argument("--no-first-run")
    options.add_argument("--password-store=basic")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--headless=new")
    
    driver = uc.Chrome(options=options, version_main=145)
    driver.get("https://www.sheinindia.in/")
    time.sleep(5) 

def save_cookies():
    global driver
    if not driver: return
    try:
        cookies = driver.get_cookies()
        with open(COOKIES_FILE, 'w') as f:
            json.dump(cookies, f, indent=2)
        print("💾 Cookies Saved.")
    except Exception: pass

def load_cookies_if_exist():
    if not os.path.exists(COOKIES_FILE):
        print("🔓 Opening Login Page...")
        driver.get(URL_LOGIN_PAGE)
        input("🔴 Please Log in manually, then press ENTER here...")
        save_cookies()
    else:
        print(f"✅ Found {COOKIES_FILE}.")

def refresh_browser_and_update_sensor():
    global driver
    print("🔄 REFRESH TRIGGERED: Updating Sensor & Cookies...")
    driver.refresh()
    time.sleep(5) 
    driver.execute_script("window.scrollTo(0, 500);")
    time.sleep(1)
    save_cookies()
    print("✅ Refresh Complete.")

def browser_api_call(method: str, url: str, json_data: dict = None):
    global driver
    if not driver: init_browser()
    
    js_script = """
    var callback = arguments[arguments.length - 1];
    var url = arguments[0];
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
    
    const timeout = new Promise((_, reject) =>
        setTimeout(() => reject(new Error("Request timed out")), 10000)
    );

    Promise.race([fetch(url, options), timeout])
        .then(response => response.json().then(data => ({ status: response.status, body: data })))
        .then(result => callback(result))
        .catch(error => callback({ status: 500, error: error.toString() }));
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
        print("\n⚠️ Session Invalid.")
        input("🔴 Log in manually then press ENTER...")
        save_cookies()

# ================= CART LOGIC =================

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
             print("⚠️ Tracker empty but Server has items. Syncing...")
        else:
            return

    print(f"🧹 Clearing {count_needed} items...")

    for i in range(count_needed):
        del_res = browser_api_call("POST", URL_DELETE, {"entryNumber": 0})
        status = del_res.get("status", 500)
        
        if status in [403, 429, 500, 502, 503]:
             print(f"⛔ Error {status} during delete. Refreshing...")
             refresh_browser_and_update_sensor()
             del_res = browser_api_call("POST", URL_DELETE, {"entryNumber": 0})
             status = del_res.get("status", 500)
        
        if status == 200:
            print(f"   ✅ Deleted 1 Item.")
            remove_tracker_item()
        else:
            print(f"   ❌ Failed Delete (Status: {status})")
        
        time.sleep(0.2)
    
    clear_tracker_file()
    print("✨ Cart Cleaned.")

def add_product_bridge(cart_id: str, pid: str) -> bool:
    print(f"➕ Adding {pid}...")
    url = URL_ADD_FMT.format(cart_id=cart_id, product_id=pid)
    
    res = browser_api_call("POST", url, {"quantity": 1})
    status = res.get("status", 500)
    
    if status in [403, 429, 500, 502, 503]:
        print(f"🚨 CRITICAL ERROR {status}! Refreshing Sensor...")
        refresh_browser_and_update_sensor()
        print(f"🔄 Retrying add {pid}...")
        res = browser_api_call("POST", url, {"quantity": 1})
        status = res.get("status", 500)

    if status != 200:
        print(f"❌ Failed to add: {status}")
        return False

    save_tracker_item(pid)
    return True

def apply_voucher_bridge() -> bool:
    print(f"🎫 Testing Voucher: {VOUCHER_CODE}...")
    payload = {
        "voucherId": VOUCHER_CODE, 
        "device": {"client_type": "MSITE"}
    }
    
    res = browser_api_call("POST", URL_APPLY_VOUCHER, payload)
    status = res.get("status", 500)
    
    if status == 200:
        print("🎉 VOUCHER HIT! (Status 200)")
        return True
    
    print(f"❌ Voucher Status: {status}")
    return False

def fetch_products(curated_id: str) -> List[str]:
    print(f"🔍 Fetching {curated_id}...")
    product_ids = []
    headers = {"user-agent": "Mozilla/5.0", "x-tenant-id": "SHEIN"}
    url = (
        "https://search-edge.services.sheinindia.in/rilfnlwebservices/v4/rilfnl/products/category/83"
        f"?advfilter=true&curatedid={curated_id}&curated=true&pageSize=40&store=shein&fields=FULL&currentPage=0"
    )
    try:
        r = requests.get(url, headers=headers, timeout=15)
        for p in r.json().get("products", []):
            if p.get("code"): product_ids.append(str(p.get("code")))
    except: pass
    return product_ids




# ================= MAIN RUN LOOP =================
# ================= MAIN RUN LOOP =================
def run():
    global driver
    init_browser()
    ensure_login()
    
    print(f"📲 System Ready. Batch: {BATCH_SIZE} | Timer: {COOLDOWN_SECONDS}s")
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
                batch = all_products[i:i + BATCH_SIZE]
                print(f"📦 Batch {i//BATCH_SIZE + 1} ({len(batch)} items)")

                # 1️⃣ ADD BATCH
                count = 0
                for pid in batch:
                    if add_product_bridge(cart_id, pid):
                        count += 1
                    time.sleep(0.2)

                # 2️⃣ CHECK BATCH VOUCHER
                if count > 0 and apply_voucher_bridge():
                    print("🎯 VOUCHER HIT! Starting individual verification...")

                    for verify_pid in batch:
                        print(f"🧪 Testing individually: {verify_pid}")
                        clear_cart_bridge()
                        time.sleep(1)

                        if add_product_bridge(cart_id, verify_pid):
                            time.sleep(0.5)

                            if apply_voucher_bridge():
                                original_link = f"https://www.sheinindia.in/p/{verify_pid}"

                                send_order_update(
                                    f"🚨 <b>VOUCHER WORKED!</b>\n"
                                    f"Cat: {category}\n"
                                    f"🔗 <a href='{original_link}'>Buy Now</a>"
                                )

                                print(f"✅ Success: {verify_pid}")
                            else:
                                print(f"⏩ {verify_pid} individual fail.")

                # 3️⃣ FINAL CLEAR & WAIT
                clear_cart_bridge()
                print(f"🔄 Cycle Done. Waiting {COOLDOWN_SECONDS}s...")
                time.sleep(COOLDOWN_SECONDS)

        print("\n🔁 Restarting Main Loop...")
        time.sleep(5)

if __name__ == "__main__":
    run()

import sys
import re
import threading
import subprocess
import json
import urllib.parse
import time
from typing import List, Optional, Dict, Any, Tuple
from concurrent.futures import ThreadPoolExecutor, as_completed
import httpx

SHEIN_BASE = "https://www.sheinindia.in"
STOCK_API  = "https://www.sheinindia.in/api/cart/sizeVariants/{code}"

_HEADERS = {
    'accept':          'application/json, text/plain, */*',
    'accept-language': 'en-GB,en-US;q=0.9,en;q=0.8',
    'user-agent':      'Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Mobile Safari/537.36',
}

_executor = ThreadPoolExecutor(max_workers=256, thread_name_prefix='shein')

# ── Thread-local storage — httpx.Client is NOT thread-safe ──────────────────
_local = threading.local()

# ── Cookie state — version-tracked for hot-swap ──────────────────────────────
_stock_cookies: Dict[str, str] = {}
_stock_cookies_lock = threading.Lock()
_stock_version: int  = 0
# Raw cookie string kept for curl subprocess
_cookie_str_raw: str = ""


# ── Per-thread page client (HTTP/2, no cookies) ──────────────────────────────
def _get_page_client() -> httpx.Client:
    if getattr(_local, 'page_client', None) is None:
        _local.page_client = httpx.Client(
            http2=True,
            headers=_HEADERS,
            follow_redirects=True,
            timeout=httpx.Timeout(connect=4.0, read=6.0, write=4.0, pool=2.0),
            limits=httpx.Limits(max_connections=32, max_keepalive_connections=16,
                                keepalive_expiry=30),
        )
    return _local.page_client


# ── Per-thread page-with-cookies client (fallback on 403) ────────────────────
def _get_page_cookie_client() -> httpx.Client:
    client  = getattr(_local, 'page_cookie_client', None)
    version = getattr(_local, 'page_cookie_version', -1)
    if client is None or version != _stock_version:
        if client is not None:
            try: client.close()
            except Exception: pass
        with _stock_cookies_lock:
            cookies_copy = dict(_stock_cookies)
        _local.page_cookie_client  = httpx.Client(
            http2=True,
            headers=_HEADERS,
            cookies=cookies_copy,
            follow_redirects=True,
            timeout=httpx.Timeout(connect=4.0, read=6.0, write=4.0, pool=2.0),
            limits=httpx.Limits(max_connections=32, max_keepalive_connections=16,
                                keepalive_expiry=30),
        )
        _local.page_cookie_version = _stock_version
    return _local.page_cookie_client


def refresh_stock_cookies(cookie_str: str):
    """Hot-swap cookies. Bumps version → all thread-local clients recreate on next use."""
    global _stock_version, _cookie_str_raw
    with _stock_cookies_lock:
        _stock_cookies.clear()
        _cookie_str_raw = cookie_str or ""
        if cookie_str:
            for item in cookie_str.split(';'):
                item = item.strip()
                if '=' in item:
                    k, v = item.split('=', 1)
                    _stock_cookies[k.strip()] = v.strip()
        _stock_version += 1
        count   = len(_stock_cookies)
        version = _stock_version
    print(f"[SCRAPER] Cookies refreshed ({count} keys, v{version})", file=sys.stderr)


# ── 403 state ────────────────────────────────────────────────────────────────
_page_403_active:  bool = False
_stock_403_active: bool = False
_403_lock = threading.Lock()

def is_page_403()  -> bool: return _page_403_active
def is_stock_403() -> bool: return _stock_403_active

def _set_page_403(state: bool):
    global _page_403_active
    with _403_lock:
        _page_403_active = state

def _set_stock_403(state: bool):
    global _stock_403_active
    with _403_lock:
        _stock_403_active = state


# ── URL builder ───────────────────────────────────────────────────────────────
_url_cache: Dict[str, Tuple] = {}
_url_cache_lock = threading.Lock()

def _build_page_url(base_url: str, page: int) -> str:
    with _url_cache_lock:
        if base_url not in _url_cache:
            _url_cache[base_url] = _parse_url(base_url)
        cat, params = _url_cache[base_url]
    if not cat:
        return ""
    p = dict(params)
    p['currentPage'] = str(page)
    return f"{SHEIN_BASE}/api/category/{cat}?{urllib.parse.urlencode(p)}"


def _parse_url(url: str) -> Tuple[Optional[str], Optional[Dict]]:
    try:
        parsed     = urllib.parse.urlparse(url)
        path_parts = [x for x in parsed.path.strip('/').split('/') if x]

        if len(path_parts) >= 3 and path_parts[0] == 'api' and path_parts[1] == 'category':
            cat    = path_parts[2]
            qp     = urllib.parse.parse_qs(parsed.query, keep_blank_values=False)
            params = {k: v[0] for k, v in qp.items() if v and v[0].strip()}
            params.setdefault('fields',    'SITE')
            params.setdefault('format',    'json')
            params.setdefault('pageSize',  '60')
            params.setdefault('segmentIds','15,8,19')
            params.pop('currentPage', None)
            return cat, params

        cat = None
        for i, part in enumerate(path_parts):
            if part == 'c' and i + 1 < len(path_parts):
                cat = path_parts[i + 1]; break
        if not cat:
            for part in reversed(path_parts):
                if part not in ('api', 'category', 'sheinverse', 'shein'):
                    cat = part; break
        if not cat:
            return None, None

        qp      = urllib.parse.parse_qs(parsed.query)
        facets  = qp.get('facets', [''])[0]
        query_p = qp.get('query', [''])[0] or qp.get('q', [''])[0]
        params  = {
            'fields':                 qp.get('fields',                 ['SITE'])[0],
            'pageSize':               qp.get('pageSize',               ['60'])[0],
            'format':                 'json',
            'gridColumns':            qp.get('gridColumns',            ['2'])[0],
            'segmentIds':             qp.get('segmentIds',             ['15,8,19'])[0],
            'customerType':           'Existing',
            'customertype':           'Existing',
            'includeUnratedProducts': qp.get('includeUnratedProducts', ['false'])[0],
            'advfilter':              qp.get('advfilter',              ['true'])[0],
            'platform':               qp.get('platform',               ['Desktop'])[0],
            'showAdsOnNextPage':      qp.get('showAdsOnNextPage',      ['false'])[0],
            'is_ads_enable_plp':      qp.get('is_ads_enable_plp',      ['true'])[0],
            'displayRatings':         qp.get('displayRatings',         ['true'])[0],
            'store':                  qp.get('store',                  ['shein'])[0],
        }
        if facets:  params['facets'] = facets
        if query_p: params['query']  = query_p
        elif facets: params['query'] = f':relevance:{facets}'
        return cat, params
    except Exception:
        return None, None


# ── Product parser ────────────────────────────────────────────────────────────
def _parse_product(p: Dict[str, Any]) -> Optional[Dict]:
    code = p.get('code')
    if not code: return None
    code = str(code)

    fcd         = p.get('fnlColorVariantData') or {}
    color_group = fcd.get('colorGroup', '')
    if not color_group:
        m = re.search(r'/p/([^/?]+)', p.get('url', ''))
        color_group = m.group(1) if m else code

    image  = ""
    images = p.get('images') or []
    for fmt in ('productGrid3ListingImage', 'product'):
        for img in images:
            if isinstance(img, dict) and img.get('format') == fmt:
                image = img.get('url', ''); break
        if image: break
    if not image and images:
        first = images[0]
        image = first.get('url', '') if isinstance(first, dict) else ''
    if not image:
        image = fcd.get('outfitPictureURL', '')

    price_obj = p.get('price') or {}
    offer_obj = p.get('offerPrice') or {}
    mrp = (price_obj.get('displayformattedValue') or price_obj.get('formattedValue', '')
           or offer_obj.get('displayformattedValue') or offer_obj.get('formattedValue', ''))

    name = (p.get('name') or p.get('title') or fcd.get('colorName', '') or '').strip()

    return {'code': code, 'color_group': color_group, 'name': name, 'image': image, 'mrp': mrp}


def _parse_page_response(data: Any) -> Tuple[int, List[Dict]]:
    if not isinstance(data, dict): return 0, []
    pag   = data.get('pagination') or {}
    total = max(int(pag.get('totalPages', 0)), int(pag.get('numberOfPages', 0)), 0)
    prods = [r for r in (_parse_product(p) for p in data.get('products', [])) if r]
    return total, prods


# ── Page fetchers ─────────────────────────────────────────────────────────────
_err_logged: set = set()
_err_lock = threading.Lock()

def _fetch_page0(api_url: str) -> Tuple[int, List[Dict]]:
    try:
        t   = int(time.time() * 1000)
        sep = '&' if '?' in api_url else '?'
        url = f"{api_url}{sep}_t={t}"

        resp = _get_page_client().get(url)
        if resp.status_code == 403:
            resp = _get_page_cookie_client().get(url)

        if resp.status_code != 200:
            _set_page_403(True)
            key = f"http:{resp.status_code}"
            with _err_lock:
                if key not in _err_logged:
                    _err_logged.add(key)
                    print(f"[SCRAPER] HTTP {resp.status_code} — category page (update cookies)", file=sys.stderr)
            return 1, []

        _set_page_403(False)
        with _err_lock:
            _err_logged.discard("http:403")
        total, prods = _parse_page_response(resp.json())
        return max(total, 1), prods
    except Exception as e:
        key = f"exc:{type(e).__name__}"
        with _err_lock:
            if key not in _err_logged:
                _err_logged.add(key)
                print(f"[SCRAPER] Page fetch error: {e}", file=sys.stderr)
        return 1, []


def _fetch_page_n(api_url: str) -> List[Dict]:
    try:
        t   = int(time.time() * 1000)
        sep = '&' if '?' in api_url else '?'
        url = f"{api_url}{sep}_t={t}"
        resp = _get_page_client().get(url)
        if resp.status_code == 403:
            resp = _get_page_cookie_client().get(url)
        if resp.status_code != 200:
            return []
        _, prods = _parse_page_response(resp.json())
        return prods
    except Exception:
        return []


# ── Stock fetch via subprocess curl ──────────────────────────────────────────
def _parse_size_options(opts: list) -> List[Dict[str, Any]]:
    sizes = []
    for opt in opts:
        stock = (opt.get('stock') or {}).get('stockLevel', 0)
        label = ''
        for q in opt.get('variantOptionQualifiers', []):
            if q.get('qualifier') == 'size':
                label = q.get('value', ''); break
        if label:
            sizes.append({'size': label, 'stock': stock})
    return sizes


def fetch_stock(code: str) -> List[Dict[str, Any]]:
    """Fetch stock via subprocess curl — bypasses all Python HTTP client issues.
    Uses stored cookie string directly in curl -b flag.
    """
    url = STOCK_API.format(code=code)
    with _stock_cookies_lock:
        cookie_str = _cookie_str_raw

    cmd = [
        'curl', '-s', '--max-time', '5',
        '--http2',
        '-H', f'accept: application/json, text/plain, */*',
        '-H', f'accept-language: en-GB,en-US;q=0.9,en;q=0.8',
        '-H', f'user-agent: {_HEADERS["user-agent"]}',
        '-L',   # follow redirects
        '-w', '\n__STATUS__%{http_code}',
    ]
    if cookie_str:
        cmd += ['-b', cookie_str]
    cmd.append(url)

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=6,
        )
        raw = result.stdout
        # Extract HTTP status from our sentinel
        status_str = ''
        body       = raw
        if '\n__STATUS__' in raw:
            body, status_str = raw.rsplit('\n__STATUS__', 1)
        status = int(status_str.strip()) if status_str.strip().isdigit() else 0

        if status == 403:
            _set_stock_403(True)
            return []
        if status != 200 or not body.strip():
            return []

        _set_stock_403(False)
        data = json.loads(body)

        for base in data.get('baseOptions', []):
            if base.get('variantType') == 'FnlSizeVariant':
                sizes = _parse_size_options(base.get('options', []))
                if sizes:
                    return sizes
        return _parse_size_options(data.get('variantOptions', []))

    except subprocess.TimeoutExpired:
        return []
    except Exception:
        return []


def fetch_stock_batch(codes: List[str]) -> Dict[str, List[Dict]]:
    """All stock fetches fire simultaneously via curl subprocesses. 6s hard cap."""
    if not codes:
        return {}
    result: Dict[str, List[Dict]] = {}
    futs = {_executor.submit(fetch_stock, c): c for c in codes}
    try:
        for f in as_completed(futs, timeout=6):
            c = futs[f]
            try:
                result[c] = f.result(timeout=0)
            except Exception:
                result[c] = []
    except Exception:
        pass
    for c in codes:
        result.setdefault(c, [])
    return result


# ── Main scraper ──────────────────────────────────────────────────────────────
class SheinScraper:
    def fetch_all_products(self, url: str) -> List[Dict]:
        p0_url = _build_page_url(url, 0)
        if not p0_url:
            print(f"[SCRAPER] Bad URL: {url[:60]}", file=sys.stderr)
            return []

        total_pages, products = _fetch_page0(p0_url)
        if total_pages <= 1:
            return products

        futs = {}
        for n in range(1, total_pages):
            pn_url = _build_page_url(url, n)
            if pn_url:
                futs[_executor.submit(_fetch_page_n, pn_url)] = n

        for f in as_completed(futs, timeout=8):
            try:
                prods = f.result(timeout=0)
                if prods:
                    products.extend(prods)
            except Exception:
                pass

        if products:
            print(f"[SCRAPER] {total_pages}p → {len(products)} products", file=sys.stderr)
        return products


_scraper_instance: Optional[SheinScraper] = None

def get_scraper() -> SheinScraper:
    global _scraper_instance
    if _scraper_instance is None:
        _scraper_instance = SheinScraper()
    return _scraper_instance
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

        # Pass cookies to scraper so it can bypass 403 on product fetching
        try:
            cookie_str = "; ".join(
                f"{c['name']}={c['value']}" for c in cookies
                if c.get("name") and c.get("value")
            )
            refresh_stock_cookies(cookie_str)
            print(f"🍪 Scraper cookies updated ({len(cookies)} cookies)")
        except Exception as e:
            print(f"⚠️ Scraper cookie update error: {e}")
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
    Uses scraper.py (httpx + HTTP/2 + cookie fallback) — no browser needed.
    Passes cookies so it never hits 403.
    """
    print(f"\n🔍 Fetching category: {category_slug}…")

    # Build the full API URL (exactly what the site uses)
    api_url = (
        f"https://www.sheinindia.in/api/category/83"
        f"?fields=SITE"
        f"&currentPage=0"
        f"&pageSize=40"
        f"&format=json"
        f"&query=%3Arelevance%3Aundefined%3Anull"
        f"&facets=undefined%3Anull"
        f"&curated=true"
        f"&curatedid={category_slug}"
        f"&gridColumns=2"
        f"&includeUnratedProducts=false"
        f"&segmentIds=15%2C8%2C19"
        f"&customertype=Existing"
        f"&advfilter=true"
        f"&platform=Msite"
        f"&showAdsOnNextPage=false"
        f"&is_ads_enable_plp=true"
        f"&displayRatings=true"
        f"&segmentIds="
        f"&&store=shein"
    )

    products = get_scraper().fetch_all_products(api_url)
    product_ids = [p["code"] for p in products if p.get("code")]
    print(f"   ✅ Total fetched: {len(product_ids)} products")
    return product_ids


    data = res["body"]
    total_pages = data.get("pagination", {}).get("totalPages", 1)
    total_results = data.get("pagination", {}).get("totalResults", 0)
    print(f"   📊 Total: {total_results} products across {total_pages} pages")

    for p in data.get("products", []):
        if p.get("code"):
            product_ids.append(str(p["code"]))

    # Remaining pages
    for page in range(1, total_pages):
        res = browser_api_call("GET", build_url(page))
        status = res.get("status", 500)
        print(f"   📡 Page {page} — status: {status}")
        if status != 200 or not res.get("body"):
            print(f"   ⚠️ Stopping at page {page} — bad response")
            break
        for p in res["body"].get("products", []):
            if p.get("code"):
                product_ids.append(str(p["code"]))
        time.sleep(0.3)

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

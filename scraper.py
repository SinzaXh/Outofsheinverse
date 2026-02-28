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

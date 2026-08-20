"""
Flask 主程式 — 台股掃描器網站
"""
import sys
import os
import json
import time
import logging
import threading
import requests as http_requests
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

logger = logging.getLogger(__name__)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# ===== 載入 .env (若存在),不依賴外部套件 =====
def _load_dotenv():
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env')
    if not os.path.isfile(env_path):
        return
    try:
        with open(env_path, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#') or '=' not in line:
                    continue
                k, _, v = line.partition('=')
                k = k.strip()
                v = v.strip().strip('"').strip("'")
                if k and k not in os.environ:
                    os.environ[k] = v
    except Exception as e:
        logger.warning(f".env 載入失敗: {e}")

_load_dotenv()

from flask import Flask, render_template, request, jsonify, send_file, redirect, url_for
from flask_httpauth import HTTPBasicAuth
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from werkzeug.security import check_password_hash, generate_password_hash
from models.database import (init_db, get_conn, get_latest_date, get_breakouts_by_date,
                             get_trading_dates, get_broker_trades,
                             get_regime_history, get_latest_regime,
                             add_to_watchlist, remove_from_watchlist,
                             get_watchlist, is_in_watchlist)
from scanners.institutional import get_ranking
from scanners.futures_large_trader import get_stock_large_trader
try:
    from scanners.regime import get_market_temperature, rolling_retrain, get_model_info
except ImportError:
    get_market_temperature = None
    rolling_retrain = None
    get_model_info = None
from scrapers.market import fetch_futures_oi, fetch_retail_ratio, fetch_put_call_ratio, _finmind_get

app = Flask(__name__)

# ===== Basic Auth =====
auth = HTTPBasicAuth()

_SCANNER_USER = os.environ.get('SCANNER_USER', '')
_SCANNER_PASS = os.environ.get('SCANNER_PASS', '')


@auth.verify_password
def _verify_password(username, password):
    # 未設定帳密時視為未啟用,直接放行(避免本機開發誤鎖死)
    if not _SCANNER_USER or not _SCANNER_PASS:
        return 'guest'
    if username == _SCANNER_USER and password == _SCANNER_PASS:
        return username
    return None


# ===== Rate Limiter =====
limiter = Limiter(
    key_func=get_remote_address,
    app=app,
    default_limits=["120 per minute"],
)


# ===== 全站 Basic Auth: 排除 /static/* 與 /api/health =====
_AUTH_EXEMPT_PREFIXES = ('/static/',)
_AUTH_EXEMPT_PATHS = {'/api/health'}


@app.before_request
def _global_auth_guard():
    # 未設定帳密時不啟用 (本機開發友善)
    if not _SCANNER_USER or not _SCANNER_PASS:
        return None
    path = request.path or ''
    if path in _AUTH_EXEMPT_PATHS:
        return None
    for prefix in _AUTH_EXEMPT_PREFIXES:
        if path.startswith(prefix):
            return None
    # 利用 flask_httpauth 的 login_required 機制驗證 (回傳 None 表示通過)
    return auth.login_required(lambda: None)()


# 啟動時初始化 DB
init_db()


# ===== 全球行情 (Global Quotes) =====

GLOBAL_QUOTES_SYMBOLS = [
    ("^TWII",   "台股加權"),
    ("^DJI",    "道瓊"),
    ("^GSPC",   "S&P 500"),
    ("^N225",   "日經225"),
    ("^KS11",   "韓國KOSPI"),
    ("^GDAXI",  "德國DAX"),
    ("BTC-USD", "比特幣"),
    ("ETH-USD", "以太幣"),
    ("GC=F",    "黃金"),
    ("SI=F",    "白銀"),
]

_quotes_cache = {"data": [], "ts": 0}
_quotes_lock = threading.Lock()


def _fetch_single_quote(sym, label):
    """從 Yahoo Finance v8 chart API 抓取單一商品行情"""
    try:
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}?range=2d&interval=1d"
        r = http_requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
        if r.status_code == 200:
            result = r.json()["chart"]["result"][0]
            meta = result["meta"]
            price = meta.get("regularMarketPrice", 0)
            prev = meta.get("chartPreviousClose", 0) or meta.get("previousClose", 0)
            pct = ((price - prev) / prev * 100) if prev else 0
            return {
                "symbol": sym,
                "label": label,
                "price": round(price, 2),
                "pct": round(pct, 2),
            }
    except Exception:
        pass
    return None


def _fetch_global_quotes():
    """從 Yahoo Finance v8 chart API 並行抓取全球行情"""
    try:
        data = []
        with ThreadPoolExecutor(max_workers=10) as executor:
            futures = {
                executor.submit(_fetch_single_quote, sym, label): sym
                for sym, label in GLOBAL_QUOTES_SYMBOLS
            }
            for future in as_completed(futures):
                result = future.result()
                if result:
                    data.append(result)
        # Sort by original order
        order = {s[0]: i for i, s in enumerate(GLOBAL_QUOTES_SYMBOLS)}
        data.sort(key=lambda x: order.get(x["symbol"], 999))
        return data
    except Exception as e:
        logger.error(f"Yahoo Finance 全球行情抓取失敗: {e}")
        return []


def get_global_quotes():
    """取得全球行情（60 秒快取，double-check locking 防止競態）"""
    with _quotes_lock:
        now = time.time()
        if now - _quotes_cache["ts"] < 60 and _quotes_cache["data"]:
            return _quotes_cache["data"]
        # fetch inside lock to prevent concurrent duplicate requests
        data = _fetch_global_quotes()
        if data:
            _quotes_cache["data"] = data
            _quotes_cache["ts"] = time.time()
            return data
        # return stale cache on failure
        return _quotes_cache["data"]


# ===== 融資融券 (Margin Trading) =====

def fetch_margin_data(stock_id, days=20):
    """從 FinMind 抓取融資融券資料"""
    end = datetime.now()
    start = end - timedelta(days=days + 15)
    try:
        r = http_requests.get("https://api.finmindtrade.com/api/v4/data",
            params={
                "dataset": "TaiwanStockMarginPurchaseShortSale",
                "data_id": stock_id,
                "start_date": start.strftime("%Y-%m-%d"),
                "end_date": end.strftime("%Y-%m-%d"),
            },
            headers={"User-Agent": "Mozilla/5.0"}, timeout=20)
        d = r.json()
        if d.get("status") != 200 or not d.get("data"):
            return []
        result = []
        for row in d["data"]:
            bal = float(row.get("MarginPurchaseTodayBalance", 0))
            limit = float(row.get("MarginPurchaseLimit", 0))
            s_bal = float(row.get("ShortSaleTodayBalance", 0))
            result.append({
                "date": row["date"],
                "balance": bal,
                "limit": limit,
                "use_rate": round(bal / limit * 100, 2) if limit > 0 else 0,
                "short_bal": s_bal,
            })
        result.sort(key=lambda x: x["date"])
        return result[-days:]
    except Exception as e:
        logger.error(f"融資融券資料抓取失敗 ({stock_id}): {e}")
        return []


# Cached latest_date for context processor (avoid DB hit on every request)
_latest_date_cache = {'value': None, 'ts': 0}
_latest_date_lock = threading.Lock()


@app.context_processor
def inject_global():
    """每個頁面都注入今天日期和最後更新時間"""
    now = time.time()
    with _latest_date_lock:
        if now - _latest_date_cache['ts'] < 60 and _latest_date_cache['value'] is not None:
            latest = _latest_date_cache['value']
        else:
            try:
                conn = get_conn()
                try:
                    latest = get_latest_date(conn)
                    _latest_date_cache['value'] = latest
                    _latest_date_cache['ts'] = now
                finally:
                    conn.close()
            except Exception:
                latest = _latest_date_cache['value'] or '更新中'
    return {
        'today': datetime.now().strftime('%Y-%m-%d %H:%M'),
        'last_update': latest or '尚未更新',
    }


@app.errorhandler(404)
def page_not_found(e):
    return render_template('error.html', code=404, message='頁面不存在'), 404


@app.errorhandler(500)
def server_error(e):
    logger.error(f"500 錯誤: {e}")
    return render_template('error.html', code=500,
                           message='伺服器暫時無法處理請求，資料庫可能忙碌中，請稍後重試'), 500


import sqlite3

@app.errorhandler(sqlite3.OperationalError)
def db_error(e):
    logger.error(f"DB 錯誤: {e}")
    return render_template('error.html', code=503,
                           message='資料庫忙碌中（背景正在更新資料），請稍後重試'), 503


@app.route('/')
def index():
    return redirect_to_breakout()


# ===== 產業研究 =====
RESEARCH_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'research')

@app.route('/research')
def research_list():
    """列出所有研究報告，按分類資料夾分組"""
    import re as _re
    def _extract_title(fpath):
        """從 HTML <title> 抓取報告標題"""
        try:
            with open(fpath, 'r', encoding='utf-8', errors='ignore') as fh:
                head = fh.read(3000)
            m = _re.search(r'<title[^>]*>([^<]+)</title>', head)
            if m:
                t = m.group(1).strip()
                t = _re.sub(r'\s*\|\s*GiS.*$', '', t)
                if t:
                    return t
        except Exception:
            pass
        return None

    def _valid_ymd(y, mo, d):
        """檢查 (年,月,日) 是否為合理日期；過濾掉如 2026/27/28 這種年度簡寫"""
        return 1 <= int(mo) <= 12 and 1 <= int(d) <= 31

    def _extract_date(fpath):
        """報告日期：優先檔名 YYYYMMDD / YYYY-MM-DD，其次 HTML 內容"""
        fname = os.path.basename(fpath)
        for m in _re.finditer(r'(\d{4})(\d{2})(\d{2})', fname):
            if _valid_ymd(*m.groups()):
                return f'{m.group(1)}-{m.group(2)}-{m.group(3)}'
        for m in _re.finditer(r'(\d{4})-(\d{2})-(\d{2})', fname):
            if _valid_ymd(*m.groups()):
                return f'{m.group(1)}-{m.group(2)}-{m.group(3)}'
        try:
            with open(fpath, 'r', encoding='utf-8', errors='ignore') as fh:
                content = fh.read(15000)
            text = _re.sub(r'<[^>]+>', ' ', content)
            for m in _re.finditer(r'(\d{4})年(\d{1,2})月(\d{1,2})日', text):
                if _valid_ymd(*m.groups()):
                    return f'{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}'
            for m in _re.finditer(r'(\d{4})[-/](\d{1,2})[-/](\d{1,2})', text):
                if _valid_ymd(*m.groups()):
                    return f'{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}'
        except Exception:
            pass
        return None

    _RATING_WORDS = ('買進', '增持', '中立', '減碼', '偏多', '賣出', '增加持股')
    def _normalize_rating(raw):
        """把內文評等字樣正規化成標題用語"""
        raw = raw.strip()
        if raw.startswith(('增加持股', '增持')) or 'OW' in raw or 'Overweight' in raw.title():
            return '增持'
        if raw.startswith('買進') or 'Buy' in raw.title():
            return '買進'
        if raw.startswith('中立') or 'Neutral' in raw.title():
            return '中立'
        if raw.startswith(('減碼', '減持')) or 'Underweight' in raw.title():
            return '減碼'
        if raw.startswith('偏多'):
            return '偏多'
        if raw.startswith(('賣出',)) or 'Sell' in raw.title():
            return '賣出'
        return None

    def _apply_rating(fpath, title):
        """標題若尚未含評等，從內文自動抓出並接上（僅影響顯示，不改檔案）

        依序嘗試：①「評等：…」字樣 ②評等徽章 <span class="badge …">…</span>
        （研究員觀點報告的評等多以徽章呈現，且位置在 2 萬位元組附近，故讀取量放大）
        """
        if any(w in title for w in _RATING_WORDS):
            return title
        try:
            with open(fpath, 'r', encoding='utf-8', errors='ignore') as fh:
                content = fh.read(40000)
            rating = None
            m = _re.search(r'評等[：:]\s*([^<\n]{1,16})', content)
            if m:
                rating = _normalize_rating(m.group(1))
            if not rating:
                # 徽章：去除前導符號（▼◆▲ 等）後正規化
                b = _re.search(r'class="badge[^"]*"[^>]*>\s*([^<]{1,24})', content)
                if b:
                    raw = _re.sub(r'^[\s▼◆▲△▽●○\-—]+', '', b.group(1))
                    rating = _normalize_rating(raw)
            if rating:
                return f'{title} — {rating}'
        except Exception:
            pass
        return title

    def _extract_summary(fpath, max_len=80):
        """從 HTML 抓第一段有意義的文字當摘要"""
        try:
            with open(fpath, 'r', encoding='utf-8', errors='ignore') as fh:
                content = fh.read(10000)
            # 找 <p> 或 <div> 裡第一段有意義的文字
            for tag in ['p', 'h2', 'h3', 'li']:
                matches = _re.findall(rf'<{tag}[^>]*>([^<]+)</{tag}>', content)
                for m in matches:
                    text = m.strip()
                    # 跳過太短、純英文標題、或 boilerplate
                    if len(text) > 15 and text not in ('GiS', 'Report', 'Table of Contents'):
                        if len(text) > max_len:
                            text = text[:max_len] + '...'
                        return text
        except Exception:
            pass
        return ''

    categories = {}

    def _scan_folder(base_dir, category_name, path_prefix=''):
        """掃描資料夾內的 .html 報告，加入 categories"""
        reports = []
        if not os.path.isdir(base_dir):
            return
        for f in sorted(os.listdir(base_dir)):
            if f.endswith('.html'):
                fpath = os.path.join(base_dir, f)
                date_str = _extract_date(fpath)
                if not date_str:
                    mtime = os.path.getmtime(fpath)
                    date_str = datetime.fromtimestamp(mtime).strftime('%Y-%m-%d')
                title = _extract_title(fpath) or f.replace('.html', '').replace('-', ' ').replace('_', ' ')
                title = _apply_rating(fpath, title)
                summary = _extract_summary(fpath)
                rel_path = (path_prefix + '/' + f) if path_prefix else f
                reports.append({'filename': rel_path, 'title': title, 'category': category_name, 'date': date_str, 'summary': summary})
        if reports:
            categories.setdefault(category_name, []).extend(reports)

    if os.path.isdir(RESEARCH_DIR):
        for item in sorted(os.listdir(RESEARCH_DIR)):
            item_path = os.path.join(RESEARCH_DIR, item)
            if item == '_archive' and os.path.isdir(item_path):
                # _archive 下的子資料夾自動展開為分類
                for sub in sorted(os.listdir(item_path)):
                    sub_path = os.path.join(item_path, sub)
                    if os.path.isdir(sub_path):
                        _scan_folder(sub_path, sub, path_prefix='_archive/' + sub)
            elif os.path.isdir(item_path) and not item.startswith('_'):
                _scan_folder(item_path, item, path_prefix=item)
            elif item.endswith('.html'):
                fpath = os.path.join(RESEARCH_DIR, item)
                date_str = _extract_date(fpath)
                if not date_str:
                    mtime = os.path.getmtime(fpath)
                    date_str = datetime.fromtimestamp(mtime).strftime('%Y-%m-%d')
                title = _extract_title(fpath) or item.replace('.html', '').replace('-', ' ').replace('_', ' ')
                title = _apply_rating(fpath, title)
                summary = _extract_summary(fpath)
                categories.setdefault('其他', []).append({'filename': item, 'title': title, 'category': '', 'date': date_str, 'summary': summary})

        # 每個分類內部按日期排序
        for cat in categories:
            categories[cat].sort(key=lambda x: x['date'], reverse=True)

        # 分類之間按最新報告日期排序（最近更新的分類排最前面）
        categories = dict(sorted(
            categories.items(),
            key=lambda kv: kv[1][0]['date'] if kv[1] else '0000',
            reverse=True
        ))

    total_count = sum(len(v) for v in categories.values())
    # 全部 view：跨類別純日期排序（新的置頂）
    all_reports = [r for rs in categories.values() for r in rs]
    all_reports.sort(key=lambda x: x['date'], reverse=True)
    return render_template('research.html', categories=categories, total_count=total_count, all_reports=all_reports)

@app.route('/api/research/<path:filepath>')
def api_research(filepath):
    """動態載入研究報告內容"""
    import re
    # 安全檢查：只允許英數中文、底線、連字號、點、斜線
    if re.search(r'\.\.', filepath) or not re.match(r'^[\w\-\./\u4e00-\u9fff]+\.html$', filepath):
        return 'Invalid', 400
    full_path = os.path.join(RESEARCH_DIR, filepath)
    # \u8def\u5f91\u904d\u6b77\u9632\u8b77: realpath \u5f8c\u5fc5\u9808\u4f4d\u65bc RESEARCH_DIR \u4e4b\u5167
    resolved = os.path.realpath(full_path)
    research_root = os.path.realpath(RESEARCH_DIR)
    if not resolved.startswith(research_root + os.sep):
        return 'Forbidden', 403
    if not os.path.isfile(resolved):
        return 'Not found', 404
    with open(resolved, 'r', encoding='utf-8') as f:
        return f.read()


# ===== 每日券商報告 =====
BROKER_REPORTS_DIR = r'P:\2026年報告'
BROKER_RATING_CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'broker_ratings.json')
MAX_EXTRACT_PER_REQ = 80


def _broker_normalize_rating(raw):
    """把一段文字正規化成標準評等字串，抓不到回 None。先比對到先回傳。"""
    if not raw:
        return None
    t = raw.strip()
    tl = t.lower()
    # 買進
    if '買進' in t or 'buy' in tl or 'outperform' in tl or '優於大盤' in t:
        return '買進'
    # 增持
    if '增持' in t or '增加持股' in t or '加碼' in t or 'overweight' in tl or 'accumulate' in tl:
        return '增持'
    # 偏多
    if '偏多' in t:
        return '偏多'
    # 中立
    if '中立' in t or 'neutral' in tl or 'hold' in tl or '持有' in t or '同大盤' in t:
        return '中立'
    # 區間操作
    if '區間' in t:
        return '區間操作'
    # 減碼
    if '減碼' in t or '減持' in t or 'underweight' in tl or 'reduce' in tl or '劣於大盤' in t:
        return '減碼'
    # 賣出
    if '賣出' in t or 'sell' in tl:
        return '賣出'
    return None


def _extract_pdf_rating(fpath):
    """從 PDF 第 1 頁抽取投資評等，回傳標準評等字串或 ''。"""
    import re as _re
    try:
        import fitz
    except Exception:
        return ''
    try:
        doc = fitz.open(fpath)
    except Exception:
        return ''
    try:
        if doc.page_count < 1:
            return ''
        try:
            text = doc.load_page(0).get_text() or ''
        except Exception:
            return ''
    finally:
        try:
            doc.close()
        except Exception:
            pass
    text = text[:6000]
    # ① 先找有標籤的評等：標籤後方 0–14 字內的評等字樣（最可靠）
    m = _re.search(r'(投資評等|投資建議|評等|Rating|Recommendation)[\s：:．.\-]{0,4}(.{0,14})', text, _re.IGNORECASE)
    if m:
        rating = _broker_normalize_rating(m.group(2))
        if rating:
            return rating
    # ② 退而找內文前 1500 字是否直接出現評等字樣
    rating = _broker_normalize_rating(text[:1500])
    if rating:
        return rating
    return ''


@app.route('/broker-reports')
def broker_reports():
    """每日券商報告：一次只讀選定那一天的 PDF 清單"""
    import re as _re
    # pCloud 未掛載
    if not os.path.isdir(BROKER_REPORTS_DIR):
        return render_template('broker_reports.html', dates=[], reports=[],
                               selected_date=None,
                               message='pCloud 磁碟 (P:) 未掛載，無法讀取券商報告')

    # 掃描符合 ^\d{4}$ 的日期資料夾，計算各自 PDF 數
    dates = []
    for item in os.listdir(BROKER_REPORTS_DIR):
        item_path = os.path.join(BROKER_REPORTS_DIR, item)
        if _re.match(r'^\d{4}$', item) and os.path.isdir(item_path):
            count = 0
            for f in os.listdir(item_path):
                if f.lower().endswith('.pdf'):
                    count += 1
            dates.append({
                'code': item,
                'label': f'{item[:2]}/{item[2:]}',
                'count': count,
            })
    # 依 code 由大到小排序（最新在前）
    dates.sort(key=lambda d: d['code'], reverse=True)

    # 選定日期
    date_codes = [d['code'] for d in dates]
    selected = request.args.get('date')
    if selected not in date_codes:
        selected = dates[0]['code'] if dates else None

    # 讀取評等快取（不存在或壞掉 → 空 dict）
    rating_cache = {}
    try:
        with open(BROKER_RATING_CACHE, 'r', encoding='utf-8') as _cf:
            rating_cache = json.load(_cf)
        if not isinstance(rating_cache, dict):
            rating_cache = {}
    except Exception:
        rating_cache = {}
    cache_dirty = False
    extracted_this_req = 0
    deferred_count = 0

    reports = []
    count_all = count_stock = count_industry = 0
    if selected:
        day_dir = os.path.join(BROKER_REPORTS_DIR, selected)
        for f in os.listdir(day_dir):
            if not f.lower().endswith('.pdf'):
                continue
            base = f[:-4]  # 去掉 .pdf
            code = ''
            m = _re.match(r'^reports_(stock|industry)_reports_\d{4}_\d{4}_(.+)$', base)
            if m:
                kind, rest = m.group(1), m.group(2)
                if kind == 'stock':
                    rtype = '個股'
                    sm = _re.match(r'^(\d{2,6})(.+)$', rest)
                    if sm:
                        code, name = sm.group(1), sm.group(2)
                        title = f'{code} {name}'
                    else:
                        title = rest
                else:
                    rtype = '產業'
                    title = rest
            else:
                rtype = '其他'
                title = base

            # 評等：先查快取；未快取則抽取（每請求上限 MAX_EXTRACT_PER_REQ）
            cache_key = f'{selected}/{f}'
            if cache_key in rating_cache:
                rating = rating_cache[cache_key]
            elif extracted_this_req < MAX_EXTRACT_PER_REQ:
                rating = _extract_pdf_rating(os.path.join(day_dir, f))
                rating_cache[cache_key] = rating
                cache_dirty = True
                extracted_this_req += 1
            else:
                # 超過本請求抽取上限：先給空、不寫入快取，下次載入再補抽
                rating = ''
                deferred_count += 1

            reports.append({'filename': f, 'title': title, 'rtype': rtype, 'code': code, 'rating': rating})

        if deferred_count:
            logger.info(f"broker rating: {selected} 有 {deferred_count} 篇評等延後抽取")

        # 若本次有新增評等 → 寫回快取檔（寫失敗只 log 不讓頁面掛掉）
        if cache_dirty:
            try:
                with open(BROKER_RATING_CACHE, 'w', encoding='utf-8') as _cf:
                    json.dump(rating_cache, _cf, ensure_ascii=False)
            except Exception as _e:
                logger.warning(f"broker rating: 寫入快取失敗 {_e}")

        count_all = len(reports)
        count_stock = sum(1 for r in reports if r['rtype'] == '個股')
        count_industry = sum(1 for r in reports if r['rtype'] == '產業')
        # 產業在前、個股在後，同類型再依標題排序
        _order = {'產業': 0, '個股': 1, '其他': 2}
        reports.sort(key=lambda r: (_order.get(r['rtype'], 3), r['title']))

    return render_template('broker_reports.html', dates=dates, reports=reports,
                           selected_date=selected, count_all=count_all,
                           count_stock=count_stock, count_industry=count_industry,
                           message=None)


@app.route('/api/broker-report/<path:filepath>')
def api_broker_report(filepath):
    """動態載入券商報告 PDF（inline 內嵌預覽）"""
    # 安全檢查：拒絕 .. ；必須以 .pdf 結尾（不分大小寫）；
    # 字元不再限制，真正防護交給下方 realpath 包含檢查
    if '..' in filepath or not filepath.lower().endswith('.pdf'):
        return 'Invalid', 400
    full_path = os.path.join(BROKER_REPORTS_DIR, filepath)
    # 路徑遍歷防護：realpath 後必須位於 BROKER_REPORTS_DIR 之內
    resolved = os.path.realpath(full_path)
    broker_root = os.path.realpath(BROKER_REPORTS_DIR)
    if not resolved.startswith(broker_root + os.sep):
        return 'Forbidden', 403
    if not os.path.isfile(resolved):
        return 'Not found', 404
    return send_file(resolved, mimetype='application/pdf')


@app.route('/breakout')
def breakout():
    conn = get_conn()
    try:
        date = request.args.get('date') or get_latest_date(conn)
        market = request.args.get('market', 'all')
        filter_days = request.args.get('days', '')  # e.g. '5,10,20'

        if not date:
            return render_template('breakout.html', rows=[], date=None, market=market,
                                   filter_days=[], available_dates=[],
                                   message='尚無資料，請先執行 python run_daily.py 抓取資料')

        rows = get_breakouts_by_date(conn, date, market if market != 'all' else None)
        available_dates = get_trading_dates(conn, 30)

        # 篩選特定突破天數
        active_filters = [int(d) for d in filter_days.split(',') if d.isdigit()]
        if active_filters:
            filtered = []
            for r in rows:
                match = False
                for d in active_filters:
                    if r[f'break_{d}'] == 1:
                        match = True
                        break
                if match:
                    filtered.append(r)
            rows = filtered

        # 取得市場體溫（失敗不影響頁面）
        regime_info = None
        if get_market_temperature is not None:
            try:
                regime_info = get_market_temperature(lookback_days=5)
            except Exception:
                pass

        return render_template('breakout.html', rows=rows, date=date, market=market,
                               filter_days=active_filters, available_dates=available_dates,
                               message=None, regime_info=regime_info)
    finally:
        conn.close()


def _calc_regime_temperatures(errors):
    """temperature = percentile rank × 100(相對整段歷史窗的位置)。
    比線性 (error/tau)*50 公式更穩,不會卡 100°。
    回傳 list[float],對齊 errors 順序。"""
    if not errors:
        return []
    n = len(errors)
    return [round(sum(1 for x in errors if x <= e) / n * 100, 1) for e in errors]


@app.route('/regime')
def regime():
    model_info = get_model_info() if get_model_info else {}
    source = request.args.get('source', 'auto')  # auto / live / db

    # 優先從 DB 讀（快），DB 沒資料或指定 live 才即時計算
    if source != 'live':
        conn = get_conn()
        try:
            rows = get_regime_history(conn, limit=120)
            if rows:
                latest = rows[0]
                tau = latest['tau']
                current_error = latest['recon_error']
                regime_val = latest['regime']
                # ASC 順序的 errors,給 percentile rank
                errors_asc = [r['recon_error'] for r in reversed(rows)]
                temps_asc = _calc_regime_temperatures(errors_asc)
                temperature = temps_asc[-1] if temps_asc else 0.0
                history = [{'date': r['date'], 'error': r['recon_error'],
                            'regime': r['regime'], 'temperature': t}
                           for r, t in zip(reversed(rows), temps_asc)]
                return render_template('regime.html',
                                       temperature=temperature,
                                       current_error=current_error,
                                       tau=tau,
                                       regime=regime_val,
                                       latest_date=latest['date'],
                                       history=history,
                                       history_json=json.dumps(history),
                                       model_info=model_info,
                                       data_source='db')
        except Exception:
            pass
        finally:
            conn.close()

    # DB 沒資料，走即時計算
    try:
        result = get_market_temperature(lookback_days=120)
        # 統一用 percentile rank 重算 temperature(覆蓋 live 的線性公式)
        hist = result.get('history') or []
        errors_asc = [h.get('error', 0) for h in hist]
        temps_asc = _calc_regime_temperatures(errors_asc)
        for h, t in zip(hist, temps_asc):
            h['temperature'] = t
        latest_temp = temps_asc[-1] if temps_asc else result['temperature']
        return render_template('regime.html',
                               temperature=latest_temp,
                               current_error=result['current_error'],
                               tau=result['tau'],
                               regime=result['regime'],
                               latest_date=result['latest_date'],
                               history=hist,
                               history_json=json.dumps(hist),
                               model_info=model_info,
                               data_source='live')
    except Exception as e:
        logger.error(f"Regime error: {e}")
        return render_template('regime.html',
                               temperature=0, current_error=0, tau=0,
                               regime='unknown', latest_date=None,
                               history=[], history_json='[]',
                               model_info=model_info,
                               data_source='error',
                               error=str(e))


@app.route('/api/regime')
def api_regime():
    try:
        result = get_market_temperature(lookback_days=60)
        if get_model_info:
            result['model_info'] = get_model_info()
        return jsonify(result)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/regime/retrain', methods=['POST'])
def api_regime_retrain():
    if rolling_retrain is None:
        return jsonify({'error': 'Regime module not available'}), 500
    try:
        window = int(request.args.get('window_years', 2))
        result = rolling_retrain(window_years=window)
        return jsonify(result)
    except Exception as e:
        logger.error(f"Retrain error: {e}")
        return jsonify({'error': str(e)}), 500


# ===== 持股水位投票系統 =====

# Staleness-aware 滾動抓取：頁面載入時若指標超過 2 天沒更新就即時補抓
# 用 module-level lock + timestamp 節流，避免 1 小時內重複打 FRED/Yahoo
_macro_refresh_lock = threading.Lock()
_macro_last_refresh = 0.0
MACRO_STALE_DAYS = 2          # 指標超過 N 天沒更新視為 stale
MACRO_REFRESH_THROTTLE = 3600  # 同一 process 內至少間隔 N 秒才再抓一次

def _macro_is_stale(conn):
    row = conn.execute(
        "SELECT MAX(date) as d FROM macro_indicators "
        "WHERE indicator IN ('T10Y3M','CP_SPREAD','DOLLAR','COR3M','MOVE')"
    ).fetchone()
    if not row or not row['d']:
        return True
    try:
        latest = datetime.strptime(row['d'], '%Y-%m-%d').date()
    except Exception:
        return True
    return (datetime.now().date() - latest).days > MACRO_STALE_DAYS

def _credit_is_stale(conn):
    row = conn.execute(
        "SELECT MAX(date) as d FROM credit_spread_history"
    ).fetchone()
    if not row or not row['d']:
        return True
    try:
        latest = datetime.strptime(row['d'], '%Y-%m-%d').date()
    except Exception:
        return True
    return (datetime.now().date() - latest).days > MACRO_STALE_DAYS

def _rolling_refresh_macro(conn):
    """若 DB 過期且未被節流，即時抓 FRED + Yahoo 補齊。失敗不擋頁面。"""
    global _macro_last_refresh
    now = time.time()
    if now - _macro_last_refresh < MACRO_REFRESH_THROTTLE:
        return
    if not _macro_is_stale(conn) and not _credit_is_stale(conn):
        return
    if not _macro_refresh_lock.acquire(blocking=False):
        return  # 已有另一個請求在抓，直接放行
    try:
        _macro_last_refresh = now
        if _macro_is_stale(conn):
            logger.info("[rolling] macro indicators 過期，即時補抓...")
            try:
                from scanners.macro_indicators import update_macro_indicators
                update_macro_indicators(conn)
            except Exception as e:
                logger.warning(f"[rolling] macro 補抓失敗: {e}")
        if _credit_is_stale(conn):
            logger.info("[rolling] credit spread 過期，即時補抓...")
            try:
                from scanners.credit_spread import update_credit_spread_db
                update_credit_spread_db(conn)
            except Exception as e:
                logger.warning(f"[rolling] credit 補抓失敗: {e}")
    finally:
        _macro_refresh_lock.release()


@app.route('/position-vote')
def position_vote():
    from scanners.position_vote import compute_position_vote
    from scanners.indicator_correlation import run_correlation_analysis
    conn = get_conn()
    try:
        _rolling_refresh_macro(conn)
        data = compute_position_vote(conn)
        corr = run_correlation_analysis(conn)
        return render_template('position_vote.html', data=data, corr=corr)
    except Exception as e:
        logger.error(f"Position vote error: {e}")
        return render_template('position_vote.html', data=None, corr=None, error=str(e))
    finally:
        conn.close()


@app.route('/api/position-vote')
def api_position_vote():
    from scanners.position_vote import compute_position_vote
    conn = get_conn()
    try:
        _rolling_refresh_macro(conn)
        return jsonify(compute_position_vote(conn))
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()


# ===== 市場廣度指標 =====

@app.route('/breadth')
def breadth():
    from scanners.breadth import compute_breadth, compute_breadth_history
    conn = get_conn()
    try:
        current = compute_breadth(conn)
        if not current:
            return render_template('breadth.html', current=None, history_json='[]', error='無資料')
        history = compute_breadth_history(conn, limit=60)
        return render_template('breadth.html',
                               current=current,
                               history=history,
                               history_json=json.dumps(history))
    except Exception as e:
        logger.error(f"Breadth error: {e}")
        return render_template('breadth.html', current=None, history_json='[]', error=str(e))
    finally:
        conn.close()


@app.route('/api/breadth')
def api_breadth():
    from scanners.breadth import compute_breadth
    conn = get_conn()
    try:
        result = compute_breadth(conn)
        return jsonify(result) if result else jsonify({'error': '無資料'}), 404
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()


# ===== 期現價差（正/逆價差）掃描 =====

@app.route('/futures-basis')
def futures_basis():
    return render_template('futures_basis.html')


@app.route('/api/futures-basis')
def api_futures_basis():
    from scanners.futures_basis import compute_futures_basis
    try:
        result = compute_futures_basis()
        return jsonify(result)
    except Exception as e:
        logger.error(f"futures-basis error: {e}")
        return jsonify({'ok': False, 'error': str(e), 'rows': [],
                        'stats': {}, 'quote_status': {}}), 500


# ===== 電金強弱（電子/金融期相對強弱）=====

@app.route('/te-tf-strength')
def te_tf_strength_page():
    return render_template('te_tf_strength.html')


@app.route('/api/te-tf-strength')
def api_te_tf_strength():
    from scanners.te_tf_strength import build_response
    try:
        smooth = request.args.get('smooth', default=0, type=int)
        return jsonify(build_response(smooth=smooth))
    except Exception as e:
        logger.error(f"te-tf-strength error: {e}")
        return jsonify({'ok': False, 'error': str(e), 'now': None, 'series': [], 'quote_status': {}}), 500


# ===== 選擇權支撐壓力表（TXO OI/Max Pain）=====

@app.route('/option-sr')
def option_sr():
    return render_template('option_sr.html')


@app.route('/api/option-sr')
def api_option_sr():
    from scanners.option_sr import compute_option_sr
    try:
        date = request.args.get('date')
        contract = request.args.get('contract')
        result = compute_option_sr(date, contract)
        return jsonify(result)
    except Exception as e:
        logger.error(f"option-sr error: {e}")
        return jsonify({'ok': False, 'error': str(e), 'rows': [],
                        'available_dates': [], 'available_contracts': []}), 500


# ===== 信用利差紅綠燈 =====

CREDIT_SPREAD_THRESHOLD = 0.3
CREDIT_SPREAD_YELLOW_LOW = 0.28
CREDIT_SPREAD_YELLOW_HIGH = 0.32


def _live_credit_signal(v):
    if v is None:
        return ''
    if v < CREDIT_SPREAD_YELLOW_LOW:
        return 'GREEN'
    if v >= CREDIT_SPREAD_YELLOW_HIGH:
        return 'RED'
    return 'YELLOW'


@app.route('/credit-spread')
def credit_spread():
    from models.database import get_credit_spread_history
    conn = get_conn()
    try:
        raw_rows = get_credit_spread_history(conn, limit=500)
        rows = [
            {
                'date': r['date'],
                'hyg_shy_ratio': r['hyg_shy_ratio'],
                'indicator_value': r['indicator_value'],
                'signal': _live_credit_signal(r['indicator_value']),
                'spy_close': r['spy_close'],
                'trend5d': r['trend5d'],
            }
            for r in raw_rows
        ]

        if not rows:
            # DB empty - try live compute and seed
            try:
                from scanners.credit_spread import update_credit_spread_db
                update_credit_spread_db(conn)
                raw_rows = get_credit_spread_history(conn, limit=500)
                rows = [
                    {
                        'date': r['date'],
                        'hyg_shy_ratio': r['hyg_shy_ratio'],
                        'indicator_value': r['indicator_value'],
                        'signal': _live_credit_signal(r['indicator_value']),
                        'spy_close': r['spy_close'],
                        'trend5d': r['trend5d'],
                    }
                    for r in raw_rows
                ]
            except Exception as e:
                logger.warning(f"Credit spread live seed failed: {e}")
                return render_template('credit_spread.html',
                                       signal='N/A', indicator_value=0, percentile=0,
                                       days_in_signal=0, last_switch='', latest_date='',
                                       history=[], history_json='[]', backtest=None,
                                       threshold=CREDIT_SPREAD_THRESHOLD,
                                       error=f"DB empty. Run daily_check.py first. ({e})")

        # rows are DESC order, reverse for chart
        rows_asc = list(reversed(rows))

        latest = rows[0]
        signal = latest['signal']
        value = latest['indicator_value']
        latest_date = latest['date']

        # Days in current signal
        days = 0
        for r in rows:
            if r['signal'] == signal:
                days += 1
            else:
                break
        last_switch = rows[days - 1]['date'] if days < len(rows) else rows[-1]['date']

        # History for chart (include spy_close + trend)
        history = [{'date': r['date'], 'ratio': r['hyg_shy_ratio'],
                     'value': r['indicator_value'], 'signal': r['signal'],
                     'spy': r['spy_close'] if r['spy_close'] else 0,
                     'trend': r['trend5d'] if r['trend5d'] else 0}
                    for r in rows_asc]

        # Current trend direction
        latest_trend = rows[0]['trend5d'] if rows[0]['trend5d'] else 0

        # 5-day average of indicator value + its signal
        avg5d = sum(r['indicator_value'] for r in rows[:5]) / min(5, len(rows)) if rows else 0
        if avg5d < CREDIT_SPREAD_YELLOW_LOW:
            avg5d_signal = 'GREEN'
        elif avg5d >= CREDIT_SPREAD_YELLOW_HIGH:
            avg5d_signal = 'RED'
        else:
            avg5d_signal = 'YELLOW'

        # Backtest: compute from DB data (simple version)
        backtest = _compute_backtest_from_db(conn)

        # SPY CTA 訊號(由 cta_signal scanner 寫入 DB)
        cta_data = _build_cta_payload(conn)

        return render_template('credit_spread.html',
                               signal=signal,
                               indicator_value=value,
                               percentile=value,
                               days_in_signal=days,
                               last_switch=last_switch,
                               latest_date=latest_date,
                               history=history,
                               history_json=json.dumps(history),
                               backtest=backtest,
                               threshold=CREDIT_SPREAD_THRESHOLD,
                               yellow_low=CREDIT_SPREAD_YELLOW_LOW,
                               yellow_high=CREDIT_SPREAD_YELLOW_HIGH,
                               trend5d=latest_trend,
                               avg5d=avg5d,
                               avg5d_signal=avg5d_signal,
                               cta=cta_data)
    except Exception as e:
        logger.error(f"Credit spread error: {e}")
        import traceback
        traceback.print_exc()
        return render_template('credit_spread.html',
                               signal='N/A', indicator_value=0, percentile=0,
                               days_in_signal=0, last_switch='', latest_date='',
                               history=[], history_json='[]', backtest=None,
                               threshold=CREDIT_SPREAD_THRESHOLD,
                               error=str(e))
    finally:
        conn.close()


def _build_cta_payload(conn):
    """從 cta_signal_history 讀整段歷史,算回測 + 勝率,塞給 template。
    DB 沒資料就回 None,template 會略過 CTA 區塊。"""
    from models.database import get_cta_signal_all
    try:
        rows = get_cta_signal_all(conn)
    except Exception:
        return None
    if not rows or len(rows) < 200:
        return None

    import pandas as pd
    df = pd.DataFrame([dict(r) for r in rows])
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values("date").set_index("date")

    # 用 scanners.cta_signal 的回測邏輯(避免重複)
    from scanners.cta_signal import compute_backtest, compute_trades
    bt = compute_backtest(df)
    tr = compute_trades(df)

    # 整段時間序列(供 chart 疊圖)
    history = []
    for dt, row in df.iterrows():
        history.append({
            "date": dt.strftime("%Y-%m-%d"),
            "spy": float(row["close"]),
            "signal": float(row["signal_raw"]),
            "pos": int(row["raw_pos"]) if pd.notna(row["raw_pos"]) else 0,
        })

    # 最新狀態
    last = df.iloc[-1]
    pos = int(last["raw_pos"]) if pd.notna(last["raw_pos"]) else 0
    action = "BUY" if pos > 0 else ("SELL" if pos < 0 else "HOLD")

    return {
        "action": action,
        "signal": float(last["signal_raw"]),
        "close": float(last["close"]),
        "date": str(df.index[-1].date()),
        "history": history,
        "history_json": json.dumps(history),
        "backtest": bt,
        "trades": tr,
    }


def _compute_backtest_from_db(conn):
    """Quick backtest from DB data."""
    import numpy as np
    import pandas as pd
    rows = conn.execute("""
        SELECT cs.date, cs.signal, cs.indicator_value
        FROM credit_spread_history cs
        ORDER BY cs.date ASC
    """).fetchall()
    if len(rows) < 252:
        return None

    try:
        import yfinance as yf
        spy = yf.download('SPY', start=rows[0]['date'], auto_adjust=True, progress=False)
        if isinstance(spy.columns, pd.MultiIndex):
            spy_close = spy['Close']['SPY']
        else:
            spy_close = spy['Close']
        spy_close.index = spy_close.index.tz_localize(None) if spy_close.index.tz else spy_close.index

        # Build signal series
        sig_df = pd.DataFrame(rows)
        sig_df['date'] = pd.to_datetime(sig_df['date'])
        sig_df = sig_df.set_index('date')

        # Align
        common = spy_close.index.intersection(sig_df.index)
        if len(common) < 100:
            return None

        spy_ret = spy_close.pct_change()
        position = (sig_df.loc[common, 'signal'] == 'GREEN').astype(float)
        tc = 0.5 * 0.01 * 0.01 * position.diff().abs().fillna(0)
        strat_ret = (spy_ret.loc[common] * position - tc).fillna(0)
        strat_eq = (1 + strat_ret).cumprod()
        bh_ret = spy_ret.loc[common].fillna(0)
        bh_eq = (1 + bh_ret).cumprod()

        n_yr = len(strat_ret) / 252
        s_tot = strat_eq.iloc[-1] / strat_eq.iloc[0] - 1
        b_tot = bh_eq.iloc[-1] / bh_eq.iloc[0] - 1

        class BT:
            pass
        bt = BT()
        bt.cagr = (1 + s_tot) ** (1 / n_yr) - 1
        bt.bh_cagr = (1 + b_tot) ** (1 / n_yr) - 1
        bt.vol = strat_ret.std() * np.sqrt(252)
        bt.bh_vol = bh_ret.std() * np.sqrt(252)
        bt.sharpe = strat_ret.mean() * np.sqrt(252) / strat_ret.std() if strat_ret.std() > 0 else 0
        bt.bh_sharpe = bh_ret.mean() * np.sqrt(252) / bh_ret.std() if bh_ret.std() > 0 else 0
        bt.maxdd = float((strat_eq / strat_eq.cummax() - 1).min())
        bt.bh_maxdd = float((bh_eq / bh_eq.cummax() - 1).min())
        bt.calmar = bt.cagr / abs(bt.maxdd) if bt.maxdd != 0 else 0
        bt.bh_calmar = bt.bh_cagr / abs(bt.bh_maxdd) if bt.bh_maxdd != 0 else 0
        bt.tim = float(position.mean())
        return bt
    except Exception as e:
        logger.warning(f"Backtest compute failed: {e}")
        return None


@app.route('/api/credit-spread')
def api_credit_spread():
    from models.database import get_credit_spread_history
    conn = get_conn()
    try:
        rows = get_credit_spread_history(conn, limit=1)
        if not rows:
            return jsonify({'error': 'No data. Run daily_check.py first.'}), 404
        latest = rows[0]
        return jsonify({
            'signal': _live_credit_signal(latest['indicator_value']),
            'indicator_value': latest['indicator_value'],
            'percentile': latest['indicator_value'],
            'hyg_shy_ratio': latest['hyg_shy_ratio'],
            'date': latest['date'],
            'threshold': CREDIT_SPREAD_THRESHOLD,
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()


@app.route('/institutional')
def institutional():
    conn = get_conn()
    try:
        inst_type = request.args.get('type', 'foreign')
        days = int(request.args.get('days', '1'))
        market = request.args.get('market', 'all')
        date = request.args.get('date')
        if not date:
            row = conn.execute("SELECT MAX(date) as d FROM institutional").fetchone()
            date = row['d'] if row else None

        if not date:
            return render_template('institutional.html', buy_rows=[], sell_rows=[],
                                   date=None, inst_type=inst_type, days=days, market=market,
                                   available_dates=[],
                                   message='尚無資料，請先執行 python run_daily.py 抓取資料')

        buy_rows, sell_rows = get_ranking(conn, inst_type, days, date,
                                           market if market != 'all' else None)
        available_dates = get_trading_dates(conn, 30)

        return render_template('institutional.html',
                               buy_rows=buy_rows, sell_rows=sell_rows,
                               date=date, inst_type=inst_type, days=days, market=market,
                               available_dates=available_dates, message=None)
    finally:
        conn.close()


@app.route('/consecutive')
def consecutive():
    conn = get_conn()
    try:
        date = get_latest_date(conn)
        inst_type = request.args.get('type', 'foreign')  # foreign, sitc, dealer
        min_days = int(request.args.get('days', '3'))
        direction = request.args.get('dir', 'buy')  # buy or sell

        col_map = {'foreign': 'foreign_buy', 'sitc': 'sitc_buy', 'dealer': 'dealer_buy'}
        if inst_type not in col_map:
            inst_type = 'foreign'
        col = col_map[inst_type]

        if not date:
            return render_template('consecutive.html', results=[], date=date,
                                   inst_type=inst_type, min_days=min_days, direction=direction)

        # Get the last 20 trading dates
        dates = conn.execute(
            "SELECT DISTINCT date FROM institutional ORDER BY date DESC LIMIT 20"
        ).fetchall()
        date_list = [d['date'] for d in dates]

        if len(date_list) < min_days:
            return render_template('consecutive.html', results=[], date=date,
                                   inst_type=inst_type, min_days=min_days, direction=direction)

        # For each stock, check consecutive days
        # Get all institutional data for recent dates
        placeholders = ','.join(['?'] * len(date_list))
        rows = conn.execute(f"""
            SELECT stock_id, date, {col} as net_buy
            FROM institutional
            WHERE date IN ({placeholders})
            ORDER BY stock_id, date DESC
        """, date_list).fetchall()

        # Group by stock
        from collections import defaultdict
        stock_data = defaultdict(list)
        for r in rows:
            stock_data[r['stock_id']].append({
                'date': r['date'],
                'net_buy': r['net_buy']
            })

        # Count consecutive days
        results = []
        for stock_id, days_data in stock_data.items():
            # days_data is sorted by date DESC
            count = 0
            total = 0
            for d in days_data:
                if direction == 'buy' and d['net_buy'] > 0:
                    count += 1
                    total += d['net_buy']
                elif direction == 'sell' and d['net_buy'] < 0:
                    count += 1
                    total += d['net_buy']
                else:
                    break
            if count >= min_days:
                results.append({
                    'stock_id': stock_id,
                    'consecutive_days': count,
                    'total_volume': total
                })

        # Sort by consecutive days desc, then total volume
        results.sort(key=lambda x: (-x['consecutive_days'], -abs(x['total_volume'])))

        # Enrich with stock info and latest price
        enriched = []
        for r in results[:100]:
            info = conn.execute("""
                SELECT s.name, s.market, dp.close_price, dp.change_pct, dp.volume
                FROM stocks s
                LEFT JOIN daily_prices dp ON dp.stock_id = s.stock_id AND dp.date = ?
                WHERE s.stock_id = ?
            """, (date, r['stock_id'])).fetchone()
            if info:
                enriched.append({**r, **dict(info)})

        return render_template('consecutive.html', results=enriched, date=date,
                               inst_type=inst_type, min_days=min_days, direction=direction)
    finally:
        conn.close()


@app.route('/deleveraging')
def deleveraging():
    """台股去槓桿壓力儀表板 — 優先用即時管線,失敗則回退 2026-07-14 靜態快照"""
    ind = None
    # Phase B: 即時管線 (scanners/deleveraging.py),尚未建置時自動回退快照
    try:
        from scanners.deleveraging import build_indicators
        ind = build_indicators()
    except Exception as e:
        logger.warning(f'deleveraging 即時管線失敗,回退快照: {e}')
    if not ind:
        snap = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            'data', 'deleveraging_snapshot.json')
        try:
            with open(snap, encoding='utf-8') as f:
                ind = json.load(f)
        except Exception as e:
            logger.error(f'deleveraging 快照載入失敗: {e}')
            ind = None
    return render_template('deleveraging.html',
                           ind_json=json.dumps(ind, ensure_ascii=False) if ind else 'null')


@app.route('/margin-warning')
def margin_warning():
    """融資預警訊號白話報告 — 靜態教育型內容"""
    return render_template('margin_warning.html')


@app.route('/margin-alert')
def margin_alert():
    # 已由「融資維持率查詢」取代；保留舊 URL 可用，直接導向新頁。
    return redirect(url_for('margin_maintenance'))


def _margin_alert_legacy():
    """（保留備用）舊融資使用率警示頁邏輯，現已不由 route 直接呈現。"""
    conn = get_conn()
    try:
        date = get_latest_date(conn)
        if not date:
            return render_template('margin_alert.html', results=[], date=None,
                                   message='尚無資料，請先執行 python run_daily.py 抓取資料')

        # Fetch per-stock margin data from TWSE
        results = _fetch_margin_stocks(date)
        sort_by = request.args.get('sort', 'use_rate')

        if sort_by == 'margin_change':
            results.sort(key=lambda x: -x.get('margin_change', 0))
        elif sort_by == 'short_balance':
            results.sort(key=lambda x: -x.get('short_balance', 0))
        else:
            results.sort(key=lambda x: -x.get('use_rate', 0))

        return render_template('margin_alert.html', results=results[:100], date=date,
                               sort_by=sort_by, message=None)
    finally:
        conn.close()


@app.route('/margin-maintenance')
def margin_maintenance():
    return render_template('margin_maintenance.html')


@app.route('/api/margin-maintenance')
def api_margin_maintenance():
    from scanners.margin_maintenance import (get_stock_maintenance, CodeError,
                                             MarketNotFoundError, DEFAULT_N)
    code = request.args.get('code', '').strip()
    try:
        n = int(request.args.get('n', DEFAULT_N))
    except (TypeError, ValueError):
        n = DEFAULT_N
    try:
        return jsonify(get_stock_maintenance(code, n))
    except CodeError as e:
        return jsonify({'error': 'invalid_code', 'message': str(e)}), 422
    except MarketNotFoundError as e:
        return jsonify({'error': 'not_found', 'message': str(e)}), 422
    except Exception as e:
        logger.error(f"margin-maintenance error: {e}")
        return jsonify({'error': 'internal', 'message': str(e)}), 500


@app.route('/api/margin-maintenance/scan')
def api_margin_maintenance_scan():
    from scanners.margin_maintenance import scan_market, DEFAULT_N
    try:
        n = int(request.args.get('n', DEFAULT_N))
    except (TypeError, ValueError):
        n = DEFAULT_N
    try:
        return jsonify(scan_market(n))
    except Exception as e:
        logger.error(f"margin-maintenance scan error: {e}")
        return jsonify({'error': 'internal', 'message': str(e), 'rows': []}), 500


def _fetch_margin_stocks(date_str):
    """從 TWSE 抓取個股融資融券資料"""
    cached = _get_report_cache('margin_stocks_' + date_str)
    if cached is not None:
        return cached

    try:
        yyyymmdd = date_str.replace('-', '')
        url = f'https://www.twse.com.tw/exchangeReport/MI_MARGN?response=json&date={yyyymmdd}&selectType=ALL'
        r = http_requests.get(url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=20)
        d = r.json()
        if d.get('stat') != 'OK' or not d.get('data'):
            return []

        conn = get_conn()
        try:
            results = []
            for row in d['data']:
                try:
                    stock_id = str(row[0]).strip()
                    if not stock_id or not stock_id[0].isdigit():
                        continue
                    name_raw = str(row[1]).strip()

                    margin_buy = int(str(row[2]).replace(',', '') or '0')
                    margin_sell = int(str(row[3]).replace(',', '') or '0')
                    margin_cash = int(str(row[4]).replace(',', '') or '0')
                    margin_balance_prev = int(str(row[5]).replace(',', '') or '0')
                    margin_balance = int(str(row[6]).replace(',', '') or '0')
                    margin_limit = int(str(row[7]).replace(',', '') or '0')

                    short_sell = int(str(row[8]).replace(',', '') or '0')
                    short_return = int(str(row[9]).replace(',', '') or '0')
                    short_balance_prev = int(str(row[10]).replace(',', '') or '0')
                    short_balance = int(str(row[11]).replace(',', '') or '0')

                    use_rate = round(margin_balance / margin_limit * 100, 2) if margin_limit > 0 else 0
                    margin_change = margin_balance - margin_balance_prev

                    # Lookup stock name/price from DB
                    info = conn.execute("""
                        SELECT s.name, dp.close_price, dp.change_pct
                        FROM stocks s
                        LEFT JOIN daily_prices dp ON dp.stock_id = s.stock_id AND dp.date = ?
                        WHERE s.stock_id = ?
                    """, (date_str, stock_id)).fetchone()

                    stock_name = info['name'] if info else name_raw
                    close_price = info['close_price'] if info else None
                    change_pct = info['change_pct'] if info else None

                    results.append({
                        'stock_id': stock_id,
                        'name': stock_name,
                        'close_price': close_price,
                        'change_pct': change_pct,
                        'margin_balance': margin_balance,
                        'margin_change': margin_change,
                        'use_rate': use_rate,
                        'margin_limit': margin_limit,
                        'short_balance': short_balance,
                        'short_change': short_balance - short_balance_prev,
                    })
                except (ValueError, IndexError):
                    continue

            _set_report_cache('margin_stocks_' + date_str, results)
            return results
        finally:
            conn.close()
    except Exception as e:
        logger.error(f"個股融資融券資料抓取失敗: {e}")
        return []


@app.route('/broker')
def broker():
    conn = get_conn()
    try:
        stock_id = request.args.get('stock', '').strip()
        # 只顯示有分點資料的日期
        available_dates = [r['date'] for r in conn.execute(
            "SELECT DISTINCT date FROM broker_trades ORDER BY date DESC LIMIT 30"
        ).fetchall()]
        date = request.args.get('date') or (available_dates[0] if available_dates else None)

        if not date:
            return render_template('broker.html', buy_rows=[], sell_rows=[],
                                   stock_id=stock_id, date=None,
                                   available_dates=[],
                                   message='尚無資料，請先執行 python run_daily.py 抓取資料')

        buy_rows = []
        sell_rows = []
        stock_name = ''
        if stock_id:
            buy_rows, sell_rows = get_broker_trades(conn, stock_id, date)
            # 取得股票名稱
            row = conn.execute("SELECT name FROM stocks WHERE stock_id = ?", (stock_id,)).fetchone()
            stock_name = row['name'] if row else ''

        return render_template('broker.html',
                               buy_rows=buy_rows, sell_rows=sell_rows,
                               stock_id=stock_id, stock_name=stock_name,
                               date=date, available_dates=available_dates,
                               message=None)
    finally:
        conn.close()


def fetch_margin_trading_summary(date_str):
    """從 TWSE 抓取信用交易增減（融資融券彙總）"""
    try:
        yyyymmdd = date_str.replace('-', '')
        url = f'https://www.twse.com.tw/exchangeReport/MI_MARGN?response=json&date={yyyymmdd}&selectType=MS'
        r = http_requests.get(url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=15)
        d = r.json()
        if d.get('stat') != 'OK' or not d.get('tables'):
            return None
        # tables[0] is margin purchase/short sale summary
        table = d['tables'][0]
        rows = table.get('data', [])
        result = {}
        for row in rows:
            item = row[0].strip() if row else ''
            if '融資' in item and '融券' not in item:
                # 融資(張): [項目, 買進, 賣出, 現金償還, 前日餘額, 今日餘額, ...]
                try:
                    prev_bal = int(str(row[4]).replace(',', ''))
                    today_bal = int(str(row[5]).replace(',', ''))
                    result['margin_buy_prev'] = prev_bal
                    result['margin_buy_today'] = today_bal
                    result['margin_buy_change'] = today_bal - prev_bal
                except (ValueError, IndexError):
                    pass
            elif '融券' in item:
                try:
                    prev_bal = int(str(row[4]).replace(',', ''))
                    today_bal = int(str(row[5]).replace(',', ''))
                    result['short_sell_prev'] = prev_bal
                    result['short_sell_today'] = today_bal
                    result['short_sell_change'] = today_bal - prev_bal
                except (ValueError, IndexError):
                    pass

        # Also try tables for 融資金額
        if len(d['tables']) > 1:
            table2 = d['tables'][1]
            rows2 = table2.get('data', [])
            for row in rows2:
                item = row[0].strip() if row else ''
                if '融資' in item and '融券' not in item:
                    try:
                        prev_bal = int(str(row[4]).replace(',', ''))
                        today_bal = int(str(row[5]).replace(',', ''))
                        # 金額單位：仟元 -> 億
                        result['margin_amount_prev'] = prev_bal
                        result['margin_amount_today'] = today_bal
                        result['margin_amount_change'] = today_bal - prev_bal
                    except (ValueError, IndexError):
                        pass
        return result if result else None
    except Exception as e:
        logger.error(f"信用交易資料抓取失敗: {e}")
        return None


def fetch_institutional_detail(date_str):
    """從 TWSE 抓取三大法人買進/賣出/淨額明細"""
    try:
        yyyymmdd = date_str.replace('-', '')
        url = f'https://www.twse.com.tw/fund/BFI82U?response=json&dayDate={yyyymmdd}&type=day'
        r = http_requests.get(url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=15)
        d = r.json()
        if d.get('stat') != 'OK' or not d.get('data'):
            return None
        result = []
        for row in d['data']:
            name = row[0].strip()
            try:
                buy = int(str(row[1]).replace(',', ''))
                sell = int(str(row[2]).replace(',', ''))
                net = int(str(row[3]).replace(',', ''))
            except (ValueError, IndexError):
                buy, sell, net = 0, 0, 0
            result.append({'name': name, 'buy': buy, 'sell': sell, 'net': net})
        return result
    except Exception as e:
        logger.error(f"三大法人買賣超明細抓取失敗: {e}")
        return None


def fetch_institutional_detail_prev(date_str):
    """嘗試抓前一個交易日的三大法人資料（用日期往回推最多7天）"""
    from datetime import datetime as dt
    base = dt.strptime(date_str, '%Y-%m-%d')
    for i in range(1, 8):
        prev = base - timedelta(days=i)
        prev_str = prev.strftime('%Y-%m-%d')
        data = fetch_institutional_detail(prev_str)
        if data:
            return data
    return None


def fetch_night_session_spread():
    """從 FinMind 抓取外資夜盤台指期資料，計算夜盤價差"""
    try:
        end = datetime.now()
        start = end - timedelta(days=10)
        rows = _finmind_get('TaiwanFuturesDaily', 'TX',
                            start.strftime('%Y-%m-%d'), end.strftime('%Y-%m-%d'))
        if not rows:
            return None

        # Find the latest after_market (night session) and day session
        # Only use near-month contracts (no spread contracts like 202604/202605)
        day_sessions = {}
        night_sessions = {}
        for row in rows:
            d = row['date']
            contract = str(row.get('contract_date', ''))
            # Skip spread contracts (contain '/')
            if '/' in contract:
                continue
            session = row.get('trading_session', '')
            close = float(row.get('close', 0) or 0)
            volume = int(row.get('volume', 0) or 0)
            if close <= 0:
                continue
            # Pick the contract with highest volume (near-month)
            if session == 'after_market':
                if d not in night_sessions or volume > night_sessions[d][1]:
                    night_sessions[d] = (close, volume)
            elif session in ('position', ''):
                if d not in day_sessions or volume > day_sessions[d][1]:
                    day_sessions[d] = (close, volume)

        if not night_sessions:
            return None

        # Get latest night session date
        latest_night_date = max(night_sessions.keys())
        night_close = night_sessions[latest_night_date][0]

        # Day session close (same date or most recent before)
        day_entry = day_sessions.get(latest_night_date)
        if not day_entry:
            for d in sorted(day_sessions.keys(), reverse=True):
                if d <= latest_night_date:
                    day_entry = day_sessions[d]
                    break

        if not day_entry:
            return None

        day_close = day_entry[0]
        spread = night_close - day_close
        pct = (spread / day_close * 100) if day_close else 0

        return {
            'date': latest_night_date,
            'day_close': day_close,
            'night_close': night_close,
            'spread': spread,
            'pct': round(pct, 2),
        }
    except Exception as e:
        logger.error(f"外資夜盤資料抓取失敗: {e}")
        return None


# ===== Report Cache =====
_report_cache = {}
_report_cache_lock = threading.Lock()
_REPORT_CACHE_TTL = 300  # 5 minutes


def _get_report_cache(key):
    """Get cached value if not expired."""
    with _report_cache_lock:
        entry = _report_cache.get(key)
        if entry and (time.time() - entry['ts']) < _REPORT_CACHE_TTL:
            return entry['data']
    return None


def _set_report_cache(key, data):
    """Set cache value with current timestamp."""
    with _report_cache_lock:
        _report_cache[key] = {'data': data, 'ts': time.time()}


def _fetch_tsm_adr():
    """Fetch TSM ADR quote from Yahoo Finance."""
    try:
        r = http_requests.get(
            'https://query1.finance.yahoo.com/v8/finance/chart/TSM',
            params={'interval': '1d', 'range': '2d'},
            headers={'User-Agent': 'Mozilla/5.0'}, timeout=8)
        d = r.json()
        res = d.get('chart', {}).get('result', [])
        if res:
            meta = res[0].get('meta', {})
            price = meta.get('regularMarketPrice', 0)
            prev = meta.get('chartPreviousClose', 0) or meta.get('previousClose', 0)
            chg = price - prev if prev else 0
            pct = (chg / prev * 100) if prev else 0
            return {'price': round(price, 2), 'change': round(chg, 2), 'pct': round(pct, 2)}
    except Exception as e:
        logger.warning(f"TSM ADR 報價抓取失敗: {e}")
    return None


@app.route('/report')
def report():
    conn = get_conn()
    try:
        latest = get_latest_date(conn)
        if not latest:
            return render_template('report.html', date=None,
                                   quotes_json='[]', twse_inst=None, tpex_inst=None,
                                   foreign_trend_json='[]', sitc_trend_json='[]',
                                   futures_json='[]', pc_json='[]',
                                   inst_detail_json='[]', inst_detail_prev_json='[]',
                                   margin_summary=None, night_session=None,
                                   message='尚無資料，請先執行 python run_daily.py 抓取資料')

        # DB queries (fast, no caching needed)
        # 法人資料可能比收盤價晚一天，用 institutional 自己的最新日期
        inst_latest_row = conn.execute("SELECT MAX(date) as d FROM institutional").fetchone()
        inst_latest = inst_latest_row['d'] if inst_latest_row and inst_latest_row['d'] else latest

        # 2. Institutional aggregates for TWSE (latest institutional date)
        twse_inst = conn.execute("""
            SELECT SUM(i.foreign_buy) as foreign_net,
                   SUM(i.sitc_buy) as sitc_net,
                   SUM(i.dealer_buy) as dealer_net
            FROM institutional i
            JOIN stocks s ON s.stock_id = i.stock_id
            WHERE i.date = ? AND s.market = 'twse'
        """, (inst_latest,)).fetchone()

        # 3. Same for TPEx
        tpex_inst = conn.execute("""
            SELECT SUM(i.foreign_buy) as foreign_net,
                   SUM(i.sitc_buy) as sitc_net,
                   SUM(i.dealer_buy) as dealer_net
            FROM institutional i
            JOIN stocks s ON s.stock_id = i.stock_id
            WHERE i.date = ? AND s.market = 'tpex'
        """, (inst_latest,)).fetchone()

        # 4. Foreign buy daily trend (20 days)
        foreign_trend = conn.execute("""
            SELECT i.date, SUM(i.foreign_buy) as net
            FROM institutional i
            GROUP BY i.date ORDER BY i.date DESC LIMIT 20
        """).fetchall()
        foreign_trend = [{'date': r['date'], 'net': r['net']} for r in reversed(foreign_trend)]

        # 5. SITC buy daily trend (20 days)
        sitc_trend = conn.execute("""
            SELECT i.date, SUM(i.sitc_buy) as net
            FROM institutional i
            GROUP BY i.date ORDER BY i.date DESC LIMIT 20
        """).fetchall()
        sitc_trend = [{'date': r['date'], 'net': r['net']} for r in reversed(sitc_trend)]

        # 8. Limit up/down anomalies
        limit_up_foreign_sell = conn.execute("""
            SELECT dp.stock_id, s.name, dp.close_price, dp.change_pct, dp.volume,
                   i.foreign_buy,
                   (SELECT SUM(i2.foreign_buy) FROM institutional i2
                    WHERE i2.stock_id = dp.stock_id AND i2.date >= date(?, '-5 days')
                   ) as foreign_5d
            FROM daily_prices dp
            JOIN stocks s ON s.stock_id = dp.stock_id
            LEFT JOIN institutional i ON i.stock_id = dp.stock_id AND i.date = dp.date
            WHERE dp.date = ? AND dp.change_pct >= 9.5 AND i.foreign_buy < 0
            ORDER BY i.foreign_buy ASC
            LIMIT 15
        """, (inst_latest, inst_latest)).fetchall()

        limit_dn_foreign_buy = conn.execute("""
            SELECT dp.stock_id, s.name, dp.close_price, dp.change_pct, dp.volume,
                   i.foreign_buy,
                   (SELECT SUM(i2.foreign_buy) FROM institutional i2
                    WHERE i2.stock_id = dp.stock_id AND i2.date >= date(?, '-5 days')
                   ) as foreign_5d
            FROM daily_prices dp
            JOIN stocks s ON s.stock_id = dp.stock_id
            LEFT JOIN institutional i ON i.stock_id = dp.stock_id AND i.date = dp.date
            WHERE dp.date = ? AND dp.change_pct <= -9.5 AND i.foreign_buy > 0
            ORDER BY i.foreign_buy DESC
            LIMIT 15
        """, (inst_latest, inst_latest)).fetchall()

        # Parallel fetch of all external API data with caching
        external_results = {}
        tasks = {
            'quotes': lambda: get_global_quotes(),
            'futures': lambda: fetch_futures_oi(days=20),
            'pc': lambda: fetch_put_call_ratio(days=20),
            'margin_summary': lambda: fetch_margin_trading_summary(inst_latest),
            'inst_detail': lambda: fetch_institutional_detail(inst_latest),
            'inst_detail_prev': lambda: fetch_institutional_detail_prev(inst_latest),
            'night_session': lambda: fetch_night_session_spread(),
            'tsm_adr': lambda: _fetch_tsm_adr(),
        }

        # Check cache first, collect tasks that need fetching
        tasks_to_fetch = {}
        for key, fn in tasks.items():
            cached = _get_report_cache(key)
            if cached is not None:
                external_results[key] = cached
            else:
                tasks_to_fetch[key] = fn

        # Fetch missing data in parallel
        if tasks_to_fetch:
            with ThreadPoolExecutor(max_workers=6) as executor:
                future_map = {executor.submit(fn): key for key, fn in tasks_to_fetch.items()}
                for future in as_completed(future_map):
                    key = future_map[future]
                    try:
                        result = future.result(timeout=30)
                        external_results[key] = result
                        _set_report_cache(key, result)
                    except Exception as e:
                        logger.warning(f"Report parallel fetch failed for {key}: {e}")
                        external_results[key] = None

        quotes = external_results.get('quotes', [])
        futures_data = external_results.get('futures', [])
        pc_data = external_results.get('pc', [])
        margin_summary = external_results.get('margin_summary')
        inst_detail = external_results.get('inst_detail')
        inst_detail_prev = external_results.get('inst_detail_prev')
        night_session = external_results.get('night_session')
        tsm_adr = external_results.get('tsm_adr')

        return render_template('report.html',
                               date=latest,
                               quotes_json=json.dumps(quotes or []),
                               twse_inst={
                                   'foreign_net': twse_inst['foreign_net'] or 0,
                                   'sitc_net': twse_inst['sitc_net'] or 0,
                                   'dealer_net': twse_inst['dealer_net'] or 0,
                               } if twse_inst else {'foreign_net': 0, 'sitc_net': 0, 'dealer_net': 0},
                               tpex_inst={
                                   'foreign_net': tpex_inst['foreign_net'] or 0,
                                   'sitc_net': tpex_inst['sitc_net'] or 0,
                                   'dealer_net': tpex_inst['dealer_net'] or 0,
                               } if tpex_inst else {'foreign_net': 0, 'sitc_net': 0, 'dealer_net': 0},
                               foreign_trend_json=json.dumps(foreign_trend),
                               sitc_trend_json=json.dumps(sitc_trend),
                               futures_json=json.dumps(futures_data or []),
                               pc_json=json.dumps(pc_data or []),
                               limit_up_sell=limit_up_foreign_sell,
                               limit_dn_buy=limit_dn_foreign_buy,
                               tsm_adr=tsm_adr,
                               margin_summary=margin_summary,
                               inst_detail_json=json.dumps(inst_detail or []),
                               inst_detail_prev_json=json.dumps(inst_detail_prev or []),
                               night_session=night_session,
                               message=None)
    finally:
        conn.close()


@app.route('/heatmap')
def heatmap():
    return render_template('heatmap.html')


@app.route('/api/heatmap')
@limiter.limit("10 per minute")
def api_heatmap():
    """Proxy finviz S&P 500 heatmap data"""
    try:
        r = http_requests.get(
            'https://finviz.com/api/map_perf.ashx?t=sec',
            headers={'User-Agent': 'Mozilla/5.0'}, timeout=15)
        if r.status_code == 200:
            return jsonify(r.json())
        return jsonify({'error': f'Finviz returned {r.status_code}'}), 502
    except Exception as e:
        logger.error(f"Finviz heatmap API error: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/market')
def market():
    futures_data = fetch_futures_oi(days=60)
    retail_data = fetch_retail_ratio(days=60)
    pc_data = fetch_put_call_ratio(days=60)

    # Technical indicators for TAIEX
    indicators = {}
    try:
        ohlc = fetch_taiex_ohlc(120)
        if ohlc:
            indicators = calc_technical_indicators(ohlc)
    except Exception as e:
        logger.warning(f"TAIEX technical indicators failed: {e}")

    return render_template('market.html',
                           futures_json=json.dumps(futures_data),
                           retail_json=json.dumps(retail_data),
                           pc_json=json.dumps(pc_data),
                           indicators_json=json.dumps(indicators))


# ===== API 路由 =====

@app.route('/api/breakout')
def api_breakout():
    conn = get_conn()
    try:
        date = request.args.get('date') or get_latest_date(conn)
        market = request.args.get('market', 'all')
        if not date:
            return jsonify({'error': '無資料'}), 404
        rows = get_breakouts_by_date(conn, date, market if market != 'all' else None)
        return jsonify([dict(r) for r in rows])
    finally:
        conn.close()


@app.route('/api/broker')
def api_broker():
    conn = get_conn()
    try:
        stock_id = request.args.get('stock', '').strip()
        date = request.args.get('date') or get_latest_date(conn)
        if not date or not stock_id:
            return jsonify({'error': '需提供 stock 參數'}), 400
        buy_rows, sell_rows = get_broker_trades(conn, stock_id, date)
        return jsonify({
            'buy': [dict(r) for r in buy_rows],
            'sell': [dict(r) for r in sell_rows],
        })
    finally:
        conn.close()


@app.route('/api/institutional')
def api_institutional():
    conn = get_conn()
    try:
        inst_type = request.args.get('type', 'foreign')
        days = int(request.args.get('days', '1'))
        market = request.args.get('market', 'all')
        date = request.args.get('date')
        if not date:
            # 用 institutional 表自己的最新日期，避免 daily_prices 超前
            row = conn.execute("SELECT MAX(date) as d FROM institutional").fetchone()
            date = row['d'] if row else None
        if not date:
            return jsonify({'error': '無資料'}), 404
        buy_rows, sell_rows = get_ranking(conn, inst_type, days, date,
                                           market if market != 'all' else None)
        return jsonify({
            'buy': [dict(r) for r in buy_rows],
            'sell': [dict(r) for r in sell_rows],
        })
    finally:
        conn.close()


# ===== 大盤技術指標 (TAIEX Technical Indicators) =====

def fetch_taiex_ohlc(days=120):
    """Fetch TAIEX OHLC from Yahoo Finance"""
    cached = _get_report_cache(f'taiex_ohlc_{days}')
    if cached is not None:
        return cached
    try:
        resp = http_requests.get(
            'https://query1.finance.yahoo.com/v8/finance/chart/%5ETWII',
            params={'range': f'{days}d', 'interval': '1d'},
            headers={'User-Agent': 'Mozilla/5.0'}, timeout=15)
        data = resp.json()['chart']['result'][0]
        timestamps = data['timestamp']
        quotes = data['indicators']['quote'][0]
        result = []
        for i, ts in enumerate(timestamps):
            o = quotes['open'][i]
            h = quotes['high'][i]
            l = quotes['low'][i]
            c = quotes['close'][i]
            v = quotes['volume'][i]
            if c is None:
                continue
            d = datetime.fromtimestamp(ts).strftime('%Y-%m-%d')
            result.append({
                'date': d,
                'open': o or c,
                'high': h or c,
                'low': l or c,
                'close': c,
                'volume': v or 0,
            })
        _set_report_cache(f'taiex_ohlc_{days}', result)
        return result
    except Exception as e:
        logger.error(f"TAIEX OHLC 抓取失敗: {e}")
        return []


def _ema(data, period):
    """Calculate EMA"""
    result = []
    multiplier = 2 / (period + 1)
    ema = None
    for val in data:
        if val is None:
            result.append(None)
            continue
        if ema is None:
            ema = val
        else:
            ema = (val - ema) * multiplier + ema
        result.append(round(ema, 2))
    return result


def calc_technical_indicators(ohlc_data):
    """Calculate KD, MACD, Bollinger Bands from OHLC data"""
    closes = [d['close'] for d in ohlc_data]
    highs = [d['high'] for d in ohlc_data]
    lows = [d['low'] for d in ohlc_data]
    dates = [d['date'] for d in ohlc_data]

    # KD (9-day stochastic)
    k_values = []
    d_values = []
    prev_k = 50
    prev_d = 50
    for i in range(len(closes)):
        if i < 8:
            k_values.append(None)
            d_values.append(None)
            continue
        high_9 = max(highs[i-8:i+1])
        low_9 = min(lows[i-8:i+1])
        if high_9 == low_9:
            rsv = 50
        else:
            rsv = (closes[i] - low_9) / (high_9 - low_9) * 100
        k = prev_k * 2/3 + rsv * 1/3
        d = prev_d * 2/3 + k * 1/3
        k_values.append(round(k, 2))
        d_values.append(round(d, 2))
        prev_k = k
        prev_d = d

    # MACD (12, 26, 9)
    ema12 = _ema(closes, 12)
    ema26 = _ema(closes, 26)
    dif = [round(a - b, 2) if a and b else None for a, b in zip(ema12, ema26)]
    dif_clean = [v for v in dif if v is not None]
    macd_signal = _ema(dif_clean, 9)
    # Pad macd_signal to match length
    pad = len(dif) - len(macd_signal)
    macd_signal = [None] * pad + macd_signal
    histogram = [round(d - s, 2) if d is not None and s is not None else None
                 for d, s in zip(dif, macd_signal)]

    # Bollinger Bands (20-day, 2 std)
    bb_mid = []
    bb_upper = []
    bb_lower = []
    for i in range(len(closes)):
        if i < 19:
            bb_mid.append(None)
            bb_upper.append(None)
            bb_lower.append(None)
            continue
        window = closes[i-19:i+1]
        mean = sum(window) / 20
        std = (sum((x - mean) ** 2 for x in window) / 20) ** 0.5
        bb_mid.append(round(mean, 2))
        bb_upper.append(round(mean + 2 * std, 2))
        bb_lower.append(round(mean - 2 * std, 2))

    return {
        'dates': dates,
        'closes': closes,
        'k': k_values,
        'd': d_values,
        'dif': dif,
        'macd_signal': macd_signal,
        'histogram': histogram,
        'bb_mid': bb_mid,
        'bb_upper': bb_upper,
        'bb_lower': bb_lower,
    }


# ===== 主力成本線 (Institutional Cost Estimation) =====

def calc_institutional_cost(conn, stock_id, days=20):
    """Estimate institutional cost by VWAP of buying days"""
    rows = conn.execute("""
        SELECT dp.date, dp.close_price, dp.volume, dp.high_price, dp.low_price,
               COALESCE(i.foreign_buy, 0) as foreign_buy,
               COALESCE(i.sitc_buy, 0) as sitc_buy,
               COALESCE(i.dealer_buy, 0) as dealer_buy
        FROM daily_prices dp
        LEFT JOIN institutional i ON i.stock_id = dp.stock_id AND i.date = dp.date
        WHERE dp.stock_id = ?
        ORDER BY dp.date DESC LIMIT ?
    """, (stock_id, days)).fetchall()

    if not rows:
        return None

    result = {}
    for inst_type, col in [('foreign', 'foreign_buy'), ('sitc', 'sitc_buy'), ('dealer', 'dealer_buy')]:
        total_cost = 0
        total_vol = 0
        days_buying = 0
        for r in rows:
            buy_vol = r[col]
            if buy_vol > 0:
                avg_price = (r['high_price'] + r['low_price']) / 2
                total_cost += avg_price * buy_vol
                total_vol += buy_vol
                days_buying += 1

        if total_vol > 0:
            vwap = round(total_cost / total_vol, 2)
            result[inst_type] = {
                'cost': vwap,
                'total_volume': total_vol,
                'days_buying': days_buying,
            }
        else:
            result[inst_type] = None

    if rows:
        result['current_price'] = rows[0]['close_price']
        result['period'] = days

    return result


# ===== 技術指標計算 =====

def calc_ma(closes, period):
    """計算移動平均線"""
    result = []
    for i in range(len(closes)):
        if i < period - 1:
            result.append(None)
        else:
            result.append(round(sum(closes[i - period + 1:i + 1]) / period, 2))
    return result


def calc_rsi(closes, period=14):
    """計算 RSI 指標"""
    if len(closes) < period + 1:
        return [None] * len(closes)
    result = [None] * period
    changes = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains = [max(0, c) for c in changes[:period]]
    losses = [max(0, -c) for c in changes[:period]]
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    if avg_loss == 0:
        result.append(100.0)
    else:
        rs = avg_gain / avg_loss
        result.append(round(100 - 100 / (1 + rs), 2))
    for i in range(period, len(changes)):
        gain = max(0, changes[i])
        loss = max(0, -changes[i])
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
        if avg_loss == 0:
            result.append(100.0)
        else:
            rs = avg_gain / avg_loss
            result.append(round(100 - 100 / (1 + rs), 2))
    return result


def _get_stock_detail_data(conn, stock_id):
    """取得個股詳細資料（K線、法人、券商），供頁面和 API 共用"""
    # 股票基本資料
    stock_row = conn.execute("SELECT stock_id, name, market FROM stocks WHERE stock_id = ?",
                             (stock_id,)).fetchone()
    if not stock_row:
        return None

    stock_name = stock_row['name']
    stock_market = stock_row['market']

    # K線資料：全歷史（圖表預設顯示最近 250 根，可拖曳到完整歷史）
    price_rows = conn.execute("""
        SELECT date, open_price, high_price, low_price, close_price, volume, change_pct
        FROM daily_prices WHERE stock_id = ? ORDER BY date ASC
    """, (stock_id,)).fetchall()

    kline_data = []
    closes = []
    for r in price_rows:
        kline_data.append({
            'date': r['date'],
            'open': r['open_price'],
            'high': r['high_price'],
            'low': r['low_price'],
            'close': r['close_price'],
            'volume': r['volume'],
            'change_pct': r['change_pct'],
        })
        closes.append(r['close_price'])

    # 計算技術指標
    ma5 = calc_ma(closes, 5)
    ma20 = calc_ma(closes, 20)
    ma60 = calc_ma(closes, 60)
    rsi = calc_rsi(closes, 14)

    # 最新價格資訊
    current_price = closes[-1] if closes else 0
    current_change = kline_data[-1]['change_pct'] if kline_data else 0

    # 法人買賣超：最近 20 個交易日
    inst_rows = conn.execute("""
        SELECT date, foreign_buy, sitc_buy, dealer_buy, total_buy
        FROM institutional
        WHERE stock_id = ? ORDER BY date DESC LIMIT 20
    """, (stock_id,)).fetchall()
    inst_data = []
    for r in inst_rows:
        inst_data.append({
            'date': r['date'],
            'foreign_buy': r['foreign_buy'],
            'sitc_buy': r['sitc_buy'],
            'dealer_buy': r['dealer_buy'],
            'total_buy': r['total_buy'],
        })

    # 法人合計
    foreign_total = sum(r['foreign_buy'] for r in inst_rows)
    sitc_total = sum(r['sitc_buy'] for r in inst_rows)
    dealer_total = sum(r['dealer_buy'] for r in inst_rows)

    # 券商分點
    latest_date = get_latest_date(conn)
    broker_buy, broker_sell = get_broker_trades(conn, stock_id, latest_date) if latest_date else ([], [])

    return {
        'stock_id': stock_id,
        'stock_name': stock_name,
        'stock_market': stock_market,
        'current_price': current_price,
        'current_change': current_change,
        'kline': kline_data,
        'ma5': ma5,
        'ma20': ma20,
        'ma60': ma60,
        'rsi': rsi,
        'institutional': inst_data,
        'foreign_total': foreign_total,
        'sitc_total': sitc_total,
        'dealer_total': dealer_total,
        'broker_buy': [dict(r) for r in broker_buy],
        'broker_sell': [dict(r) for r in broker_sell],
        'broker_date': latest_date,
    }


@app.route('/stock')
def stock_detail():
    stock_id = request.args.get('id', '').strip()
    if not stock_id:
        return render_template('stock.html', data=None, message='請提供股票代號（例：/stock?id=2330）')

    conn = get_conn()
    try:
        data = _get_stock_detail_data(conn, stock_id)
        if not data:
            return render_template('stock.html', data=None,
                                   message=f'找不到股票 {stock_id}')

        # 融資融券資料（即時從 FinMind 抓取）
        margin_data = fetch_margin_data(stock_id)

        # 主力成本線
        inst_cost = calc_institutional_cost(conn, stock_id, days=20)

        # 自選股狀態
        in_watchlist = is_in_watchlist(conn, stock_id)

        # 期貨大戶淨部位 / 籌碼集中度（近20日；無股期則 has_futures=False）
        try:
            large_trader = get_stock_large_trader(conn, stock_id, days=20)
        except Exception as e:
            logger.warning(f"期貨大戶資料讀取失敗 {stock_id}: {e}")
            large_trader = {'has_futures': False, 'products': [], 'series': [], 'latest': None}

        return render_template('stock.html', data=data, message=None,
                               kline_json=json.dumps(data['kline']),
                               ma5_json=json.dumps(data['ma5']),
                               ma20_json=json.dumps(data['ma20']),
                               ma60_json=json.dumps(data['ma60']),
                               rsi_json=json.dumps(data['rsi']),
                               margin_json=json.dumps(margin_data),
                               inst_cost=inst_cost,
                               in_watchlist=in_watchlist,
                               large_trader=large_trader,
                               large_trader_json=json.dumps(large_trader['series']))
    finally:
        conn.close()


@app.route('/api/market')
def api_market():
    futures_data = fetch_futures_oi(days=60)
    retail_data = fetch_retail_ratio(days=60)
    pc_data = fetch_put_call_ratio(days=60)
    return jsonify({
        'futures_oi': futures_data,
        'retail': retail_data,
        'put_call_ratio': pc_data,
    })


@app.route('/api/quotes')
def api_quotes():
    data = get_global_quotes()
    return jsonify(data)


@app.route('/api/search')
def api_search():
    q = request.args.get('q', '').strip()
    if len(q) < 1:
        return jsonify([])
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT stock_id, name, market FROM stocks WHERE stock_id LIKE ? OR name LIKE ? LIMIT 10",
            (f'{q}%', f'%{q}%')
        ).fetchall()
        return jsonify([{'id': r['stock_id'], 'name': r['name'], 'market': r['market']} for r in rows])
    finally:
        conn.close()


@app.route('/api/health')
def api_health():
    """系統健康檢查端點 — 回報 DB 狀態、各資料表最新日期、資料筆數"""
    status = {'status': 'ok', 'checks': {}}
    try:
        conn = get_conn()
        try:
            # DB 連線測試
            conn.execute("SELECT 1").fetchone()
            status['checks']['db'] = 'ok'

            # 各表最新日期與筆數
            # 注意: SQLite 不支援 table/column 名以參數綁定,只能透過白名單 + 嚴格 assert
            HEALTH_CHECK_TABLES = {
                'daily_prices': 'date',
                'breakouts': 'date',
                'institutional': 'date',
                'broker_trades': 'date',
            }
            _ALLOWED_TABLES = set(HEALTH_CHECK_TABLES.keys())
            _ALLOWED_DATE_COLS = {'date'}
            for table, date_col in HEALTH_CHECK_TABLES.items():
                # 白名單嚴格 assert,確保表名/欄名來源可信
                assert table in _ALLOWED_TABLES, f"unsafe table: {table}"
                assert date_col in _ALLOWED_DATE_COLS, f"unsafe column: {date_col}"
                row = conn.execute(f"SELECT MAX({date_col}) as latest, COUNT(*) as cnt FROM {table}").fetchone()
                status['checks'][table] = {
                    'latest_date': row['latest'],
                    'total_rows': row['cnt'],
                }

            # 股票總數
            stock_count = conn.execute("SELECT COUNT(*) as c FROM stocks").fetchone()
            status['checks']['stocks'] = stock_count['c']

        finally:
            conn.close()
    except Exception as e:
        status['status'] = 'error'
        status['checks']['db'] = f'error: {e}'

    # 快取狀態
    with _quotes_lock:
        quotes_age = int(time.time() - _quotes_cache['ts']) if _quotes_cache['ts'] > 0 else -1
    status['checks']['quotes_cache_age_sec'] = quotes_age

    http_code = 200 if status['status'] == 'ok' else 503
    return jsonify(status), http_code


# ===== 資料健康儀表板（不掛在側邊欄，僅 URL 直連使用） =====

_data_health_cache = {"data": None, "ts": 0}
_data_health_lock = threading.Lock()
_DATA_HEALTH_TTL = 60  # 60 秒快取，避免重壓 DB


def _build_data_health():
    """蒐集資料健康指標：每張表的覆蓋、落後、缺漏、品質問題、檔案狀態。"""
    import pandas as pd

    today_str = datetime.now().strftime('%Y-%m-%d')
    project_dir = os.path.dirname(os.path.abspath(__file__))

    # 交易日曆（檔案可能落後實際資料，僅用於缺漏比對）
    cal_path = os.path.join(project_dir, 'data', 'trading_calendar.parquet')
    trading_days = []
    cal_latest = None
    try:
        cal = pd.read_parquet(cal_path)
        cal['date'] = pd.to_datetime(cal['date']).dt.strftime('%Y-%m-%d')
        trading_days = cal['date'].tolist()
        if trading_days:
            cal_latest = trading_days[-1]
    except Exception as e:
        logger.warning(f"data-health: 無法讀取 trading_calendar.parquet: {e}")

    trading_set = set(trading_days)
    # latest_trading_day 與 expected_recent 改在拿到 daily_prices 實際日期後決定
    latest_trading_day = None
    expected_recent = []

    def _trading_lag(table_max_date):
        """從表內最大日期到 latest_trading_day 之間相差幾個交易日。"""
        if not table_max_date or not latest_trading_day:
            return None
        if table_max_date >= latest_trading_day:
            return 0
        # 計算 (table_max_date, latest_trading_day] 之間的交易日數
        lag = 0
        for d in trading_days:
            if d > table_max_date and d <= latest_trading_day:
                lag += 1
        return lag

    def _status_from_lag(lag):
        if lag is None:
            return 'unknown'
        if lag <= 1:
            return 'ok'
        if lag <= 3:
            return 'warn'
        return 'error'

    result = {
        'generated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'today': today_str,
        'latest_trading_day': None,
        'calendar_latest': cal_latest,
        'tables': [],
        'recent_coverage': {'expected_dates': [], 'by_table': {}},
        'gaps': {},
        'quality': [],
        'macro': [],
        'files': [],
        'stock_universe': {},
        'watchlist_count': 0,
    }

    conn = get_conn()
    try:
        # 先以 daily_prices 實際存在的最近 10 個交易日當基準
        recent_dp_dates = [
            r['date'] for r in conn.execute(
                "SELECT DISTINCT date FROM daily_prices ORDER BY date DESC LIMIT 10"
            ).fetchall()
        ]
        if recent_dp_dates:
            latest_trading_day = recent_dp_dates[0]
            expected_recent = recent_dp_dates  # 已是新→舊
        elif trading_days:
            past = [d for d in trading_days if d <= today_str]
            latest_trading_day = past[-1] if past else None
            expected_recent = past[-10:][::-1]
        result['latest_trading_day'] = latest_trading_day
        result['recent_coverage']['expected_dates'] = expected_recent

        # 每張表概覽
        TABLE_DEFS = [
            ('daily_prices', 'date', '日線行情'),
            ('breakouts', 'date', 'N日高點突破'),
            ('institutional', 'date', '法人買賣超'),
            ('broker_trades', 'date', '券商分點進出'),
            ('credit_spread_history', 'date', '信用利差'),
            ('macro_indicators', 'date', '總經指標'),
            ('regime_history', 'date', 'AE 體制偵測'),
        ]

        for tbl, col, label in TABLE_DEFS:
            try:
                row = conn.execute(
                    f"SELECT COUNT(*) AS cnt, MIN({col}) AS dmin, MAX({col}) AS dmax, "
                    f"COUNT(DISTINCT {col}) AS distinct_dates FROM {tbl}"
                ).fetchone()
                rows = row['cnt'] or 0
                dmin = row['dmin']
                dmax = row['dmax']
                distinct_dates = row['distinct_dates'] or 0
                # 最新一天的筆數
                latest_count = 0
                if dmax:
                    r2 = conn.execute(
                        f"SELECT COUNT(*) AS c FROM {tbl} WHERE {col} = ?", (dmax,)
                    ).fetchone()
                    latest_count = r2['c'] or 0
                lag = _trading_lag(dmax)
                result['tables'].append({
                    'name': tbl,
                    'label': label,
                    'rows': rows,
                    'date_min': dmin,
                    'date_max': dmax,
                    'distinct_dates': distinct_dates,
                    'latest_row_count': latest_count,
                    'lag_trading_days': lag,
                    'status': _status_from_lag(lag),
                })
            except Exception as e:
                result['tables'].append({
                    'name': tbl, 'label': label, 'error': str(e), 'status': 'error',
                })

        # 最近 10 個交易日，每張主表的覆蓋筆數
        # breakouts 不放入熱圖（本來就只記錄突破的個股，列數天然稀疏，不適合用同一閾值）
        COVERAGE_TABLES = ['daily_prices', 'institutional', 'broker_trades']
        GAP_TABLES = ['daily_prices', 'institutional', 'broker_trades', 'breakouts']
        if expected_recent:
            placeholders = ','.join(['?'] * len(expected_recent))
            for tbl in COVERAGE_TABLES:
                try:
                    rows = conn.execute(
                        f"SELECT date, COUNT(DISTINCT stock_id) AS n FROM {tbl} "
                        f"WHERE date IN ({placeholders}) GROUP BY date",
                        expected_recent,
                    ).fetchall()
                    by_date = {r['date']: r['n'] for r in rows}
                    result['recent_coverage']['by_table'][tbl] = [
                        {'date': d, 'count': by_date.get(d, 0)} for d in expected_recent
                    ]
                except Exception as e:
                    result['recent_coverage']['by_table'][tbl] = {'error': str(e)}

        # 缺漏的交易日（與 trading_calendar 比對，限定在表內 [min,max] 範圍內）
        for tbl in GAP_TABLES:
            try:
                rmm = conn.execute(
                    f"SELECT MIN(date) AS dmin, MAX(date) AS dmax FROM {tbl}"
                ).fetchone()
                dmin, dmax = rmm['dmin'], rmm['dmax']
                if not dmin or not dmax or not trading_set:
                    result['gaps'][tbl] = []
                    continue
                expected_in_range = {d for d in trading_days if dmin <= d <= dmax}
                actual = {
                    r['date'] for r in conn.execute(
                        f"SELECT DISTINCT date FROM {tbl} WHERE date BETWEEN ? AND ?",
                        (dmin, dmax),
                    ).fetchall()
                }
                missing = sorted(expected_in_range - actual, reverse=True)
                result['gaps'][tbl] = {
                    'missing_count': len(missing),
                    'missing_recent': missing[:20],
                }
            except Exception as e:
                result['gaps'][tbl] = {'error': str(e)}

        # 品質檢查
        QUALITY_CHECKS = [
            ('daily_prices.null_close',
             "SELECT COUNT(*) FROM daily_prices WHERE close_price IS NULL", 'OK 應為 0'),
            ('daily_prices.zero_or_negative_close',
             "SELECT COUNT(*) FROM daily_prices WHERE close_price <= 0", 'OK 應為 0'),
            ('daily_prices.zero_volume',
             "SELECT COUNT(*) FROM daily_prices WHERE volume = 0 OR volume IS NULL", '停牌或缺資料'),
            ('daily_prices.extreme_change_pct',
             "SELECT COUNT(*) FROM daily_prices dp "
             "WHERE ABS(dp.change_pct) > 11 "
             "AND LENGTH(dp.stock_id) = 4 AND dp.stock_id GLOB '[1-9]*' "
             "AND (SELECT COUNT(*) FROM daily_prices WHERE stock_id=dp.stock_id AND date<=dp.date) > 5",
             '真股票上市第 6 日後仍漲跌超過 ±11%（豁免 ETF/權證/IPO 前 5 日）'),
            ('daily_prices.unadjusted_split_dividend',
             "WITH paired AS ( "
             "  SELECT d1.stock_id, d1.date, d1.adj_close AS c, d1.change_pct, "
             "    (SELECT adj_close FROM daily_prices d2 WHERE d2.stock_id=d1.stock_id "
             "     AND d2.date<d1.date ORDER BY d2.date DESC LIMIT 1) AS pc "
             "  FROM daily_prices d1 "
             "  WHERE LENGTH(d1.stock_id)=4 AND d1.stock_id GLOB '[1-9]*' "
             "    AND d1.adj_close IS NOT NULL "
             ") "
             "SELECT COUNT(*) FROM paired "
             "WHERE pc > 0 AND c > 0 "
             "AND ABS((c/pc - 1)*100 - change_pct) > 5",
             '已用 adj_close 比對；非 0 = 還原失敗或 raw close 仍含未還原跳空'),
            ('daily_prices.adj_close_missing',
             "SELECT COUNT(*) FROM daily_prices WHERE adj_close IS NULL",
             'adj_close 未計算（請執行 backfill_adj_prices.py）'),
            ('daily_prices.high_lt_low',
             "SELECT COUNT(*) FROM daily_prices WHERE high_price < low_price", 'OK 應為 0'),
            ('institutional.zero_total',
             "SELECT COUNT(*) FROM institutional WHERE total_buy = 0 AND foreign_buy = 0 AND sitc_buy = 0 AND dealer_buy = 0",
             '冷門股當日無法人交易（已驗證為真實資料,非異常）'),
            ('daily_prices.zero_volume_real_stock',
             "SELECT COUNT(*) FROM daily_prices WHERE (volume = 0 OR volume IS NULL) "
             "AND LENGTH(stock_id)=4 AND stock_id GLOB '[1-9]*'",
             '真股票零成交量（停牌或缺資料,豁免 ETF/權證）'),
            ('stocks.duplicates',
             "SELECT COUNT(*) - COUNT(DISTINCT stock_id) FROM stocks", 'OK 應為 0'),
            ('orphan.daily_prices_not_in_stocks',
             "SELECT COUNT(DISTINCT dp.stock_id) FROM daily_prices dp "
             "LEFT JOIN stocks s ON dp.stock_id = s.stock_id WHERE s.stock_id IS NULL",
             '行情中存在、但 stocks 名冊查不到的代號'),
            ('orphan.institutional_recent_not_in_stocks',
             "SELECT COUNT(*) FROM (SELECT i.stock_id FROM institutional i "
             "LEFT JOIN stocks s ON i.stock_id = s.stock_id WHERE s.stock_id IS NULL "
             "GROUP BY i.stock_id HAVING MAX(i.date) >= date('now','-6 months'))",
             '近 6 個月仍有法人資料、但 stocks 名冊缺收（疑似新上市未收錄）'),
            ('orphan.institutional_historical_delisted',
             "SELECT COUNT(*) FROM (SELECT i.stock_id FROM institutional i "
             "LEFT JOIN stocks s ON i.stock_id = s.stock_id WHERE s.stock_id IS NULL "
             "GROUP BY i.stock_id HAVING MAX(i.date) < date('now','-6 months'))",
             '已下市超過 6 個月的歷史法人資料（保留正常）'),
        ]
        for name, sql, hint in QUALITY_CHECKS:
            try:
                cnt = conn.execute(sql).fetchone()[0]
                severity = 'ok' if cnt == 0 else ('warn' if cnt < 100 else 'error')
                # 預期非 0 的檢查（zero_volume / extreme_change_pct / zero_total）降一級
                if name in ('daily_prices.zero_volume', 'daily_prices.extreme_change_pct',
                            'institutional.zero_total'):
                    severity = 'ok' if cnt == 0 else ('info' if cnt < 1000 else 'warn')
                # zero_volume 全是 ETF/權證、zero_total 已驗證為冷門股 → 直接降 info
                if name in ('daily_prices.zero_volume', 'institutional.zero_total'):
                    severity = 'info' if cnt > 0 else 'ok'
                # 歷史下市股的 orphan 永遠標 info（保留是正常的）
                if name == 'orphan.institutional_historical_delisted':
                    severity = 'info' if cnt > 0 else 'ok'
                result['quality'].append({
                    'check': name, 'count': int(cnt), 'hint': hint, 'severity': severity,
                })
            except Exception as e:
                result['quality'].append({
                    'check': name, 'error': str(e), 'severity': 'error',
                })

        # macro_indicators 各 series
        try:
            for r in conn.execute(
                "SELECT indicator, MAX(date) AS dmax, COUNT(*) AS cnt "
                "FROM macro_indicators GROUP BY indicator ORDER BY indicator"
            ).fetchall():
                lag = _trading_lag(r['dmax']) if r['dmax'] else None
                result['macro'].append({
                    'indicator': r['indicator'],
                    'latest': r['dmax'],
                    'rows': r['cnt'],
                    'lag_trading_days': lag,
                    'status': _status_from_lag(lag),
                })
        except Exception as e:
            result['macro'] = [{'error': str(e)}]

        # 股票名冊
        try:
            for r in conn.execute(
                "SELECT market, COUNT(*) AS c FROM stocks GROUP BY market"
            ).fetchall():
                result['stock_universe'][r['market']] = r['c']
        except Exception:
            pass

        # 自選股
        try:
            row = conn.execute("SELECT COUNT(*) AS c FROM watchlist").fetchone()
            result['watchlist_count'] = row['c'] or 0
        except Exception:
            pass

    finally:
        conn.close()

    # 重要檔案的大小與更新時間
    FILES_TO_CHECK = [
        'db/scanner.db',
        'data/institutional_clean.parquet',
        'data/institutional_full.parquet',
        'data/trading_calendar.parquet',
        'data/stocks_index.parquet',
        'data/data_quality_report.txt',
        'data/institutional_summary.txt',
        'backfill_institutional.log',
        'backfill_broker.log',
        'watchdog.log',
    ]
    for rel in FILES_TO_CHECK:
        full = os.path.join(project_dir, rel)
        try:
            if os.path.exists(full):
                st = os.stat(full)
                result['files'].append({
                    'path': rel,
                    'size_kb': round(st.st_size / 1024, 1),
                    'mtime': datetime.fromtimestamp(st.st_mtime).strftime('%Y-%m-%d %H:%M:%S'),
                    'exists': True,
                })
            else:
                result['files'].append({'path': rel, 'exists': False})
        except Exception as e:
            result['files'].append({'path': rel, 'error': str(e)})

    return result


@app.route('/api/data-health')
def api_data_health():
    """回傳資料健康儀表板的 JSON。預設 60 秒快取，?force=1 可強制重新計算。"""
    force = request.args.get('force') == '1'
    now = time.time()
    with _data_health_lock:
        if (not force and _data_health_cache['data'] is not None
                and now - _data_health_cache['ts'] < _DATA_HEALTH_TTL):
            data = _data_health_cache['data']
        else:
            data = _build_data_health()
            _data_health_cache['data'] = data
            _data_health_cache['ts'] = now
    return jsonify(data)


@app.route('/data-health')
def data_health_page():
    """資料健康儀表板頁面。刻意不放在側邊欄，僅供直接以 URL 訪問。"""
    return render_template('data_health.html')


@app.route('/api/stock')
def api_stock():
    stock_id = request.args.get('id', '').strip()
    if not stock_id:
        return jsonify({'error': '需提供 id 參數'}), 400

    conn = get_conn()
    try:
        data = _get_stock_detail_data(conn, stock_id)
        if not data:
            return jsonify({'error': f'找不到股票 {stock_id}'}), 404
        return jsonify(data)
    finally:
        conn.close()


@app.route('/api/stock-large-trader')
def api_stock_large_trader():
    """
    個股期「期貨大戶淨部位 + 籌碼集中度」。
    參數：id=股票代號（必填）、days=取幾個交易日（預設 20，上限 250）。
    無股期標的 → has_futures=false、series=[]（不視為錯誤）。
    """
    stock_id = request.args.get('id', '').strip()
    if not stock_id:
        return jsonify({'error': '需提供 id 參數'}), 400
    try:
        days = max(1, min(250, int(request.args.get('days', 20))))
    except (TypeError, ValueError):
        days = 20

    conn = get_conn()
    try:
        return jsonify(get_stock_large_trader(conn, stock_id, days=days))
    except Exception as e:
        logger.error(f"api_stock_large_trader({stock_id}) 失敗: {e}", exc_info=True)
        return jsonify({'error': '期貨大戶資料讀取失敗'}), 500
    finally:
        conn.close()


@app.route('/watchlist')
def watchlist():
    conn = get_conn()
    try:
        items = get_watchlist(conn)
        return render_template('watchlist.html', items=items)
    finally:
        conn.close()


@app.route('/api/watchlist/add', methods=['POST'])
def api_watchlist_add():
    stock_id = request.json.get('stock_id', '').strip()
    if not stock_id:
        return jsonify({'error': 'missing stock_id'}), 400
    conn = get_conn()
    try:
        add_to_watchlist(conn, stock_id)
        return jsonify({'ok': True})
    finally:
        conn.close()


@app.route('/api/watchlist/remove', methods=['POST'])
def api_watchlist_remove():
    stock_id = request.json.get('stock_id', '').strip()
    conn = get_conn()
    try:
        remove_from_watchlist(conn, stock_id)
        return jsonify({'ok': True})
    finally:
        conn.close()


@app.route('/api/stock-realtime')
@limiter.limit("10 per minute")
def api_stock_realtime():
    """盤中即時報價（單一個股），用 mis.twse.com.tw"""
    stock_id = request.args.get('id', '').strip()
    if not stock_id:
        return jsonify({'error': 'missing id'}), 400

    conn = get_conn()
    try:
        stock = conn.execute("SELECT stock_id, name, market FROM stocks WHERE stock_id=?", (stock_id,)).fetchone()
        if not stock:
            return jsonify({'error': 'not found'}), 404
    finally:
        conn.close()

    from scrapers.realtime import MIS_URL, _parse_float, _parse_int
    prefix = 'tse' if stock['market'] == 'twse' else 'otc'
    query = f'{prefix}_{stock_id}.tw'

    try:
        resp = http_requests.get(MIS_URL, params={'ex_ch': query},
                                 headers={'User-Agent': 'Mozilla/5.0'}, timeout=10)
        data = resp.json()
        items = data.get('msgArray', [])
        if not items:
            return jsonify({'error': 'no data'}), 404

        item = items[0]
        z = _parse_float(item.get('z'))       # 最新成交價
        y = _parse_float(item.get('y'))       # 昨收
        o = _parse_float(item.get('o'))       # 開盤
        h = _parse_float(item.get('h'))       # 最高
        l = _parse_float(item.get('l'))       # 最低
        v = _parse_int(item.get('v'))         # 成交量(張)
        t = item.get('t', '')                 # 時間

        pct = round((z - y) / y * 100, 2) if z and y and y > 0 else 0
        change = round(z - y, 2) if z and y else 0

        return jsonify({
            'stock_id': stock_id,
            'price': z,
            'change': change,
            'change_pct': pct,
            'open': o,
            'high': h,
            'low': l,
            'volume': v,
            'time': t,
            'yesterday': y,
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/stock-preview')
def api_stock_preview():
    stock_id = request.args.get('id', '').strip()
    conn = get_conn()
    try:
        stock = conn.execute("SELECT * FROM stocks WHERE stock_id=?", (stock_id,)).fetchone()
        if not stock:
            return jsonify({'error': 'not found'}), 404
        prices = conn.execute("""
            SELECT date, close_price, open_price, high_price, low_price, change_pct, volume
            FROM daily_prices WHERE stock_id=? ORDER BY date DESC LIMIT 20
        """, (stock_id,)).fetchall()
        inst = conn.execute("""
            SELECT date, foreign_buy, sitc_buy, dealer_buy
            FROM institutional WHERE stock_id=? ORDER BY date DESC LIMIT 5
        """, (stock_id,)).fetchall()
        in_watchlist = is_in_watchlist(conn, stock_id)
        return jsonify({
            'stock_id': stock['stock_id'],
            'name': stock['name'],
            'market': stock['market'],
            'prices': [dict(r) for r in prices],
            'institutional': [dict(r) for r in inst],
            'in_watchlist': in_watchlist,
        })
    finally:
        conn.close()


@app.route('/api/export/breakout')
def export_breakout():
    import csv, io
    conn = get_conn()
    try:
        date = request.args.get('date') or get_latest_date(conn)
        market = request.args.get('market', 'all')
        rows = get_breakouts_by_date(conn, date, market if market != 'all' else None)
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(['代號', '名稱', '市場', '收盤價', '漲跌%', '成交量', '5日', '10日', '20日', '60日', '120日', '240日'])
        for r in rows:
            writer.writerow([r['stock_id'], r['name'], r['market'], r['close_price'], r['change_pct'], r['volume'],
                           r['break_5'], r['break_10'], r['break_20'], r['break_60'], r['break_120'], r['break_240']])
        resp = app.response_class(output.getvalue(), mimetype='text/csv',
                                   headers={'Content-Disposition': f'attachment;filename=breakout_{date}.csv'})
        return resp
    finally:
        conn.close()


@app.route('/api/export/institutional')
def export_institutional():
    import csv, io
    conn = get_conn()
    try:
        inst_type = request.args.get('type', 'foreign')
        days = int(request.args.get('days', '1'))
        market = request.args.get('market', 'all')
        date = request.args.get('date') or get_latest_date(conn)
        if not date:
            return jsonify({'error': 'no data'}), 404
        buy_rows, sell_rows = get_ranking(conn, inst_type, days, date,
                                           market if market != 'all' else None)
        type_names = {'foreign': '外資', 'sitc': '投信', 'dealer': '自營商', 'total': '三大法人'}
        type_label = type_names.get(inst_type, inst_type)
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(['方向', '代號', '名稱', '市場', f'{type_label}買賣超(張)', '收盤價', '漲跌%'])
        for r in buy_rows:
            writer.writerow(['買超', r['stock_id'], r['name'], r['market'], r['total_amount'], r['close_price'] or 0, r['change_pct'] or 0])
        for r in sell_rows:
            writer.writerow(['賣超', r['stock_id'], r['name'], r['market'], r['total_amount'], r['close_price'] or 0, r['change_pct'] or 0])
        resp = app.response_class(output.getvalue(), mimetype='text/csv',
                                   headers={'Content-Disposition': f'attachment;filename=institutional_{inst_type}_{days}d_{date}.csv'})
        return resp
    finally:
        conn.close()


@app.route('/api/export/broker')
def export_broker():
    import csv, io
    conn = get_conn()
    try:
        stock_id = request.args.get('stock', '').strip()
        date = request.args.get('date') or get_latest_date(conn)
        if not stock_id or not date:
            return jsonify({'error': 'missing params'}), 400
        buy_rows, sell_rows = get_broker_trades(conn, stock_id, date)
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(['方向', '券商', '買進', '賣出', '淨買賣超', '佔成交%'])
        for r in buy_rows:
            writer.writerow(['買超', r['broker_name'], r['buy_volume'], r['sell_volume'], r['net_volume'], r['pct']])
        for r in sell_rows:
            writer.writerow(['賣超', r['broker_name'], r['buy_volume'], r['sell_volume'], r['net_volume'], r['pct']])
        resp = app.response_class(output.getvalue(), mimetype='text/csv',
                                   headers={'Content-Disposition': f'attachment;filename=broker_{stock_id}_{date}.csv'})
        return resp
    finally:
        conn.close()


def redirect_to_breakout():
    from flask import redirect, url_for
    return redirect(url_for('breakout'))


# ===== Feature 1: 條件篩選器 (Custom Stock Screener) =====

VALID_BREAK_DAYS = {5, 10, 20, 60, 120, 240}


@app.route('/screener')
def screener():
    conn = get_conn()
    try:
        date = get_latest_date(conn)
        results = []
        # Get filter params
        min_volume = request.args.get('min_vol', type=int)
        min_price = request.args.get('min_price', type=float)
        max_price = request.args.get('max_price', type=float)
        min_change = request.args.get('min_change', type=float)
        max_change = request.args.get('max_change', type=float)
        foreign_dir = request.args.get('foreign')  # 'buy' or 'sell'
        sitc_dir = request.args.get('sitc')
        break_days = request.args.get('break_days', type=int)
        market = request.args.get('market', 'all')
        consecutive_foreign = request.args.get('consec_foreign', type=int)

        # Validate break_days against whitelist to prevent SQL injection
        if break_days and break_days not in VALID_BREAK_DAYS:
            break_days = None

        # Build dynamic query
        conditions = ["dp.date = ?"]
        params = [date]

        joins = """
            FROM daily_prices dp
            JOIN stocks s ON s.stock_id = dp.stock_id
            LEFT JOIN institutional i ON i.stock_id = dp.stock_id AND i.date = dp.date
            LEFT JOIN breakouts b ON b.stock_id = dp.stock_id AND b.date = dp.date
        """

        if market and market != 'all':
            conditions.append("s.market = ?")
            params.append(market)
        if min_volume:
            conditions.append("dp.volume >= ?")
            params.append(min_volume)
        if min_price:
            conditions.append("dp.close_price >= ?")
            params.append(min_price)
        if max_price:
            conditions.append("dp.close_price <= ?")
            params.append(max_price)
        if min_change is not None:
            conditions.append("dp.change_pct >= ?")
            params.append(min_change)
        if max_change is not None:
            conditions.append("dp.change_pct <= ?")
            params.append(max_change)
        if foreign_dir == 'buy':
            conditions.append("i.foreign_buy > 0")
        elif foreign_dir == 'sell':
            conditions.append("i.foreign_buy < 0")
        if sitc_dir == 'buy':
            conditions.append("i.sitc_buy > 0")
        elif sitc_dir == 'sell':
            conditions.append("i.sitc_buy < 0")
        if break_days:
            conditions.append(f"b.break_{break_days} = 1")

        where = " AND ".join(conditions)

        # Only query if at least one filter is active (besides date)
        has_filter = any([min_volume, min_price, max_price, min_change is not None, max_change is not None,
                         foreign_dir, sitc_dir, break_days, (market and market != 'all'), consecutive_foreign])

        if has_filter:
            sql = f"""
                SELECT dp.stock_id, s.name, s.market, dp.close_price, dp.change_pct, dp.volume,
                       COALESCE(i.foreign_buy, 0) as foreign_buy, COALESCE(i.sitc_buy, 0) as sitc_buy,
                       b.break_5, b.break_10, b.break_20, b.break_60, b.break_120, b.break_240
                {joins}
                WHERE {where}
                ORDER BY dp.volume DESC
                LIMIT 200
            """
            results = conn.execute(sql, params).fetchall()

        return render_template('screener.html', results=results, date=date,
                             has_filter=has_filter,
                             f_min_vol=min_volume, f_min_price=min_price, f_max_price=max_price,
                             f_min_change=min_change, f_max_change=max_change,
                             f_foreign=foreign_dir, f_sitc=sitc_dir,
                             f_break=break_days, f_market=market,
                             f_consec=consecutive_foreign)
    finally:
        conn.close()


@app.route('/api/export/screener')
def export_screener():
    """Export screener results as CSV"""
    import csv, io
    conn = get_conn()
    try:
        date = get_latest_date(conn)
        min_volume = request.args.get('min_vol', type=int)
        min_price = request.args.get('min_price', type=float)
        max_price = request.args.get('max_price', type=float)
        min_change = request.args.get('min_change', type=float)
        max_change = request.args.get('max_change', type=float)
        foreign_dir = request.args.get('foreign')
        sitc_dir = request.args.get('sitc')
        break_days = request.args.get('break_days', type=int)
        market = request.args.get('market', 'all')

        if break_days and break_days not in VALID_BREAK_DAYS:
            break_days = None

        conditions = ["dp.date = ?"]
        params = [date]
        joins = """
            FROM daily_prices dp
            JOIN stocks s ON s.stock_id = dp.stock_id
            LEFT JOIN institutional i ON i.stock_id = dp.stock_id AND i.date = dp.date
            LEFT JOIN breakouts b ON b.stock_id = dp.stock_id AND b.date = dp.date
        """
        if market and market != 'all':
            conditions.append("s.market = ?")
            params.append(market)
        if min_volume:
            conditions.append("dp.volume >= ?")
            params.append(min_volume)
        if min_price:
            conditions.append("dp.close_price >= ?")
            params.append(min_price)
        if max_price:
            conditions.append("dp.close_price <= ?")
            params.append(max_price)
        if min_change is not None:
            conditions.append("dp.change_pct >= ?")
            params.append(min_change)
        if max_change is not None:
            conditions.append("dp.change_pct <= ?")
            params.append(max_change)
        if foreign_dir == 'buy':
            conditions.append("i.foreign_buy > 0")
        elif foreign_dir == 'sell':
            conditions.append("i.foreign_buy < 0")
        if sitc_dir == 'buy':
            conditions.append("i.sitc_buy > 0")
        elif sitc_dir == 'sell':
            conditions.append("i.sitc_buy < 0")
        if break_days:
            conditions.append(f"b.break_{break_days} = 1")

        where = " AND ".join(conditions)
        sql = f"""
            SELECT dp.stock_id, s.name, s.market, dp.close_price, dp.change_pct, dp.volume,
                   COALESCE(i.foreign_buy, 0) as foreign_buy, COALESCE(i.sitc_buy, 0) as sitc_buy,
                   b.break_5, b.break_10, b.break_20, b.break_60, b.break_120, b.break_240
            {joins}
            WHERE {where}
            ORDER BY dp.volume DESC
            LIMIT 200
        """
        results = conn.execute(sql, params).fetchall()

        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(['代號', '名稱', '市場', '收盤價', '漲跌%', '成交量(張)', '外資買賣超', '投信買賣超',
                         '5日突破', '10日突破', '20日突破', '60日突破', '120日突破', '240日突破'])
        for r in results:
            writer.writerow([r['stock_id'], r['name'], r['market'], r['close_price'], r['change_pct'],
                           r['volume'], r['foreign_buy'], r['sitc_buy'],
                           r['break_5'] or 0, r['break_10'] or 0, r['break_20'] or 0,
                           r['break_60'] or 0, r['break_120'] or 0, r['break_240'] or 0])
        resp = app.response_class(output.getvalue(), mimetype='text/csv',
                                   headers={'Content-Disposition': f'attachment;filename=screener_{date}.csv'})
        return resp
    finally:
        conn.close()


# ===== Feature 2: 產業族群分類 (Industry Sector Classification) =====

def populate_sectors():
    """Fetch and update sector info for all stocks (idempotent)"""
    try:
        resp = http_requests.get('https://api.finmindtrade.com/api/v4/data',
            params={'dataset': 'TaiwanStockInfo'},
            headers={'User-Agent': 'Mozilla/5.0'}, timeout=30)
        data = resp.json().get('data', [])
        conn = get_conn()
        try:
            updated = 0
            for row in data:
                stock_id = row.get('stock_id', '')
                sector = row.get('industry_category', '')
                if stock_id and sector:
                    conn.execute("UPDATE stocks SET sector = ? WHERE stock_id = ?", (sector, stock_id))
                    updated += 1
            conn.commit()
            logger.info(f"產業分類更新完成: {updated} 筆")
        finally:
            conn.close()
    except Exception as e:
        logger.error(f"產業分類更新失敗: {e}")


@app.route('/sectors')
def sectors():
    conn = get_conn()
    try:
        date = get_latest_date(conn)
        rows = conn.execute("""
            SELECT s.sector, COUNT(*) as stock_count,
                   ROUND(AVG(dp.change_pct), 2) as avg_change,
                   SUM(CASE WHEN dp.change_pct > 0 THEN 1 ELSE 0 END) as up_count,
                   SUM(CASE WHEN dp.change_pct < 0 THEN 1 ELSE 0 END) as dn_count
            FROM stocks s
            JOIN daily_prices dp ON dp.stock_id = s.stock_id AND dp.date = ?
            WHERE s.sector != '' AND s.sector IS NOT NULL
            GROUP BY s.sector
            ORDER BY avg_change DESC
        """, (date,)).fetchall()
        return render_template('sectors.html', sectors=rows, date=date)
    finally:
        conn.close()


@app.route('/sector/<name>')
def sector_detail(name):
    conn = get_conn()
    try:
        date = get_latest_date(conn)
        rows = conn.execute("""
            SELECT dp.stock_id, s.name, dp.close_price, dp.change_pct, dp.volume,
                   COALESCE(i.foreign_buy, 0) as foreign_buy
            FROM daily_prices dp
            JOIN stocks s ON s.stock_id = dp.stock_id
            LEFT JOIN institutional i ON i.stock_id = dp.stock_id AND i.date = dp.date
            WHERE dp.date = ? AND s.sector = ?
            ORDER BY dp.change_pct DESC
        """, (date, name)).fetchall()
        return render_template('sector_detail.html', stocks=rows, sector=name, date=date)
    finally:
        conn.close()


# ===== Feature 3: 類股漲跌幅熱力圖 (Taiwan Sector Heatmap) =====

@app.route('/tw-heatmap')
def tw_heatmap():
    return render_template('tw_heatmap.html')


@app.route('/api/tw-heatmap')
def api_tw_heatmap():
    """
    盤中(9:00~13:30)：從即時 API 抓最新報價
    盤後：用 DB 收盤資料
    """
    now = datetime.now()
    is_trading = (now.weekday() < 5 and 900 <= now.hour * 100 + now.minute <= 1330)

    if is_trading:
        return _tw_heatmap_realtime()
    else:
        return _tw_heatmap_db()


def _tw_heatmap_db():
    """盤後：用 DB 收盤資料"""
    conn = get_conn()
    try:
        date = get_latest_date(conn)
        rows = conn.execute("""
            SELECT s.sector, dp.stock_id, s.name, dp.close_price, dp.change_pct, dp.volume
            FROM daily_prices dp
            JOIN stocks s ON s.stock_id = dp.stock_id
            WHERE dp.date = ? AND s.sector != '' AND s.sector IS NOT NULL
            ORDER BY s.sector, dp.volume DESC
        """, (date,)).fetchall()
        sector_map = {}
        for r in rows:
            sec = r['sector']
            if sec not in sector_map:
                sector_map[sec] = []
            if len(sector_map[sec]) < 10:
                sector_map[sec].append({
                    'id': r['stock_id'], 'name': r['name'],
                    'price': r['close_price'], 'pct': r['change_pct'],
                    'volume': r['volume']
                })
        return jsonify({'date': date, 'sectors': sector_map, 'realtime': False})
    finally:
        conn.close()


# 即時熱力圖快取（5 分鐘）
_heatmap_rt_cache = {'data': None, 'ts': 0}

def _tw_heatmap_realtime():
    """盤中：從 mis.twse.com.tw 抓即時報價"""
    import time as _time
    now_ts = _time.time()

    # 5 分鐘快取
    if _heatmap_rt_cache['data'] and (now_ts - _heatmap_rt_cache['ts']) < 300:
        return jsonify(_heatmap_rt_cache['data'])

    conn = get_conn()
    try:
        # 取所有有產業分類的股票
        stocks = conn.execute("""
            SELECT s.stock_id, s.name, s.market, s.sector
            FROM stocks s
            WHERE s.sector != '' AND s.sector IS NOT NULL
        """).fetchall()
    finally:
        conn.close()

    if not stocks:
        return _tw_heatmap_db()

    # 按產業取前 10 大（用 DB 的成交量排序）
    conn = get_conn()
    try:
        date = get_latest_date(conn)
        vol_map = {}
        rows = conn.execute("""
            SELECT stock_id, volume FROM daily_prices WHERE date = ?
        """, (date,)).fetchall()
        for r in rows:
            vol_map[r['stock_id']] = r['volume'] or 0
    finally:
        conn.close()

    # 每個產業取前 10 檔
    from collections import defaultdict
    sector_stocks = defaultdict(list)
    for s in stocks:
        sector_stocks[s['sector']].append(s)

    # 排序取 top 10
    fetch_list = []
    for sec, slist in sector_stocks.items():
        slist.sort(key=lambda x: vol_map.get(x['stock_id'], 0), reverse=True)
        for s in slist[:10]:
            fetch_list.append(s)

    # 批次抓即時報價
    from scrapers.realtime import MIS_URL, _parse_float, _parse_int
    import requests as _req
    from config import REQUEST_HEADERS, REQUEST_TIMEOUT

    BATCH = 50
    rt_prices = {}

    for i in range(0, len(fetch_list), BATCH):
        batch = fetch_list[i:i+BATCH]
        parts = []
        for s in batch:
            prefix = 'tse' if s['market'] == 'twse' else 'otc'
            parts.append(f"{prefix}_{s['stock_id']}.tw")
        query = '|'.join(parts)

        try:
            resp = _req.get(MIS_URL, params={'ex_ch': query},
                           headers=REQUEST_HEADERS, timeout=REQUEST_TIMEOUT)
            data = resp.json()
            for item in data.get('msgArray', []):
                sid = item.get('c', '')
                z = _parse_float(item.get('z'))  # 最新成交價
                y = _parse_float(item.get('y'))  # 昨收
                v = _parse_int(item.get('v'))     # 成交量(張)
                if z and y and y > 0:
                    pct = round((z - y) / y * 100, 2)
                    rt_prices[sid] = {'price': z, 'pct': pct, 'volume': v}
        except Exception:
            pass
        _time.sleep(0.3)

    # 組合結果
    today_str = datetime.now().strftime('%Y-%m-%d')
    sector_map = {}
    for sec, slist in sector_stocks.items():
        sector_map[sec] = []
        slist.sort(key=lambda x: vol_map.get(x['stock_id'], 0), reverse=True)
        for s in slist[:10]:
            sid = s['stock_id']
            if sid in rt_prices:
                sector_map[sec].append({
                    'id': sid, 'name': s['name'],
                    'price': rt_prices[sid]['price'],
                    'pct': rt_prices[sid]['pct'],
                    'volume': rt_prices[sid]['volume'],
                })

    result = {'date': today_str, 'sectors': sector_map, 'realtime': True,
              'update_time': datetime.now().strftime('%H:%M:%S')}
    _heatmap_rt_cache['data'] = result
    _heatmap_rt_cache['ts'] = now_ts
    return jsonify(result)


@app.route('/api/market-indicators')
def api_market_indicators():
    """Return TAIEX technical indicators as JSON"""
    try:
        ohlc = fetch_taiex_ohlc(120)
        if not ohlc:
            return jsonify({'error': 'No TAIEX data'}), 404
        indicators = calc_technical_indicators(ohlc)
        return jsonify(indicators)
    except Exception as e:
        logger.error(f"Market indicators API error: {e}")
        return jsonify({'error': str(e)}), 500


# ===== Feature: 歷史回測 (Backtesting) =====

@app.route('/backtest')
def backtest():
    conn = get_conn()
    try:
        break_days = int(request.args.get('break', '20'))
        hold_days = int(request.args.get('hold', '5'))
        start_date = request.args.get('start', '')
        end_date = request.args.get('end', '')

        # Validate break_days against whitelist
        if break_days not in VALID_BREAK_DAYS:
            break_days = 20

        # Default: last 3 months
        if not start_date or not end_date:
            end_dt = datetime.now()
            start_dt = end_dt - timedelta(days=90)
            start_date = start_dt.strftime('%Y-%m-%d')
            end_date = end_dt.strftime('%Y-%m-%d')

        # Get all breakout signals in date range (break_days validated above)
        signals = conn.execute(f"""
            SELECT b.stock_id, b.date as signal_date, b.close_price as entry_price,
                   s.name
            FROM breakouts b
            JOIN stocks s ON s.stock_id = b.stock_id
            WHERE b.date >= ? AND b.date <= ? AND b.break_{break_days} = 1
            ORDER BY b.date
        """, (start_date, end_date)).fetchall()

        results = []
        total_return = 0
        win_count = 0
        total_count = 0

        for sig in signals:
            future = conn.execute("""
                SELECT date, close_price FROM daily_prices
                WHERE stock_id = ? AND date > ?
                ORDER BY date LIMIT 1 OFFSET ?
            """, (sig['stock_id'], sig['signal_date'], hold_days - 1)).fetchone()

            if future:
                exit_price = future['close_price']
                ret = round((exit_price - sig['entry_price']) / sig['entry_price'] * 100, 2)
                total_return += ret
                total_count += 1
                if ret > 0:
                    win_count += 1
                results.append({
                    'stock_id': sig['stock_id'],
                    'name': sig['name'],
                    'signal_date': sig['signal_date'],
                    'entry_price': sig['entry_price'],
                    'exit_date': future['date'],
                    'exit_price': exit_price,
                    'return_pct': ret,
                })

        avg_return = round(total_return / total_count, 2) if total_count else 0
        win_rate = round(win_count / total_count * 100, 1) if total_count else 0

        # Group by month for chart
        monthly = {}
        for r in results:
            month = r['signal_date'][:7]
            if month not in monthly:
                monthly[month] = {'count': 0, 'total_ret': 0, 'wins': 0}
            monthly[month]['count'] += 1
            monthly[month]['total_ret'] += r['return_pct']
            if r['return_pct'] > 0:
                monthly[month]['wins'] += 1

        monthly_data = []
        for m in sorted(monthly.keys()):
            d = monthly[m]
            monthly_data.append({
                'month': m,
                'avg_return': round(d['total_ret'] / d['count'], 2),
                'win_rate': round(d['wins'] / d['count'] * 100, 1),
                'count': d['count'],
            })

        return render_template('backtest.html',
            results=results[-100:],
            total_count=total_count,
            avg_return=avg_return,
            win_rate=win_rate,
            win_count=win_count,
            monthly_data=monthly_data,
            monthly_json=json.dumps(monthly_data),
            break_days=break_days,
            hold_days=hold_days,
            start_date=start_date,
            end_date=end_date)
    finally:
        conn.close()


@app.route('/weekly')
def weekly():
    """研究週報頁面 — 自動彙整量化研究與科技研究週報。"""
    import glob, re

    base_src = os.path.join(os.path.dirname(__file__), '..', 'src')
    if not os.path.isdir(base_src):
        base_src = r'D:\claude\src'

    fin_lab = os.path.join(base_src, 'fin-lab')
    tech_research = os.path.join(base_src, 'tech-research')

    # ── 0. GiS 研究週報（D:\claude\GiS_研究週報_*.html / GiS_*.html）
    gis_reports = []
    gis_dir = os.path.join(os.path.dirname(__file__), '..')
    if os.path.isdir(gis_dir):
        for pattern in ['GiS_研究週報_*.html', 'GiS_*.html']:
            for f in sorted(glob.glob(os.path.join(gis_dir, pattern)), reverse=True):
                fname = os.path.basename(f)
                # 避免重複 & 排除樣板
                if any(g['filename'] == fname for g in gis_reports):
                    continue
                if '樣板' in fname or 'template' in fname.lower():
                    continue
                m = re.search(r'(\d{8})', fname)
                if m:
                    raw = m.group(1)
                    date_str = f'{raw[:4]}-{raw[4:6]}-{raw[6:]}'
                else:
                    mtime = os.path.getmtime(f)
                    date_str = datetime.fromtimestamp(mtime).strftime('%Y-%m-%d')
                # 從 HTML <title> 取標題
                title = fname.replace('.html', '')
                try:
                    with open(f, 'r', encoding='utf-8', errors='ignore') as fh:
                        head = fh.read(3000)
                    _m = re.search(r'<title[^>]*>([^<]+)</title>', head)
                    if _m:
                        t = _m.group(1).strip()
                        t = re.sub(r'\s*\|\s*GiS.*$', '', t)
                        if t:
                            title = t
                except Exception:
                    pass
                gis_reports.append({
                    'filename': fname,
                    'date': date_str,
                    'type': 'gis',
                    'title': title,
                })

    # ── 1. 量化研究週報（fin-lab/output/weekly-briefing-*.html）
    fin_briefings = []
    output_dir = os.path.join(fin_lab, 'output')
    if os.path.isdir(output_dir):
        for f in sorted(glob.glob(os.path.join(output_dir, 'weekly-briefing-*.html')), reverse=True):
            fname = os.path.basename(f)
            m = re.search(r'(\d{4}-\d{2}-\d{2})', fname)
            date_str = m.group(1) if m else ''
            fin_briefings.append({
                'filename': fname,
                'date': date_str,
                'type': 'fin',
                'title': f'量化研究週報 {date_str}',
            })

    # ── 1b. 金融科技分類報告（fin-lab/output/category-reports/*.html）
    cat_reports_dir = os.path.join(fin_lab, 'output', 'category-reports')
    cat_reports = {}  # {category: [reports]}

    # 檔名→分類 映射
    _CAT_MAP = {
        # 風險管理
        'regime-detector': '風險管理', 'garch-report': '風險管理', 'te-report': '風險管理',
        'entropy-report': '風險管理', 'km-report': '風險管理', 'risk-management_report': '風險管理',
        # 因子研究
        'blind-signal': '因子與策略', 'disagreement': '因子與策略', 'jf-ml-returns': '因子與策略',
        'raps-alpha-global': '因子與策略', 'nber-ai-pricing': '因子與策略',
        'nber-ml-markowitz': '因子與策略', 'report-factor': '因子與策略',
        'stat-arb_report': '因子與策略', 'fin-lab-reallife': '因子與策略',
        'finance-lab-briefing': '因子與策略',
        # 選擇權與波動率
        'oql-report': '選擇權與波動率', 'spx-vix': '選擇權與波動率', 'pinn-report': '選擇權與波動率',
        'options-volatility_report': '選擇權與波動率', 'tda-report': '選擇權與波動率',
        'quantum-report': '選擇權與波動率', 'diffusion-report': '選擇權與波動率',
        'report-regime-rl': '選擇權與波動率',
        # 情緒與 NLP
        'llm-screener': '情緒與 NLP', 'wti-report': '情緒與 NLP',
        'sentiment-nlp_report': '情緒與 NLP',
        # 總經與資產配置
        'rate-cycle': '總經與資產配置', 'mideast-war': '總經與資產配置',
        'FI-01': '總經與資產配置', 'FX-01': '總經與資產配置',
        'cross-market': '總經與資產配置', 'portfolio-optimization_report': '總經與資產配置',
        # 特殊主題
        'CQ-01': '特殊主題', 'ED-01': '特殊主題', 'ES-01': '特殊主題',
        'HF-01': '特殊主題', 'XA-01': '特殊主題', 'alternative-data_report': '特殊主題',
        'checkpoint': '特殊主題',
        # 量化研究
        'crypto-quant': '量化研究',
    }

    if os.path.isdir(cat_reports_dir):
        for f in sorted(glob.glob(os.path.join(cat_reports_dir, '*.html'))):
            fname = os.path.basename(f)
            title = fname.replace('.html', '').replace('-', ' ').replace('_', ' ')
            try:
                with open(f, 'r', encoding='utf-8', errors='ignore') as fh:
                    head = fh.read(3000)
                import re as _re
                m = _re.search(r'<title[^>]*>([^<]+)</title>', head)
                if m:
                    t = m.group(1).strip()
                    t = _re.sub(r'\s*\|\s*GiS.*$', '', t)
                    if t:
                        title = t
            except Exception:
                pass
            mtime = os.path.getmtime(f)
            date_str = datetime.fromtimestamp(mtime).strftime('%Y-%m-%d')

            # 分類
            cat = '其他'
            for prefix, c in _CAT_MAP.items():
                if fname.startswith(prefix) or prefix in fname:
                    cat = c
                    break

            cat_reports.setdefault(cat, []).append({
                'filename': fname,
                'date': date_str,
                'title': title,
            })

    # ── 2. 科技研究報告（tech-research/research-*/research-*.html）
    tech_reports = []
    if os.path.isdir(tech_research):
        for batch_dir in sorted(glob.glob(os.path.join(tech_research, 'research-*')), reverse=True):
            batch_name = os.path.basename(batch_dir)
            m = re.search(r'(\d{4}-\d{2}-\d{2})', batch_name)
            date_str = m.group(1) if m else ''
            for html_file in glob.glob(os.path.join(batch_dir, '*.html')):
                fname = os.path.basename(html_file)
                tech_reports.append({
                    'filename': f'{batch_name}/{fname}',
                    'date': date_str,
                    'type': 'tech',
                    'title': f'科技研究精選 {date_str}',
                })

    # ── 3. fin-lab 專案總覽
    projects = []
    categories = {}
    if os.path.isdir(fin_lab):
        for cat_dir in sorted(glob.glob(os.path.join(fin_lab, '*'))):
            cat_name = os.path.basename(cat_dir)
            if cat_name.startswith(('_', '.')) or cat_name in ('scripts', 'factor_data', 'qlib_data', '_meta', 'output'):
                continue
            if not os.path.isdir(cat_dir):
                continue
            for proj_dir in sorted(glob.glob(os.path.join(cat_dir, '[A-Z]*'))):
                proj_name = os.path.basename(proj_dir)
                has_py = len(glob.glob(os.path.join(proj_dir, '**', '*.py'), recursive=True)) > 0
                has_pdf = len(glob.glob(os.path.join(proj_dir, '**', '*.pdf'), recursive=True)) > 0
                has_json = len(glob.glob(os.path.join(proj_dir, '**', '*.json'), recursive=True)) > 0
                has_csv = len(glob.glob(os.path.join(proj_dir, '**', '*.csv'), recursive=True)) > 0
                code = proj_name.split('-')[0] if '-' in proj_name else proj_name[:5]
                display_name = '-'.join(proj_name.split('-')[1:]) if '-' in proj_name else proj_name
                proj = {
                    'code': code,
                    'name': display_name,
                    'category': cat_name,
                    'has_code': has_py,
                    'has_report': has_pdf,
                    'has_data': has_json or has_csv,
                }
                projects.append(proj)
                categories.setdefault(cat_name, []).append(proj)

    stats = {
        'total': len(projects),
        'with_code': sum(1 for p in projects if p['has_code']),
        'with_report': sum(1 for p in projects if p['has_report']),
        'categories': len(categories),
    }

    return render_template('weekly.html',
        gis_reports=gis_reports,
        fin_briefings=fin_briefings,
        cat_reports=cat_reports,
        tech_reports=tech_reports,
        projects=projects,
        categories=categories,
        stats=stats)


@app.route('/api/weekly/<path:filepath>')
def api_weekly_report(filepath):
    """動態載入週報 HTML 內容。"""
    import re
    base_src = os.path.join(os.path.dirname(__file__), '..', 'src')
    if not os.path.isdir(base_src):
        base_src = r'D:\claude\src'

    # 安全檢查
    if '..' in filepath:
        return 'Invalid', 400

    # GiS 研究週報
    if filepath.startswith('gis/'):
        fname = filepath[4:]
        if not re.match(r'^GiS[\w\-\u4e00-\u9fff]+\.html$', fname):
            return 'Invalid', 400
        gis_dir = os.path.join(os.path.dirname(__file__), '..')
        full_path = os.path.join(gis_dir, fname)
        allowed_root = gis_dir
    # fin-lab 週報
    elif filepath.startswith('fin/'):
        fname = filepath[4:]
        if not re.match(r'^weekly-briefing-\d{4}-\d{2}-\d{2}\.html$', fname):
            return 'Invalid', 400
        full_path = os.path.join(base_src, 'fin-lab', 'output', fname)
        allowed_root = os.path.join(base_src, 'fin-lab', 'output')
    # 金融科技分類報告
    elif filepath.startswith('cat/'):
        fname = filepath[4:]
        if not re.match(r'^[\w\-\.]+\.html$', fname):
            return 'Invalid', 400
        full_path = os.path.join(base_src, 'fin-lab', 'output', 'category-reports', fname)
        allowed_root = os.path.join(base_src, 'fin-lab', 'output', 'category-reports')
    # 科技研究報告
    elif filepath.startswith('tech/'):
        relpath = filepath[5:]
        if not re.match(r'^research-\d{4}-\d{2}-\d{2}/[\w\-\.]+\.html$', relpath):
            return 'Invalid', 400
        full_path = os.path.join(base_src, 'tech-research', relpath)
        allowed_root = os.path.join(base_src, 'tech-research')
    else:
        return 'Invalid', 400

    # 路徑遍歷防護: realpath 後必須位於白名單根目錄之下
    resolved = os.path.realpath(full_path)
    root_resolved = os.path.realpath(allowed_root)
    if not resolved.startswith(root_resolved + os.sep):
        return 'Forbidden', 403

    if not os.path.isfile(resolved):
        return 'Not found', 404
    with open(resolved, 'r', encoding='utf-8') as f:
        return f.read()


# ===== 盤中即時報價 =====
# 已改為由 watchdog 獨立啟動 realtime_worker.py 執行 (避免雙寫 DB 衝突)。
# 原本內嵌的 _realtime_background_loop / start_realtime_thread 已移除。


# ===== 盤中爆量預估 =====

def _load_volume_alert_cache():
    """讀 volume_anomaly_cache 單 row，回傳 (payload_dict, updated_at) 或 (None, None)"""
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT payload, updated_at FROM volume_anomaly_cache WHERE id = 1"
        ).fetchone()
        if not row:
            return None, None
        return json.loads(row['payload']), row['updated_at']
    except Exception as e:
        logger.error(f"volume_anomaly_cache 讀取失敗: {e}")
        return None, None
    finally:
        conn.close()


@app.route('/volume-alert')
def volume_alert():
    payload, updated_at = _load_volume_alert_cache()
    return render_template(
        'volume_alert.html',
        data=payload,
        updated_at=updated_at,
    )


@app.route('/api/volume-alert')
def api_volume_alert():
    payload, updated_at = _load_volume_alert_cache()
    if payload is None:
        return jsonify({'error': 'no cache yet', 'data': None, 'updated_at': None}), 200
    return jsonify({'data': payload, 'updated_at': updated_at})


@app.route('/api/volume-alert/trend')
def api_volume_alert_trend():
    """回傳今日 taiex_trend 全部 rows（依時間排序）"""
    from datetime import datetime as _dt
    today_str = _dt.now().strftime('%Y-%m-%d')
    conn = get_conn()
    try:
        rows = conn.execute("""
            SELECT snapshot_ts, minute_idx, rvol_forecast, forecast_eod_value,
                   level, ci_low, ci_high
            FROM taiex_trend
            WHERE snapshot_ts >= ?
            ORDER BY snapshot_ts ASC
        """, (today_str + ' 00:00:00',)).fetchall()
        data = [{
            'minute_idx': r['minute_idx'],
            'rvol': r['rvol_forecast'],
            'level': r['level'],
            'eod': r['forecast_eod_value'],
            'ci_low': r['ci_low'],
            'ci_high': r['ci_high'],
        } for r in rows]
        return jsonify({'data': data})
    except Exception as e:
        logger.error(f"taiex_trend 讀取失敗: {e}")
        return jsonify({'data': [], 'error': str(e)}), 200
    finally:
        conn.close()


if __name__ == '__main__':
    debug_mode = os.environ.get('FLASK_DEBUG') == '1'
    # 綁定 0.0.0.0 = 監聽所有網路介面，
    # 可同時由 localhost / 內網 IP / Tailscale IP 存取。
    # 可用環境變數 HOST / PORT 覆寫。
    host = os.environ.get('HOST', '0.0.0.0')
    port = int(os.environ.get('PORT', '5000'))
    app.run(debug=debug_mode, host=host, port=port)

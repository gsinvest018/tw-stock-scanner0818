"""產業研究與週報（自 app.py 拆出）"""
import sys
import os
import json
import time
import logging
import threading
import requests as http_requests
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
from config import load_dotenv, BASE_DIR
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
from scrapers.market import fetch_futures_oi, fetch_retail_ratio, fetch_put_call_ratio, _finmind_get
import sqlite3

import logging
logger = logging.getLogger(__name__)
from webapp.core import app, auth, limiter

RESEARCH_DIR = os.environ.get('RESEARCH_DIR') or os.path.join(BASE_DIR, 'research')

# 研究週報來源：RESEARCH_SRC_DIR 環境變數優先，否則用專案上層的 src/
def _weekly_src_dir():
    d = os.environ.get('RESEARCH_SRC_DIR', '')
    if d and os.path.isdir(d):
        return d
    return os.path.join(os.path.dirname(BASE_DIR), 'src')


# GiS 研究週報放在專案上層目錄（與專案資料夾同層的 GiS_*.html）
_GIS_DIR = os.path.dirname(BASE_DIR)


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


@app.route('/weekly')
def weekly():
    """研究週報頁面 — 自動彙整量化研究與科技研究週報。"""
    import glob, re

    base_src = _weekly_src_dir()

    fin_lab = os.path.join(base_src, 'fin-lab')
    tech_research = os.path.join(base_src, 'tech-research')

    # ── 0. GiS 研究週報（專案上層目錄的 GiS_研究週報_*.html / GiS_*.html）
    gis_reports = []
    gis_dir = _GIS_DIR
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
    base_src = _weekly_src_dir()

    # 安全檢查
    if '..' in filepath:
        return 'Invalid', 400

    # GiS 研究週報
    if filepath.startswith('gis/'):
        fname = filepath[4:]
        if not re.match(r'^GiS[\w\-\u4e00-\u9fff]+\.html$', fname):
            return 'Invalid', 400
        gis_dir = _GIS_DIR
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

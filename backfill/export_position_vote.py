"""把 position-vote 用到的八指標匯出成 CSV，並打包成 7z 給對方。

輸出位置: <專案根>/data/position_vote_export/
壓縮檔  : <專案根>/data/position_vote_indicators_{YYYYMMDD}.7z
"""
import os
import sys

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _PROJECT_ROOT)
os.chdir(_PROJECT_ROOT)
import csv
import os
import shutil
import subprocess
import sys
from datetime import datetime

from models.database import get_conn

OUT_DIR = os.path.join(_PROJECT_ROOT, 'data', 'position_vote_export')
ARCHIVE_DIR = os.path.join(_PROJECT_ROOT, 'data')


# ── breadth Layer 3 公式（複製自 scanners/breadth.py）──
def _adr(adv, dec):
    if dec == 0:
        return 10.0 if adv > 0 else 1.0
    return round(adv / dec, 3)

def _score(adr):
    if adr <= 0:
        return 0.0
    return round(min(adr / (adr + 1.0), 1.0), 4)

def _regime(adr):
    if adr > 2.0: return 'STRONG_BULL'
    if adr > 1.0: return 'BULL'
    if adr > 0.8: return 'NEUTRAL'
    if adr > 0.5: return 'BEAR'
    return 'CRASH'


def _write_csv(path, header, rows):
    with open(path, 'w', encoding='utf-8-sig', newline='') as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)


def export_regime(conn):
    rows = conn.execute("""
        SELECT date, recon_error, tau, regime
        FROM regime_history ORDER BY date
    """).fetchall()
    out = [(r['date'], r['recon_error'], r['tau'], r['regime']) for r in rows]
    _write_csv(os.path.join(OUT_DIR, '01_regime_AE.csv'),
               ['date', 'recon_error', 'tau', 'regime'], out)
    return len(out), out[0][0] if out else None, out[-1][0] if out else None


def export_credit(conn):
    rows = conn.execute("""
        SELECT date, hyg_shy_ratio, indicator_value, signal, spy_close, trend5d
        FROM credit_spread_history ORDER BY date
    """).fetchall()
    out = [(r['date'], r['hyg_shy_ratio'], r['indicator_value'], r['signal'],
            r['spy_close'], r['trend5d']) for r in rows]
    _write_csv(os.path.join(OUT_DIR, '02_credit_spread.csv'),
               ['date', 'hyg_shy_ratio', 'indicator_value', 'signal',
                'spy_close', 'trend5d'], out)
    return len(out), out[0][0] if out else None, out[-1][0] if out else None


def export_breadth(conn):
    """以單一 SQL 聚合算出全市場 (Layer 3) 廣度時序。"""
    rows = conn.execute("""
        SELECT
            dp.date,
            SUM(CASE WHEN dp.change_pct > 0.1 THEN 1 ELSE 0 END)  AS advancers,
            SUM(CASE WHEN dp.change_pct < -0.1 THEN 1 ELSE 0 END) AS decliners,
            SUM(CASE WHEN dp.change_pct BETWEEN -0.1 AND 0.1 THEN 1 ELSE 0 END) AS unchanged,
            SUM(CASE WHEN dp.change_pct >= 9.5 THEN 1 ELSE 0 END)  AS limit_up,
            SUM(CASE WHEN dp.change_pct <= -9.5 THEN 1 ELSE 0 END) AS limit_down
        FROM daily_prices dp
        WHERE dp.close_price IS NOT NULL AND dp.volume > 0
        GROUP BY dp.date
        ORDER BY dp.date
    """).fetchall()
    out = []
    for r in rows:
        adv, dec = r['advancers'], r['decliners']
        adr = _adr(adv, dec)
        out.append((r['date'], adv, dec, r['unchanged'],
                    r['limit_up'], r['limit_down'],
                    adr, _score(adr), _regime(adr)))
    _write_csv(os.path.join(OUT_DIR, '03_breadth_full_market.csv'),
               ['date', 'advancers', 'decliners', 'unchanged',
                'limit_up', 'limit_down', 'adr', 'score', 'regime'], out)
    return len(out), out[0][0] if out else None, out[-1][0] if out else None


def export_macro(conn, indicator, filename):
    rows = conn.execute("""
        SELECT date, value, signal FROM macro_indicators
        WHERE indicator = ? ORDER BY date
    """, (indicator,)).fetchall()
    out = [(r['date'], r['value'], r['signal']) for r in rows]
    _write_csv(os.path.join(OUT_DIR, filename),
               ['date', 'value', 'signal'], out)
    return len(out), out[0][0] if out else None, out[-1][0] if out else None


def write_readme(stats):
    lines = []
    today = datetime.now().strftime('%Y-%m-%d')
    lines.append(f'# Position-Vote 八指標資料集（匯出日 {today}）\n')
    lines.append('資料來源：tw-stock-scanner / db/scanner.db')
    lines.append('編碼：UTF-8 with BOM（Excel 直開不亂碼）\n')

    lines.append('## 檔案清單\n')
    lines.append('| 檔名 | 指標 | 維度 | 來源 | 筆數 | 起 ~ 迄 |')
    lines.append('|------|------|------|------|------|---------|')
    file_meta = [
        ('01_regime_AE.csv', 'AE 體制 (regime)', '股票異常', 'tw-stock-scanner Autoencoder（用現役模型對 SPY+VIX 歷史特徵回推；非 point-in-time）', 'regime'),
        ('02_credit_spread.csv', '信用利差 (credit)', '信用風險', 'Yahoo Finance: HYG / SHY / SPY', 'credit'),
        ('03_breadth_full_market.csv', '市場廣度 (breadth, Layer 3)', '台股內部', 'TWSE + TPEx 全市場日漲跌家數（自 daily_prices 聚合）', 'breadth'),
        ('04_T10Y3M.csv', '殖利率利差 10Y-3M', '景氣政策', 'FRED 系列 T10Y3M', 'T10Y3M'),
        ('05_CP_SPREAD.csv', 'CP-Treasury 資金壓力', '資金流動', 'FRED DCPF3M − DTB3', 'CP_SPREAD'),
        ('06_DOLLAR.csv', '美元指數 (DXY)', '資金流動', 'Yahoo DX-Y.NYB（FRED DTWEXBGS fallback）', 'DOLLAR'),
        ('07_COR3M_VIX.csv', 'VIX 系統性風險', '尾部波動', 'Yahoo ^VIX（FRED VIXCLS fallback）', 'COR3M'),
        ('08_MOVE.csv', 'MOVE 國債波動', '尾部波動', 'Yahoo ^MOVE', 'MOVE'),
    ]
    for fn, name, dim, src, key in file_meta:
        n, mn, mx = stats[key]
        lines.append(f'| `{fn}` | {name} | {dim} | {src} | {n:,} | {mn} ~ {mx} |')

    lines.append('\n## 欄位說明\n')
    lines.append('### 01_regime_AE.csv')
    lines.append('- `recon_error` Autoencoder 重構誤差')
    lines.append('- `tau` 動態門檻（rolling quantile）')
    lines.append('- `regime` `normal` 或 `abnormal`（recon_error > tau 視為 abnormal）')
    lines.append('\n### 02_credit_spread.csv')
    lines.append('- `hyg_shy_ratio` HYG / SHY 收盤比')
    lines.append('- `indicator_value` 189 日滾動百分位（反向，0~1，越高代表信用風險越高）')
    lines.append('- `signal` `GREEN` / `YELLOW` / `RED`')
    lines.append('- `spy_close` SPY 當日收盤；`trend5d` 5 日趨勢百分比')
    lines.append('\n### 03_breadth_full_market.csv')
    lines.append('- `advancers` / `decliners` / `unchanged` 上漲/下跌/平盤檔數（門檻 ±0.1%）')
    lines.append('- `limit_up` / `limit_down` 漲停/跌停（±9.5%）家數')
    lines.append('- `adr` advance/decline ratio = adv / dec')
    lines.append('- `score` adr / (adr + 1)')
    lines.append('- `regime` `STRONG_BULL` / `BULL` / `NEUTRAL` / `BEAR` / `CRASH`')
    lines.append('- 註：本檔僅含 Layer 3（全市場），完整 3 層投票需另跑 `scanners/breadth.py`')
    lines.append('\n### 04 ~ 08 macro_indicators.csv')
    lines.append('共同欄位 `date / value / signal`')
    lines.append('- `signal` `GREEN` / `YELLOW` / `RED`，分類規則見 `scanners/macro_indicators.py` THRESHOLDS')

    lines.append('\n## 資料完整度與限制\n')
    lines.append('| 指標 | 完整度 | 備註 |')
    lines.append('|------|--------|------|')
    lines.append('| credit | ✅ 完整 | Yahoo 提供 2007 起，已最完整 |')
    lines.append('| breadth | ✅ 完整 | TWSE/TPEx 自 2012-05-02 起回補完成 |')
    lines.append('| T10Y3M / CP_SPREAD / DOLLAR / COR3M | ✅ 已延長 | 2026-04-29 補抓至 2001-05 起 |')
    lines.append('| MOVE | ⚠ 部分 | Yahoo ^MOVE 僅提供 2002-11-12 起 |')
    lines.append('| regime | ⚠ Back-inference | 已用現役 AE 模型回推至 2001-04-06。**模型 τ 是用近 2 年訓練的**，套到歷史是「現在的模型回頭看歷史」的結果。可用於相關性分析、特徵跨期比較，**不可拿來嚴格回測**（look-ahead bias）。要 point-in-time 需要對每個歷史日期重新滾動訓練模型。 |')

    lines.append('\n## 加權與門檻（六指標投票，position_vote.py）\n')
    lines.append('- credit 15% / breadth 25% / T10Y3M 20% / CP_SPREAD 15% / DOLLAR 10% / COR3M 15%')
    lines.append('- 強制上限：credit RED → 50%；breadth CRASH → 30%；T10Y3M RED → 50%；CP_SPREAD RED → 40%')
    lines.append('- AE regime 與 MOVE 因共線性已排除於投票，僅保留於相關性分析。')

    with open(os.path.join(OUT_DIR, 'README.md'), 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines))


def main():
    if os.path.exists(OUT_DIR):
        shutil.rmtree(OUT_DIR)
    os.makedirs(OUT_DIR)

    conn = get_conn()
    print('=== 匯出 CSV ===')
    stats = {}
    stats['regime']    = export_regime(conn);   print(f'  01_regime_AE             {stats["regime"]}')
    stats['credit']    = export_credit(conn);   print(f'  02_credit_spread         {stats["credit"]}')
    stats['breadth']   = export_breadth(conn);  print(f'  03_breadth_full_market   {stats["breadth"]}')
    stats['T10Y3M']    = export_macro(conn, 'T10Y3M',    '04_T10Y3M.csv');    print(f'  04_T10Y3M                {stats["T10Y3M"]}')
    stats['CP_SPREAD'] = export_macro(conn, 'CP_SPREAD', '05_CP_SPREAD.csv'); print(f'  05_CP_SPREAD             {stats["CP_SPREAD"]}')
    stats['DOLLAR']    = export_macro(conn, 'DOLLAR',    '06_DOLLAR.csv');    print(f'  06_DOLLAR                {stats["DOLLAR"]}')
    stats['COR3M']     = export_macro(conn, 'COR3M',     '07_COR3M_VIX.csv'); print(f'  07_COR3M_VIX             {stats["COR3M"]}')
    stats['MOVE']      = export_macro(conn, 'MOVE',      '08_MOVE.csv');      print(f'  08_MOVE                  {stats["MOVE"]}')

    write_readme(stats)
    print('  README.md  已寫入')

    # ── 打包：有 7z 用 7z，否則用 ZIP ──
    import zipfile
    today = datetime.now().strftime('%Y%m%d')
    seven = shutil.which('7z') or shutil.which('7z.exe')
    print('\n=== 打包 ===')
    if seven:
        archive = os.path.join(ARCHIVE_DIR, f'position_vote_indicators_{today}.7z')
        if os.path.exists(archive):
            os.remove(archive)
        rc = subprocess.run([seven, 'a', '-t7z', '-mx=9', archive, OUT_DIR],
                            capture_output=True, text=True).returncode
        if rc != 0:
            print('  7z 失敗，改用 ZIP')
            seven = None
    if not seven:
        archive = os.path.join(ARCHIVE_DIR, f'position_vote_indicators_{today}.zip')
        if os.path.exists(archive):
            os.remove(archive)
        with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED, compresslevel=9) as z:
            for fn in sorted(os.listdir(OUT_DIR)):
                z.write(os.path.join(OUT_DIR, fn),
                        arcname=os.path.join('position_vote_indicators', fn))
    size_mb = os.path.getsize(archive) / 1024 / 1024
    print(f'  完成：{archive} ({size_mb:.2f} MB)')
    print(f'\n輸出資料夾：{OUT_DIR}')


if __name__ == '__main__':
    main()

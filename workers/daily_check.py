"""
每日 08:00 自動檢查：找出所有缺漏的交易日資料並補齊
- 國定假日 / 非交易日自動偵測（跳過）
- 資料有誤或補抓失敗會跳 Windows 通知
"""
import sys
import os
import logging
import subprocess
from datetime import datetime, timedelta

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _PROJECT_ROOT)
os.chdir(_PROJECT_ROOT)

from utils import acquire_lock, release_lock
from models.database import init_db, get_conn

logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] %(levelname)-5s %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
)
logger = logging.getLogger(__name__)

# 預期每天收盤價至少要有這麼多檔（低於此數視為不完整）
MIN_STOCKS = 1500


def win_notify(title, message):
    """發送 Windows 桌面通知"""
    try:
        ps_cmd = f'''
        [Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null
        $template = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02)
        $textNodes = $template.GetElementsByTagName("text")
        $textNodes.Item(0).AppendChild($template.CreateTextNode("{title}")) > $null
        $textNodes.Item(1).AppendChild($template.CreateTextNode("{message}")) > $null
        $toast = [Windows.UI.Notifications.ToastNotification]::new($template)
        [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("台股掃描器").Show($toast)
        '''
        subprocess.run(['powershell', '-Command', ps_cmd],
                      capture_output=True, timeout=10)
    except Exception:
        # 通知失敗不影響主流程
        pass


def is_trading_day(date_str):
    """
    檢查某日是否為交易日。
    方法：查 DB 是否有該日資料。如果 DB 沒有且是平日，嘗試抓 TWSE 看有沒有回傳。
    """
    # 週末一定不是交易日
    d = datetime.strptime(date_str, '%Y-%m-%d')
    if d.weekday() >= 5:
        return False
    return True  # 平日預設是交易日，抓不到資料再判斷


def is_holiday_by_twse(yyyymmdd):
    """嘗試抓 TWSE，如果回傳空資料表示非交易日（國定假日）"""
    import requests
    try:
        resp = requests.get(
            'https://www.twse.com.tw/exchangeReport/MI_INDEX',
            params={'response': 'json', 'date': yyyymmdd, 'type': 'ALL'},
            headers={'User-Agent': 'Mozilla/5.0'}, timeout=15)
        data = resp.json()
        if data.get('stat') != 'OK':
            return True  # 非交易日
        tables = data.get('tables', [])
        if not tables:
            return True
        best = max(tables, key=lambda t: len(t.get('data', [])))
        return len(best.get('data', [])) == 0
    except Exception:
        return False  # 抓取失敗不能確定，當作交易日處理


def get_missing_dates(conn, days_back=10):
    """檢查最近 N 天，找出缺漏或不完整的交易日"""
    today = datetime.now()
    missing = []
    incomplete = []

    for i in range(1, days_back + 1):
        d = today - timedelta(days=i)
        # 跳過週末
        if d.weekday() >= 5:
            continue
        ds = d.strftime('%Y-%m-%d')
        row = conn.execute(
            "SELECT COUNT(*) as c FROM daily_prices WHERE date=?", (ds,)
        ).fetchone()
        count = row['c']
        if count == 0:
            missing.append(ds)
        elif count < MIN_STOCKS:
            incomplete.append((ds, count))

    return missing, incomplete


def check_institutional(conn, date_str):
    """檢查某日法人資料是否存在"""
    row = conn.execute(
        "SELECT COUNT(*) as c FROM institutional WHERE date=?", (date_str,)
    ).fetchone()
    return row['c'] > 0


def check_broker(conn, date_str):
    """檢查某日分點資料是否存在"""
    row = conn.execute(
        "SELECT COUNT(DISTINCT stock_id) as c FROM broker_trades WHERE date=?", (date_str,)
    ).fetchone()
    return row['c'] > MIN_STOCKS


def main():
    try:
        init_db()
    except Exception as e:
        logger.error(f"資料庫初始化失敗: {e}")
        win_notify("台股掃描器 - 錯誤", f"資料庫初始化失敗: {e}")
        return

    conn = get_conn()
    errors = []  # 收集所有錯誤，最後一次通知

    # 1. 檢查收盤價缺漏
    logger.info("===== 每日資料完整性檢查 =====")
    missing, incomplete = get_missing_dates(conn, days_back=10)

    # 過濾掉國定假日
    real_missing = []
    for ds in missing:
        yyyymmdd = ds.replace('-', '')
        if is_holiday_by_twse(yyyymmdd):
            logger.info(f"{ds}: 非交易日（國定假日），跳過")
        else:
            real_missing.append(ds)
        import time
        time.sleep(3)  # 避免 TWSE 擋

    if real_missing:
        logger.info(f"缺漏交易日: {real_missing}")
    if incomplete:
        logger.info(f"不完整交易日: {incomplete}")

    if not real_missing and not incomplete:
        logger.info("收盤價：近 10 個交易日全部完整 ✓")

    # 2. 補抓缺漏的收盤價
    from run_daily import run_closing
    for ds in real_missing:
        try:
            yyyymmdd = ds.replace('-', '')
            logger.info(f"補抓收盤: {ds}")
            run_closing(yyyymmdd)
        except Exception as e:
            logger.error(f"補抓 {ds} 失敗: {e}")
            errors.append(f"收盤 {ds} 失敗")

    # 補抓不完整的
    for ds, count in incomplete:
        yyyymmdd = ds.replace('-', '')
        if is_holiday_by_twse(yyyymmdd):
            logger.info(f"{ds}: 非交易日，刪除不完整資料")
            conn.execute("DELETE FROM daily_prices WHERE date=?", (ds,))
            conn.execute("DELETE FROM breakouts WHERE date=?", (ds,))
            conn.commit()
            continue
        try:
            logger.info(f"重抓不完整資料: {ds} (僅 {count} 檔)")
            run_closing(yyyymmdd)
        except Exception as e:
            logger.error(f"重抓 {ds} 失敗: {e}")
            errors.append(f"重抓 {ds} 失敗")

    # 3. 檢查法人資料
    logger.info("--- 法人買賣超檢查 ---")
    from run_daily import run_institutional
    all_dates = conn.execute(
        "SELECT DISTINCT date FROM daily_prices WHERE date >= date('now', '-10 days') ORDER BY date"
    ).fetchall()
    for row in all_dates:
        ds = row['date']
        if not check_institutional(conn, ds):
            try:
                yyyymmdd = ds.replace('-', '')
                logger.info(f"補抓法人: {ds}")
                run_institutional(yyyymmdd)
            except Exception as e:
                logger.error(f"法人 {ds} 失敗: {e}")
                errors.append(f"法人 {ds} 失敗")
        else:
            logger.info(f"法人 {ds}: ✓")

    # 4. 檢查分點（最近 5 天，缺的自動補抓）
    logger.info("--- 券商分點檢查 ---")
    recent = conn.execute(
        "SELECT DISTINCT date FROM daily_prices ORDER BY date DESC LIMIT 5"
    ).fetchall()
    from run_daily import run_broker
    for row in recent:
        ds = row['date']
        if not check_broker(conn, ds):
            broker_count = conn.execute(
                "SELECT COUNT(DISTINCT stock_id) as c FROM broker_trades WHERE date=?", (ds,)
            ).fetchone()['c']
            logger.info(f"分點 {ds}: {broker_count} 檔 (不足，自動補抓)")
            try:
                yyyymmdd = ds.replace('-', '')
                run_broker(yyyymmdd)
                logger.info(f"分點 {ds}: 補抓完成 ✓")
            except Exception as e:
                logger.error(f"分點 {ds} 補抓失敗: {e}")
                errors.append(f"分點 {ds} 補抓失敗")
        else:
            logger.info(f"分點 {ds}: ✓")

    # 5. 重算漲跌幅（修正可能的錯誤）— 用 Python 批次計算取代慢速子查詢
    logger.info("--- 漲跌幅校正 ---")
    # Fetch recent prices (last 5 days + a few extra for prev-day lookup)
    rows_for_pct = conn.execute('''
        SELECT stock_id, date, close_price FROM daily_prices
        WHERE date >= date('now', '-10 days')
        ORDER BY stock_id, date
    ''').fetchall()

    # Build dict: {stock_id: [(date, close_price), ...]} sorted by date
    from collections import defaultdict
    stock_prices = defaultdict(list)
    for r in rows_for_pct:
        stock_prices[r['stock_id']].append((r['date'], r['close_price']))

    # Calculate change_pct in Python
    cutoff_row = conn.execute("SELECT date('now', '-5 days') as d").fetchone()
    cutoff_date = cutoff_row['d']

    updates = []
    for stock_id, prices in stock_prices.items():
        for i in range(1, len(prices)):
            curr_date, curr_close = prices[i]
            if curr_date < cutoff_date:
                continue
            prev_close = prices[i - 1][1]
            if prev_close and prev_close != 0:
                change_pct = round((curr_close - prev_close) / prev_close * 100, 2)
                updates.append((change_pct, stock_id, curr_date))

    if updates:
        conn.executemany(
            "UPDATE daily_prices SET change_pct = ? WHERE stock_id = ? AND date = ?",
            updates
        )
    conn.commit()
    logger.info(f"漲跌幅校正完成 ({len(updates)} 筆更新)")

    # 6. 預抓盤前籌碼
    logger.info("--- 盤前籌碼更新 ---")
    try:
        from scrapers.market import fetch_futures_oi, fetch_put_call_ratio
        fetch_futures_oi(days=20)
        fetch_put_call_ratio(days=20)
        logger.info("盤前籌碼: ✓")
    except Exception as e:
        logger.warning(f"盤前籌碼失敗（非致命）: {e}")

    # 7. 市場體溫更新（每日，SPY/VIX 基於美股資料）
    logger.info("--- 市場體溫更新 ---")
    try:
        from scanners.regime import update_regime_db, rolling_retrain
        result = update_regime_db(conn)
        conn.commit()
        logger.info(f"市場體溫: {result['temperature']}° ({result['regime']}) ✓")
    except ImportError:
        logger.warning("regime 模組未安裝，跳過")
    except Exception as e:
        logger.warning(f"市場體溫更新失敗（非致命）: {e}")
        errors.append(f"市場體溫: {e}")

    # 7c. 信用利差紅綠燈更新（每日，HYG/SHY 基於美股資料）
    logger.info("--- 信用利差更新 ---")
    try:
        from scanners.credit_spread import update_credit_spread_db
        cs_result = update_credit_spread_db(conn)
        logger.info(f"信用利差: {cs_result['signal']} ({cs_result['indicator_value']:.4f}) ✓")
    except Exception as e:
        logger.warning(f"信用利差更新失敗（非致命）: {e}")
        errors.append(f"信用利差: {e}")

    # 7c-2. SPY CTA 訊號更新（每日,Lasso 中頻日 K)
    logger.info("--- SPY CTA 訊號更新 ---")
    try:
        from scanners.cta_signal import update_cta_signal_db
        cta_result = update_cta_signal_db(conn)
        logger.info(f"SPY CTA: {cta_result['action']} signal {cta_result['signal']:+.6f} ({cta_result['rows_upserted']} rows) ✓")
    except Exception as e:
        logger.warning(f"SPY CTA 更新失敗（非致命）: {e}")
        errors.append(f"SPY CTA: {e}")

    # 7d. 宏觀指標更新（每日，T10Y3M / CP-Treasury / Dollar / VIX / MOVE，FRED+Yahoo）
    logger.info("--- 宏觀指標更新 ---")
    try:
        from scanners.macro_indicators import update_macro_indicators
        macro_result = update_macro_indicators(conn)
        logger.info(f"宏觀指標: {list(macro_result.keys())} ✓")
    except Exception as e:
        logger.warning(f"宏觀指標更新失敗（非致命）: {e}")
        errors.append(f"宏觀指標: {e}")

    # 7b. 模型滾動重訓練（每月 1 號）
    if datetime.now().day == 1:
        logger.info("--- 月度模型重訓練 ---")
        try:
            from scanners.regime import rolling_retrain
            retrain_result = rolling_retrain(window_years=2)
            logger.info(f"重訓練完成: τ {retrain_result['old_tau']:.4f} → {retrain_result['new_tau']:.4f}")
            # 重訓後立即更新 regime_history
            result = update_regime_db(conn)
            conn.commit()
            logger.info(f"重訓後體溫: {result['temperature']}° ({result['regime']})")
        except Exception as e:
            logger.warning(f"模型重訓練失敗（非致命）: {e}")

    # 8. Weekly sector update + VACUUM (Mondays only)
    if datetime.now().weekday() == 0:  # Monday
        logger.info("--- 產業分類更新（週一） ---")
        try:
            from webapp.shared import populate_sectors
            populate_sectors()
            logger.info("產業分類: 更新完成 ✓")
        except Exception as e:
            logger.warning(f"產業分類更新失敗（非致命）: {e}")

        logger.info("Weekly VACUUM...")
        conn.execute("VACUUM")
        logger.info("VACUUM complete")

    conn.close()

    # 8. 發送通知
    if errors:
        msg = "以下資料可能有誤：\n" + "\n".join(errors)
        logger.warning(f"發送錯誤通知: {msg}")
        win_notify("台股掃描器 - 資料警告", msg)
    else:
        logger.info("所有資料正常 ✓")
        win_notify("台股掃描器", "每日檢查完成，所有資料正常 ✓")

    logger.info("===== 檢查完成 =====")


if __name__ == '__main__':
    if not acquire_lock('daily_check'):
        print("Another instance is running, skipping.")
        sys.exit(0)
    try:
        main()
    finally:
        release_lock('daily_check')

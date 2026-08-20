"""歷史回灌 AE regime（back-inference，非 point-in-time）。

策略：
- 使用「現役」AE 模型 + 現役 τ
- 抓 SPY+VIX 25 年歷史 → 算特徵 → expanding z-score → 過模型 → 重構誤差
- 寫入 regime_history（覆蓋舊值）

重要免責：
- 模型是用近 2 年資料訓練的，τ 也是近 2 年校準
- 對更早期的資料，這只是「現在的模型回頭看歷史」的結果
- 不可拿來嚴格回測（會有 look-ahead bias）
- 但對相關性分析、特徵跨期比較是合理的
"""
import logging
import sys

from models.database import get_conn, upsert_regime

try:
    from scanners.regime import get_market_temperature
except ImportError:
    sys.exit('找不到 regime-detector 外部專案（可設 REGIME_DETECTOR_DIR 環境變數指定位置），無法回灌 regime')

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger(__name__)

LOOKBACK_DAYS = 25 * 365  # ~25 年


def main():
    conn = get_conn()
    r = conn.execute("SELECT COUNT(*) n, MIN(date) mn, MAX(date) mx FROM regime_history").fetchone()
    print(f'回灌前  n={r["n"]}  {r["mn"]} ~ {r["mx"]}')

    print(f'\n抓 SPY+VIX 約 {LOOKBACK_DAYS//365} 年，過 AE 模型推論...')
    result = get_market_temperature(lookback_days=LOOKBACK_DAYS)

    print(f'  τ (現役門檻)    = {result["tau"]:.4f}')
    print(f'  最早日 / 最新日 = {result["history"][0]["date"]} / {result["history"][-1]["date"]}')
    print(f'  總筆數          = {len(result["history"])}')

    n_normal = sum(1 for h in result['history'] if h['regime'] == 'normal')
    n_abnormal = len(result['history']) - n_normal
    print(f'  分布            = normal {n_normal} / abnormal {n_abnormal}')

    print('\n寫入 regime_history（覆蓋舊值）...')
    for item in result['history']:
        upsert_regime(conn, item['date'], item['error'], result['tau'], item['regime'])
    conn.commit()

    r = conn.execute("SELECT COUNT(*) n, MIN(date) mn, MAX(date) mx FROM regime_history").fetchone()
    print(f'\n回灌後  n={r["n"]}  {r["mn"]} ~ {r["mx"]}')


if __name__ == '__main__':
    main()

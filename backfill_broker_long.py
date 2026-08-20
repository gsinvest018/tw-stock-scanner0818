"""
擴展版券商分點回補 — 對 daily_prices 中近 N 個交易日（預設 250）回補缺失的 broker_trades
富邦來源能拓到約 2021-12，再往前無資料

特性：
- 從近到遠跑（最有用的資料先 backfill）
- 跳過 broker_trades 已有的日期
- 0.5s/股票，每日 ~17 分鐘
"""
import os, sys, logging, time, sqlite3
from datetime import datetime, timedelta

sys.path.insert(0, r'D:\claude\tw-stock-scanner')
os.chdir(r'D:\claude\tw-stock-scanner')

DAYS = 250
for arg in sys.argv[1:]:
    if arg.isdigit():
        DAYS = int(arg)

os.makedirs('log', exist_ok=True)
log_file = f"log/{datetime.now().strftime('%Y%m%d-%H%M%S')}-backfill-broker-long.log"
logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] %(levelname)-5s %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(log_file, encoding='utf-8'),
        logging.FileHandler('backfill_broker.log', encoding='utf-8', mode='a'),
    ],
)
logger = logging.getLogger()

from models.database import get_conn
from run_daily import run_broker

conn = sqlite3.connect('db/scanner.db')
conn.row_factory = sqlite3.Row
existing = set(r['date'] for r in conn.execute('SELECT DISTINCT date FROM broker_trades').fetchall())
all_days = [r['date'] for r in conn.execute(
    f'SELECT DISTINCT date FROM daily_prices ORDER BY date DESC LIMIT {DAYS}'
).fetchall()]
missing = sorted([d for d in all_days if d not in existing], reverse=True)
conn.close()

logger.info("=" * 60)
logger.info(f"券商分點長期回補 — 範圍近 {DAYS} 個交易日")
logger.info(f"  daily_prices 範圍：共 {len(all_days)} 天")
logger.info(f"  已有 broker：{len(set(all_days) & existing)} 天")
logger.info(f"  待補：{len(missing)} 天")
eta_min = len(missing) * 17
logger.info(f"  預估耗時：{eta_min} 分鐘 ({eta_min/60:.1f} 小時)")
logger.info("=" * 60)

t0 = time.time()
ok = 0
fail = 0
for i, d in enumerate(missing, 1):
    date_str = d.replace('-', '')
    elapsed = time.time() - t0
    rate = i / elapsed if elapsed > 0 else 0
    eta = (len(missing) - i) / rate if rate > 0 else 0
    logger.info(f"[{i}/{len(missing)}] {d}  累計 ok={ok} fail={fail}  ETA {timedelta(seconds=int(eta))}")
    try:
        run_broker(date_str)
        ok += 1
    except Exception as e:
        fail += 1
        logger.error(f"{d} 失敗: {e}")

elapsed = timedelta(seconds=int(time.time() - t0))
logger.info("=" * 60)
logger.info(f"完成 — 成功 {ok} 天 / 失敗 {fail} 天 / 耗時 {elapsed}")
logger.info("=" * 60)

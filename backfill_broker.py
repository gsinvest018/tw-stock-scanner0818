"""Backfill broker_trades for missing dates (last 30 trading days)"""
import sys, os, logging, sqlite3
sys.path.insert(0, r'D:\claude\tw-stock-scanner')
os.chdir(r'D:\claude\tw-stock-scanner')

logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] %(levelname)-5s %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler('backfill_broker.log', encoding='utf-8'),
    ],
)
logger = logging.getLogger()

from models.database import get_conn
from run_daily import run_broker

conn = sqlite3.connect('db/scanner.db')
conn.row_factory = sqlite3.Row
existing = set(r['date'] for r in conn.execute('SELECT DISTINCT date FROM broker_trades').fetchall())
all_days = [r['date'] for r in conn.execute('SELECT DISTINCT date FROM daily_prices ORDER BY date DESC LIMIT 30').fetchall()]
missing = sorted([d for d in all_days if d not in existing])
conn.close()

logger.info(f'Broker backfill: {len(missing)} days to process')
for i, d in enumerate(missing, 1):
    date_str = d.replace('-', '')
    logger.info(f'[{i}/{len(missing)}] Processing broker for {d}...')
    try:
        run_broker(date_str)
        logger.info(f'[{i}/{len(missing)}] {d} done')
    except Exception as e:
        logger.error(f'Failed {d}: {e}')
logger.info('Broker backfill complete!')

import os
import logging

logger = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, 'db', 'scanner.db')


# ===== 載入 .env (若存在)，不依賴外部套件；不覆蓋既有環境變數 =====
def load_dotenv():
    env_path = os.path.join(BASE_DIR, '.env')
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


load_dotenv()

# TWSE 上市
TWSE_DAILY_URL = 'https://www.twse.com.tw/exchangeReport/MI_INDEX'
TWSE_INSTITUTIONAL_URL = 'https://www.twse.com.tw/fund/T86'

# TPEx 上櫃
TPEX_DAILY_URL = 'https://www.tpex.org.tw/web/stock/aftertrading/otc_quotes_no1430/stk_wn1430_result.php'
TPEX_INSTITUTIONAL_URL = 'https://www.tpex.org.tw/web/stock/3insti/daily_trade/3itrade_hedge_result.php'

# 突破天數
BREAKOUT_DAYS = [5, 10, 20, 60, 120, 240]

# 請求設定（瀏覽器化以降低被擋機率）
REQUEST_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36',
    'Accept': 'application/json, text/javascript, */*; q=0.01',
    'Accept-Language': 'zh-TW,zh;q=0.9,en-US;q=0.8,en;q=0.7',
    'Referer': 'https://mis.twse.com.tw/stock/index.jsp',
    'X-Requested-With': 'XMLHttpRequest',
}
REQUEST_TIMEOUT = 30
REQUEST_RETRY = 3
REQUEST_RETRY_DELAY = 30

# 群益個股期報價服務（本機常駐服務，提供個股期即時報價）
CAPITAL_QUOTE_URL = os.environ.get('CAPITAL_QUOTE_URL', 'http://127.0.0.1:8891')

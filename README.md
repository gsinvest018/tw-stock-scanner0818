# tw-stock-scanner — 台股掃描與籌碼分析網站

單機自架的台股掃描器：Flask + SQLite，跑在 Windows 上，盤中由 worker 自動掃描、盤後由工作排程器分時抓資料，提供 30+ 個分析頁面——突破選股、三大法人、券商分點、盤中爆量預估，以及自創的市場體溫（AE regime）、持股水位投票與去槓桿壓力指數。

## 架構

```
scrapers/  →  db/scanner.db  →  scanners/  →  app.py  →  templates/
（抓公開資料）  （SQLite, WAL）   （計算/判定）  （頁面+API）  （Bootstrap 前端）
```

排程層：`run_daily.py`（無狀態 CLI，由 Windows 工作排程器分時觸發）＋ 兩支盤中 worker（`volume_alert_worker.py` 每 5 分鐘、`realtime_worker.py` 每 10 分鐘）＋ `watchdog.py`（每 30 秒守護 Flask / ngrok / worker）。

## 主要功能頁面

| 分類 | 頁面 |
|---|---|
| 選股 | `/breakout` 突破掃描、`/screener` 多條件選股、`/backtest` 回測、`/watchlist` 自選股 |
| 籌碼 | `/institutional` 三大法人、`/consecutive` 連續買賣超、`/broker` 券商分點、`/market` 大盤籌碼、`/report` 盤前盤後報告 |
| 市場狀態 | `/regime` 市場體溫、`/position-vote` 持股水位投票、`/breadth` 市場廣度、`/deleveraging` 去槓桿壓力、`/margin-maintenance` 融資維持率、`/credit-spread` 信用利差 |
| 衍生品/盤中 | `/option-sr` 選擇權支撐壓力、`/futures-basis` 期現價差、`/te-tf-strength` 電金強弱、`/volume-alert` 盤中爆量預估 |

健康檢查：`/api/health`（免驗證）、`/data-health` 資料健康儀表板。

## 安裝

需求：Windows 10/11、Python 3.10+。

```bat
git clone <this repo>
cd tw-stock-scanner
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
copy .env.example .env      &:: 編輯 .env 設定帳密
.venv\Scripts\python -c "from models.database import init_db; init_db()"
.venv\Scripts\python run_daily.py 20260801 20260818   &:: 回補近期行情（示例）
start.bat                   &:: 啟動網站並開瀏覽器
```

全歷史回補（可中斷續跑）：`backfill_daily.py`（行情，2004 起）、`backfill_institutional.py`（法人，約 6 小時）、其餘見 `backfill_*.py` 檔頭說明。

## 環境變數

見 `.env.example`。必設：`SCANNER_USER` / `SCANNER_PASS`（Basic Auth；兩者皆空 = 停用驗證，僅限本機開發）。選填：`HOST`、`PORT`、`NGROK_EXE`、`NGROK_DOMAIN`、`BROKER_REPORTS_DIR`、`REGIME_DETECTOR_DIR`、`RESEARCH_SRC_DIR`、`CAPITAL_QUOTE_URL`。

## 每日排程（Windows 工作排程器）

| 時間 | 指令 |
|---|---|
| 08:00 | `python daily_check.py`（資料完整性健檢＋補抓） |
| 08:55 / 09:00 / 13:30 | volume-alert 盤前健檢 → 啟動 worker → 安全網收尾 |
| 14:00 | `python run_daily.py`（收盤行情＋突破） |
| 15:30 / 15:40 | `run_daily.py option` / `largetrader`、`deleveraging` |
| 17:00 / 18:00 | `run_daily.py market` / `institutional` |
| 20:00 | `run_daily.py broker` |

常駐與開機自啟：`launch.bat`（detached 啟動 watchdog）、`autostart.bat`（登入時自啟）。

## 資料來源

全部為免 API key 的公開端點：TWSE / TPEx（行情、法人、公告、融資）、TWSE MIS（盤中報價，含跨行程熔斷器保護）、TAIFEX（選擇權 OI、期貨大戶）、TDCC（集保股權分散）、FinMind（期權法人籌碼）、富邦 e 券商（分點）、Yahoo Finance / FRED（美股與宏觀指標）。使用時請節制請求頻率、遵守各站服務條款。

## 選用外部依賴

- **regime-detector**（`/regime` 市場體溫）：獨立專案，需另行取得並以 `REGIME_DETECTOR_DIR` 指定位置；缺少時該頁降級顯示。
- **ngrok**（對外通道）：未安裝時 watchdog 自動跳過。
- **pCloud P: 磁碟**（`/broker-reports`）：未掛載時該頁降級顯示。

## 測試

```bat
.venv\Scripts\pip install -r requirements-dev.txt
.venv\Scripts\python -m pytest tests\test_margin_maintenance.py tests\test_volume_anomaly.py   &:: 無網路依賴
.venv\Scripts\python -m pytest tests\                                                          &:: 全部（部分需 DB 資料）
```

## 免責聲明

本工具為內部研究用途，不保證數據準確性，所有投資決策請自行負責。

"""
守護程序：管理 Flask + ngrok + 即時報價 worker
每 30 秒檢查，掛了自動重啟
子進程用 DETACHED_PROCESS 啟動，不依附 watchdog
"""
import subprocess
import sys
import time
import os
import shutil
import logging
from datetime import datetime
import requests

LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'watchdog.log')

logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] %(levelname)-5s %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(LOG_FILE, encoding='utf-8'),
    ],
)
logger = logging.getLogger(__name__)

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PROJECT_DIR)
from config import load_dotenv  # noqa: E402
load_dotenv()

PYTHON = sys.executable
NGROK = os.environ.get("NGROK_EXE", "ngrok")  # 未設定時從 PATH 尋找
NGROK_DOMAIN = os.environ.get("NGROK_DOMAIN", "")  # 空值 = ngrok 隨機網址
NGROK_ENABLED = shutil.which(NGROK) is not None

# DETACHED_PROCESS: 子進程完全獨立，watchdog 死了也不影響
DETACHED = 0x00000008


def is_flask_running():
    try:
        r = requests.get("http://127.0.0.1:5000/", timeout=5, allow_redirects=True)
        return r.status_code in (200, 302)
    except Exception:
        return False


def is_ngrok_running():
    try:
        r = requests.get("http://127.0.0.1:4040/api/tunnels", timeout=5)
        data = r.json()
        return len(data.get('tunnels', [])) > 0
    except Exception:
        return False


_flask_starting = False  # 防止重複啟動


def start_flask():
    global _flask_starting
    if _flask_starting:
        return

    # 快速檢查 port 5000 是否已被佔用
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        if s.connect_ex(('127.0.0.1', 5000)) == 0:
            logger.info("Port 5000 已被佔用，跳過啟動")
            return

    _flask_starting = True
    try:
        logger.info("啟動 Flask...")
        env = os.environ.copy()
        env['WATCHDOG_MANAGED'] = '1'
        subprocess.Popen(
            [PYTHON, "run_server.py"],
            cwd=PROJECT_DIR,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=DETACHED,
        )
        # 等 Flask 完全就緒才放行
        for i in range(15):
            time.sleep(1)
            if is_flask_running():
                logger.info("Flask 啟動成功 (port 5000)")
                return
        logger.error("Flask 啟動失敗（15 秒超時）")
    finally:
        _flask_starting = False


def start_ngrok():
    logger.info("啟動 ngrok...")
    kill_ngrok()
    time.sleep(2)
    cmd = [NGROK, "http", "5000"]
    if NGROK_DOMAIN:
        cmd = [NGROK, "http", f"--url={NGROK_DOMAIN}", "5000"]
    subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=DETACHED,
    )
    time.sleep(5)
    if is_ngrok_running():
        logger.info(f"ngrok 啟動成功: https://{NGROK_DOMAIN or '(隨機網址，見 http://127.0.0.1:4040)'}")
    else:
        logger.error("ngrok 啟動失敗")


def start_realtime_worker():
    logger.info("啟動即時報價 worker...")
    subprocess.Popen(
        [PYTHON, "realtime_worker.py"],
        cwd=PROJECT_DIR,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=DETACHED,
    )
    logger.info("即時報價 worker 已啟動")


def start_volume_alert_worker():
    logger.info("啟動爆量預估 worker...")
    subprocess.Popen(
        [PYTHON, "volume_alert_worker.py"],
        cwd=PROJECT_DIR,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=DETACHED,
    )
    logger.info("爆量預估 worker 已啟動")


def kill_ngrok():
    try:
        subprocess.run(["taskkill", "/F", "/IM", "ngrok.exe"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


def _is_realtime_running():
    """檢查 realtime_worker 進程是否存在"""
    try:
        r = subprocess.run(
            ["powershell", "-Command",
             "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | Where-Object { $_.CommandLine -like '*realtime_worker*' } | Measure-Object | Select-Object -ExpandProperty Count"],
            capture_output=True, text=True, timeout=10)
        return int(r.stdout.strip() or '0') > 0
    except Exception:
        return False


def _in_volume_alert_window():
    """volume_alert worker 該運行的時段：平日 09:00–13:30"""
    now = datetime.now()
    if now.weekday() >= 5:
        return False
    hm = now.hour * 100 + now.minute
    return 900 <= hm < 1330


def _is_volume_alert_running():
    """檢查 volume_alert_worker 進程是否存在"""
    try:
        r = subprocess.run(
            ["powershell", "-Command",
             "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | Where-Object { $_.CommandLine -like '*volume_alert_worker*' } | Measure-Object | Select-Object -ExpandProperty Count"],
            capture_output=True, text=True, timeout=10)
        return int(r.stdout.strip() or '0') > 0
    except Exception:
        return False


def main():
    logger.info("=== 守護程序啟動 ===")

    if not is_flask_running():
        start_flask()
    else:
        logger.info("Flask 已在運行")

    if not NGROK_ENABLED:
        logger.info("找不到 ngrok 執行檔（NGROK_EXE 未設定且 PATH 查無），略過對外通道")
    elif not is_ngrok_running():
        start_ngrok()
    else:
        logger.info("ngrok 已在運行")

    if not _is_realtime_running():
        start_realtime_worker()

    if _in_volume_alert_window() and not _is_volume_alert_running():
        start_volume_alert_worker()

    ngrok_fail_count = 0
    realtime_check_counter = 0

    while True:
        time.sleep(30)
        try:
            if not is_flask_running():
                logger.warning("Flask 掛了，重啟...")
                start_flask()

            if NGROK_ENABLED and not is_ngrok_running():
                ngrok_fail_count += 1
                if ngrok_fail_count >= 2:
                    logger.warning(f"ngrok 掛了（連續 {ngrok_fail_count} 次），重啟...")
                    start_ngrok()
                    ngrok_fail_count = 0
            else:
                ngrok_fail_count = 0

            # 每 5 分鐘檢查一次 worker
            realtime_check_counter += 1
            if realtime_check_counter >= 10:
                realtime_check_counter = 0
                if not _is_realtime_running():
                    logger.warning("即時報價 worker 掛了，重啟...")
                    start_realtime_worker()
                if _in_volume_alert_window() and not _is_volume_alert_running():
                    logger.warning("爆量預估 worker 掛了，重啟...")
                    start_volume_alert_worker()

        except Exception as e:
            logger.error(f"監控迴圈例外（不中斷）: {e}")


if __name__ == '__main__':
    while True:
        try:
            main()
        except KeyboardInterrupt:
            logger.info("收到 Ctrl+C，守護程序結束")
            break
        except Exception as e:
            logger.error(f"守護程序異常，10 秒後重啟: {e}")
            time.sleep(10)

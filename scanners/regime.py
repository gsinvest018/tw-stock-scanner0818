"""市場體溫掃描器 — 基於 AE 體制偵測。"""
import os
import sys
import numpy as np
import pandas as pd

# 加入 regime-detector 路徑：優先環境變數，其次專案同層的 src/regime-detector
# tw-stock-scanner/scanners/regime.py → tw-stock-scanner/ → 上層/
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))        # scanners/
_SCANNER_ROOT = os.path.dirname(_THIS_DIR)                     # tw-stock-scanner/
_PARENT_ROOT = os.path.dirname(_SCANNER_ROOT)                  # 上層目錄
REGIME_DIR = os.environ.get('REGIME_DETECTOR_DIR', '')
if not REGIME_DIR or not os.path.isdir(REGIME_DIR):
    REGIME_DIR = os.path.join(_PARENT_ROOT, 'src', 'regime-detector')
if not os.path.isdir(REGIME_DIR):
    # fallback: 原開發機的絕對路徑
    REGIME_DIR = r'D:\claude\src\regime-detector'
sys.path.insert(0, REGIME_DIR)

from regime_detector.data import fetch_ohlcv, fetch_vix, compute_features, expanding_zscore
from regime_detector.model import (
    Autoencoder, load_model, compute_reconstruction_error,
    train_autoencoder, save_model,
)
from regime_detector.config import load_config as load_regime_config


# 模型路徑
MODEL_PATH = os.path.join(REGIME_DIR, 'models', 'autoencoder.pt')
CONFIG_PATH = os.path.join(REGIME_DIR, 'config.yaml')


def get_market_temperature(lookback_days=120):
    """計算最近 lookback_days 天的市場體溫。

    Returns:
        dict: {
            'current_error': float,  # 今天的重建誤差
            'tau': float,            # 閾值
            'regime': str,           # 'normal' or 'abnormal'
            'temperature': float,    # 0~100 的體溫值 (error/tau * 50, 上限100)
            'history': list[dict],   # [{date, error, regime}, ...]
        }
    """
    from datetime import datetime, timedelta

    end = datetime.now().strftime('%Y-%m-%d')
    # 多抓一些天以確保 expanding zscore 後有足夠資料
    start = (datetime.now() - timedelta(days=lookback_days + 200)).strftime('%Y-%m-%d')

    # 擷取資料
    ohlcv = fetch_ohlcv('SPY', start, end)
    vix = fetch_vix(start, end)
    feats = compute_features(ohlcv)
    feats_z = expanding_zscore(feats, min_periods=60)

    # VIX 對齊
    vix_aligned = vix.reindex(feats_z.index, method='ffill').dropna()
    feats_z = feats_z.loc[vix_aligned.index]

    # 載入模型
    model, tau = load_model(MODEL_PATH)

    # 計算重建誤差
    errors = compute_reconstruction_error(model, feats_z)

    # 取最近 lookback_days 天
    errors_recent = errors.tail(lookback_days)

    # 當前狀態
    current_error = float(errors_recent.iloc[-1])
    regime = 'abnormal' if current_error >= tau else 'normal'
    temperature = min(100.0, (current_error / tau) * 50)

    # 歷史資料
    history = []
    for date, err in errors_recent.items():
        history.append({
            'date': date.strftime('%Y-%m-%d'),
            'error': float(err),
            'regime': 'abnormal' if float(err) >= tau else 'normal',
        })

    return {
        'current_error': current_error,
        'tau': tau,
        'regime': regime,
        'temperature': round(temperature, 1),
        'history': history,
        'latest_date': history[-1]['date'] if history else None,
    }


def rolling_retrain(window_years=2):
    """滾動重訓練：用最近 window_years 年的資料重新訓練模型和閾值 τ。

    Returns:
        dict: {
            'old_tau': float,
            'new_tau': float,
            'train_samples': int,
            'final_loss': float,
            'train_start': str,
            'train_end': str,
        }
    """
    from datetime import datetime, timedelta

    end = datetime.now().strftime('%Y-%m-%d')
    start = (datetime.now() - timedelta(days=window_years * 365)).strftime('%Y-%m-%d')

    # 載入舊模型取得舊 τ
    old_tau = 0.0
    try:
        _, old_tau = load_model(MODEL_PATH)
    except FileNotFoundError:
        pass

    # 準備新資料
    ohlcv = fetch_ohlcv('SPY', start, end)
    vix = fetch_vix(start, end)
    feats = compute_features(ohlcv)
    feats_z = expanding_zscore(feats, min_periods=60)

    vix_aligned = vix.reindex(feats_z.index, method='ffill').dropna()
    feats_z = feats_z.loc[vix_aligned.index]

    # 載入配置
    config = load_regime_config(CONFIG_PATH)

    # 訓練新模型
    model, new_tau = train_autoencoder(feats_z, vix_aligned, config)

    # 備份舊模型，儲存新模型
    import shutil
    backup_path = MODEL_PATH.replace('.pt', '_backup.pt')
    if os.path.exists(MODEL_PATH):
        shutil.copy2(MODEL_PATH, backup_path)
    save_model(model, new_tau, MODEL_PATH)

    return {
        'old_tau': old_tau,
        'new_tau': new_tau,
        'train_samples': len(feats_z),
        'train_start': feats_z.index[0].strftime('%Y-%m-%d'),
        'train_end': feats_z.index[-1].strftime('%Y-%m-%d'),
    }


def get_model_info():
    """取得目前模型的資訊。"""
    info = {
        'model_exists': os.path.exists(MODEL_PATH),
        'model_path': MODEL_PATH,
    }
    if info['model_exists']:
        import time as _time
        mtime = os.path.getmtime(MODEL_PATH)
        info['last_trained'] = pd.Timestamp(mtime, unit='s').strftime('%Y-%m-%d %H:%M')
        try:
            _, tau = load_model(MODEL_PATH)
            info['tau'] = tau
        except Exception:
            info['tau'] = None
    return info


def update_regime_db(conn):
    """
    更新 regime_history 資料庫。
    SPY/VIX 基於美股，美股假日時 Yahoo Finance 會回傳到前一個交易日的資料，
    不會產生錯誤，只是當天不會有新的一筆。
    """
    from models.database import upsert_regime
    import logging as _log

    try:
        result = get_market_temperature(lookback_days=30)
    except Exception as e:
        _log.getLogger(__name__).warning(f"市場體溫計算失敗（可能 Yahoo API 暫時不可用）: {e}")
        raise

    for item in result['history']:
        upsert_regime(conn, item['date'], item['error'], result['tau'], item['regime'])

    return result

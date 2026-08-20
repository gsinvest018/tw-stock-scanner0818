"""Stable server launcher - no debug, no reloader"""
import sys, os

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PROJECT_DIR)
os.chdir(PROJECT_DIR)

from app import app
# 綁定 0.0.0.0 = 監聽所有網路介面，
# 可同時由 localhost / 內網 IP / Tailscale IP 存取。
host = os.environ.get('HOST', '0.0.0.0')
port = int(os.environ.get('PORT', '5000'))
app.run(debug=False, host=host, port=port, use_reloader=False)

"""
Keepalive daemon: monitors Flask and ngrok, auto-restarts if either dies.
Run this once and it keeps both services alive indefinitely.
"""
import subprocess
import time
import sys
import os
import signal
import urllib.request

PROJECT_DIR = r'D:\claude\tw-stock-scanner'
PYTHON = r'C:\Users\User\AppData\Local\Programs\Python\Python312\python.exe'
FLASK_PORT = 5000
CHECK_INTERVAL = 30  # seconds

flask_proc = None
ngrok_proc = None


def is_flask_alive():
    try:
        req = urllib.request.urlopen(f'http://127.0.0.1:{FLASK_PORT}/', timeout=5)
        return req.status in (200, 302)
    except Exception:
        return False


def is_ngrok_alive():
    try:
        req = urllib.request.urlopen('http://127.0.0.1:4040/api/tunnels', timeout=5)
        return req.status == 200
    except Exception:
        return False


def start_flask():
    global flask_proc
    if flask_proc and flask_proc.poll() is None:
        flask_proc.terminate()
        flask_proc.wait(timeout=5)
    flask_proc = subprocess.Popen(
        [PYTHON, 'run_server.py'],
        cwd=PROJECT_DIR,
        creationflags=0x00000008,  # DETACHED_PROCESS
    )
    log(f'Flask started (PID {flask_proc.pid})')


def start_ngrok():
    global ngrok_proc
    if ngrok_proc and ngrok_proc.poll() is None:
        ngrok_proc.terminate()
        ngrok_proc.wait(timeout=5)
    # Kill any leftover ngrok
    os.system('taskkill /F /IM ngrok.exe >nul 2>&1')
    time.sleep(1)
    ngrok_proc = subprocess.Popen(
        ['ngrok', 'http', str(FLASK_PORT)],
        cwd=PROJECT_DIR,
        creationflags=0x00000008,
    )
    log(f'ngrok started (PID {ngrok_proc.pid})')


def get_ngrok_url():
    try:
        import json
        req = urllib.request.urlopen('http://127.0.0.1:4040/api/tunnels', timeout=5)
        data = json.loads(req.read())
        for t in data.get('tunnels', []):
            if 'https' in t.get('public_url', ''):
                return t['public_url']
    except Exception:
        pass
    return None


def log(msg):
    ts = time.strftime('%Y-%m-%d %H:%M:%S')
    line = f'[{ts}] {msg}'
    print(line, flush=True)
    try:
        with open(os.path.join(PROJECT_DIR, 'keepalive.log'), 'a', encoding='utf-8') as f:
            f.write(line + '\n')
    except Exception:
        pass


def main():
    log('Keepalive daemon started')

    start_flask()
    time.sleep(3)
    start_ngrok()
    time.sleep(5)

    url = get_ngrok_url()
    if url:
        log(f'ngrok URL: {url}')

    flask_fail_count = 0
    ngrok_fail_count = 0

    while True:
        time.sleep(CHECK_INTERVAL)

        # Check Flask
        if not is_flask_alive():
            flask_fail_count += 1
            log(f'Flask not responding (fail #{flask_fail_count})')
            if flask_fail_count >= 2:
                log('Restarting Flask...')
                start_flask()
                flask_fail_count = 0
                time.sleep(3)
        else:
            flask_fail_count = 0

        # Check ngrok
        if not is_ngrok_alive():
            ngrok_fail_count += 1
            log(f'ngrok not responding (fail #{ngrok_fail_count})')
            if ngrok_fail_count >= 2:
                log('Restarting ngrok...')
                start_ngrok()
                ngrok_fail_count = 0
                time.sleep(5)
                url = get_ngrok_url()
                if url:
                    log(f'New ngrok URL: {url}')
        else:
            ngrok_fail_count = 0


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        log('Keepalive daemon stopped')
        if flask_proc and flask_proc.poll() is None:
            flask_proc.terminate()
        if ngrok_proc and ngrok_proc.poll() is None:
            ngrok_proc.terminate()

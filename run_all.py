"""
Run both the news monitor and web dashboard together
"""

import threading
import subprocess
import time
import sys


def run_news_monitor():
    """Run the news monitoring script"""
    while True:
        try:
            subprocess.run([sys.executable, "main.py"], check=False)
        except Exception as e:
            print(f"News monitor error: {e}")
            time.sleep(10)


def run_web_server():
    """Run the Flask web server"""
    while True:
        try:
            result = subprocess.run([sys.executable, "app.py"], check=False)
            # If Flask exits cleanly, break the loop (don't restart)
            if result.returncode == 0:
                break
        except Exception as e:
            print(f"Web server error: {e}")
        time.sleep(10)


if __name__ == "__main__":
    print("=" * 60)
    print("STARTING NUVAMA NEWS MONITOR SYSTEM")
    print("=" * 60)
    print("News Monitor: Running in background")
    print("Web Dashboard: http://0.0.0.0:5000")
    print("=" * 60 + "\n")

    # Start news monitor in a separate thread
    monitor_thread = threading.Thread(target=run_news_monitor, daemon=True)
    monitor_thread.start()

    # Give monitor a moment to start
    time.sleep(2)

    # Run web server in main thread (blocks here)
    run_web_server()

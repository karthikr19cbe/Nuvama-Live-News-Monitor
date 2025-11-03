"""
Nuvama News Monitor - Replit Version
Runs continuously, checking every 1 minute
"""

import hashlib
import time
import json
import os
from datetime import datetime
import requests
from playwright.sync_api import sync_playwright

# CONFIGURATION
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "8224764009:AAHG5AGUm5LD3KD9xwSyo2GRRTCl1wPuLBw")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "678820723")
NUVAMA_URL = "https://www.nuvamawealth.com/live-news"
CHECK_INTERVAL_SECONDS = 60  # Check every 60 seconds

HISTORY_FILE = "headlines_seen.json"
HEADLINES_DB_FILE = "headlines_database.json"


def send_telegram(headline):
    """Send one headline to Telegram"""
    try:
        headline_clean = headline.strip()
        message = f"📰 <b>NUVAMA NEWS</b>\n\n{headline_clean}\n\n<a href='{NUVAMA_URL}'>View All</a>"

        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        response = requests.post(url,
                                 json={
                                     "chat_id": CHAT_ID,
                                     "text": message,
                                     "parse_mode": "HTML",
                                     "disable_web_page_preview": True
                                 },
                                 timeout=10)

        return response.status_code == 200
    except Exception as e:
        print(f"Telegram error: {e}")
        return False


def get_headlines():
    """Get headlines from Nuvama"""
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                executable_path="/nix/store/qa9cnw4v5xkxyip6mb9kxqfq1z4x2dx1-chromium-138.0.7204.100/bin/chromium",
                headless=True
            )
            page = browser.new_page()

            print("Loading page...")
            page.goto(NUVAMA_URL, wait_until="domcontentloaded", timeout=45000)
            time.sleep(8)

            all_text = page.inner_text("body")
            browser.close()

            lines = all_text.split('\n')
            headlines = []

            # Navigation/menu items to skip (exact matches only)
            skip_exact = [
                'live news', 'all', 'results', 'block deals', 'equity', 
                'commentary', 'global', 'fixed income', 'commodities', 
                'solutions', 'markets', 'tools & resources',
                'support', 'login / sign up', 'search', '0 updates'
            ]
            
            # Generic words that should be filtered (when appearing alone or with common words)
            skip_patterns = [
                'sign up', 'get started', 'why nuvama', 'support center', 
                'helpdesk', 'feedback', 'visit', 'locate', 'healthy financial',
                'customer', 'trader', 'menu', 'investor charter', 
                'dispute resolution portal', 'issue with our website',
                'issue is not resolved', 'join ', 'million customers',
                'empowering our clients', 'mon-fri', 'all rights reserved',
                'sebi scores', 'broking services offered by', 'registered office',
                'corporate office', 'financial products distribution',
                'most important terms', 'prevent unauthorized'
            ]

            for line in lines:
                line_clean = line.strip()

                # Length filter - allow longer headlines
                if len(line_clean) < 30 or len(line_clean) > 1500:
                    continue

                # Skip JavaScript/code patterns
                code_patterns = ['function(', 'var ', '=>', 'return ', 'typeof', 
                                'undefined', 'console.', 'window.', 'document.',
                                'jquery', 'dataLayer', '_gaq', '.push(', 'addEventListener']
                if any(pattern in line_clean for pattern in code_patterns):
                    continue
                
                # Skip lines with excessive special characters (code/HTML)
                special_chars = line_clean.count('{') + line_clean.count('}') + \
                               line_clean.count('[') + line_clean.count(']') + \
                               line_clean.count('<') + line_clean.count('>')
                if special_chars > 10:
                    continue

                line_lower = line_clean.lower()
                
                # Skip exact navigation menu items
                if line_lower in skip_exact:
                    continue
                    
                # Skip if line is mostly just time/date
                if line_lower.count(' mins ago') > 0 or line_lower.count(' hours ago') > 0:
                    continue
                
                # Always skip legal/compliance/footer content regardless of length
                legal_patterns = ['broking services offered by', 'registered office', 
                                 'corporate office', 'all rights reserved', 'sebi scores',
                                 'prevent unauthorized', 'financial products distribution',
                                 'most important terms', 'investor charter',
                                 'dispute resolution', 'issue is not resolved',
                                 'empowering our clients', 'dedicated to empowering']
                if any(pattern in line_lower for pattern in legal_patterns):
                    continue
                
                # Skip short lines that are just navigation patterns
                if any(pattern in line_lower for pattern in skip_patterns):
                    if len(line_clean) < 80:  # Short lines with nav words are likely menu items
                        continue
                
                # If we got here, it's likely a headline - add it!
                headlines.append(line_clean)

            print(f"Found {len(headlines)} headlines")
            # Return all headlines, don't slice them here
            return headlines

    except Exception as e:
        print(f"Scraping error: {e}")
        return []


def load_seen():
    """Load seen headlines"""
    try:
        with open(HISTORY_FILE, 'r') as f:
            return set(json.load(f))
    except:
        return set()


def save_seen(seen_ids):
    """Save seen headlines"""
    try:
        with open(HISTORY_FILE, 'w') as f:
            json.dump(list(seen_ids), f)
    except Exception as e:
        print(f"Save error: {e}")


def load_headlines_db():
    """Load headlines database"""
    try:
        with open(HEADLINES_DB_FILE, 'r') as f:
            return json.load(f)
    except:
        return []


def save_headline_to_db(headline):
    """Save a headline to the database with timestamp"""
    try:
        db = load_headlines_db()
        entry = {
            "headline": headline,
            "timestamp": datetime.now().strftime('%Y-%m-%d %I:%M:%S %p'),
            "date": datetime.now().strftime('%Y-%m-%d')
        }
        db.insert(0, entry)  # Add to beginning
        # Keep only last 100 headlines
        db = db[:100]
        with open(HEADLINES_DB_FILE, 'w') as f:
            json.dump(db, f, indent=2)
    except Exception as e:
        print(f"Database save error: {e}")


def check_and_notify():
    """Check for new headlines and send notifications"""
    print("=" * 60)
    print(f"Checking... {datetime.now().strftime('%Y-%m-%d %I:%M:%S %p')}")

    seen_ids = load_seen()
    headlines = get_headlines()

    if not headlines:
        print("No headlines found")
        return

    new_ones = []
    for h in headlines:
        h_id = hashlib.md5(h.encode()).hexdigest()
        if h_id not in seen_ids:
            new_ones.append((h, h_id))
            seen_ids.add(h_id)

    if new_ones:
        print(f"***** {len(new_ones)} NEW HEADLINES *****")
        for headline, h_id in new_ones:
            print(f"Sending: {headline[:70]}...")
            save_headline_to_db(headline)  # Save to database
            if send_telegram(headline):
                time.sleep(2)

        save_seen(seen_ids)
    else:
        print("No new headlines")


# Main loop
print("=" * 60)
print("NUVAMA NEWS MONITOR - REPLIT VERSION")
print("=" * 60)
print(f"Checking every {CHECK_INTERVAL_SECONDS} seconds")
print("=" * 60 + "\n")

# Send startup message
send_telegram(
    "Monitor started on Replit! Running 24/7. Each new headline arrives separately."
)

# Initial baseline
print("Setting baseline...")
initial_headlines = get_headlines()
initial_seen = set()
for h in initial_headlines:
    initial_seen.add(hashlib.md5(h.encode()).hexdigest())
    save_headline_to_db(h)  # Save initial headlines to database
save_seen(initial_seen)
print(f"Baseline set: {len(initial_headlines)} current headlines\n")

# Run forever
check_count = 0
while True:
    try:
        check_count += 1
        print(f"\n--- Check #{check_count} ---")
        check_and_notify()
        print(f"Waiting {CHECK_INTERVAL_SECONDS} seconds...\n")
        time.sleep(CHECK_INTERVAL_SECONDS)
    except KeyboardInterrupt:
        print("\nStopped by user")
        break
    except Exception as e:
        print(f"Error: {e}")
        print("Retrying in 60 seconds...")
        time.sleep(60)

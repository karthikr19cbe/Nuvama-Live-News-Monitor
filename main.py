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
        message = headline_clean

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
    """Get headlines from Nuvama with timestamps (handles both absolute and relative timestamps)"""
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
            
            import re
            # Pattern to match absolute timestamps like "03 Nov 06:35 AM"
            absolute_timestamp_pattern = r'^\d{2}\s+[A-Za-z]{3}\s+\d{2}:\d{2}\s+[AP]M$'
            # Pattern to match relative timestamps like "15 mins ago", "1 hour ago"
            relative_timestamp_pattern = r'^\d+\s+(min|mins|hour|hours)\s+ago$'
            # Pattern to match "Just Now"
            just_now_pattern = r'^Just\s+Now$'

            # Navigation/menu items to skip (exact matches only)
            skip_exact = [
                'live news', 'all', 'results', 'block deals', 'equity', 
                'commentary', 'global', 'fixed income', 'commodities', 
                'solutions', 'markets', 'tools & resources',
                'support', 'login / sign up', 'search', '0 updates'
            ]
            
            # Generic words that should be filtered
            skip_patterns = [
                'sign up', 'get started', 'why nuvama', 'support center', 
                'helpdesk', 'feedback', 'visit', 'locate', 'healthy financial',
                'customer', 'trader', 'menu', 'investor charter', 
                'dispute resolution portal', 'issue with our website',
                'issue is not resolved', 'join ', 'million customers',
                'empowering our clients', 'mon-fri', 'all rights reserved',
                'sebi scores', 'broking services offered by', 'registered office',
                'corporate office', 'financial products distribution',
                'most important terms', 'prevent unauthorized', 'healthy financial journey',
                'switch to old website', 'clicking the button below'
            ]
            
            # Legal patterns
            legal_patterns = ['broking services offered by', 'registered office', 
                             'corporate office', 'all rights reserved', 'sebi scores',
                             'prevent unauthorized', 'financial products distribution',
                             'most important terms', 'investor charter',
                             'dispute resolution', 'issue is not resolved',
                             'empowering our clients', 'dedicated to empowering']

            i = 0
            while i < len(lines):
                line_clean = lines[i].strip()
                
                # Look for a headline followed by a timestamp
                if i + 1 < len(lines):
                    next_line = lines[i + 1].strip()
                    
                    # Check if next line is a timestamp (absolute, relative, or "Just Now")
                    is_absolute_timestamp = re.match(absolute_timestamp_pattern, next_line)
                    is_relative_timestamp = re.match(relative_timestamp_pattern, next_line)
                    is_just_now = re.match(just_now_pattern, next_line, re.IGNORECASE)
                    
                    if is_absolute_timestamp or is_relative_timestamp or is_just_now:
                        # This line might be a headline
                        
                        # Length filter
                        if len(line_clean) >= 30 and len(line_clean) <= 1500:
                            line_lower = line_clean.lower()
                            
                            # Skip if it's a navigation item
                            if line_lower not in skip_exact:
                                # Skip legal patterns
                                if not any(pattern in line_lower for pattern in legal_patterns):
                                    # Skip short lines with nav patterns
                                    skip_this = False
                                    if any(pattern in line_lower for pattern in skip_patterns):
                                        if len(line_clean) < 80:
                                            skip_this = True
                                    
                                    if not skip_this:
                                        # This is a valid headline with timestamp
                                        headlines.append({
                                            'headline': line_clean,
                                            'timestamp': next_line
                                        })
                                        i += 3  # Skip headline, timestamp, and category
                                        continue
                
                i += 1

            # Reverse list so newest headlines get processed last (and end up at index 0)
            headlines.reverse()
            
            print(f"Found {len(headlines)} headlines")
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


def save_headline_to_db(headline_text, publish_timestamp):
    """Save a headline to the database with actual publish timestamp in IST"""
    try:
        db = load_headlines_db()
        
        # Parse the timestamp - handle both absolute and relative formats
        from datetime import datetime as dt, timedelta, timezone
        import re
        
        # IST timezone offset (UTC+5:30)
        IST = timezone(timedelta(hours=5, minutes=30))
        
        try:
            # Check if it's "Just Now"
            if re.match(r'^Just\s+Now$', publish_timestamp.strip(), re.IGNORECASE):
                # Use current IST time
                now_ist = dt.now(IST)
                formatted_timestamp = now_ist.strftime('%d %b %I:%M %p')
                formatted_date = now_ist.strftime('%Y-%m-%d')
            # Check if it's a relative timestamp like "15 mins ago" or "1 hour ago"
            elif relative_match := re.match(r'^(\d+)\s+(min|mins|hour|hours)\s+ago$', publish_timestamp.strip()):
                # It's a relative timestamp - calculate actual publish time in IST
                amount = int(relative_match.group(1))
                unit = relative_match.group(2)
                
                # Get current time in IST
                now_ist = dt.now(IST)
                if unit in ['min', 'mins']:
                    actual_time = now_ist - timedelta(minutes=amount)
                else:  # hours
                    actual_time = now_ist - timedelta(hours=amount)
                
                formatted_timestamp = actual_time.strftime('%d %b %I:%M %p')
                formatted_date = actual_time.strftime('%Y-%m-%d')
            else:
                # It's an absolute timestamp like "03 Nov 06:35 AM" (already in IST from Nuvama)
                parsed_time = dt.strptime(publish_timestamp.strip(), '%d %b %I:%M %p')
                # Add current year
                current_year = dt.now(IST).year
                parsed_time = parsed_time.replace(year=current_year)
                # Format consistently
                formatted_timestamp = parsed_time.strftime('%d %b %I:%M %p')
                formatted_date = parsed_time.strftime('%Y-%m-%d')
        except Exception as parse_error:
            # Fallback to original timestamp if parsing fails
            print(f"Timestamp parse error: {parse_error} for '{publish_timestamp}'")
            formatted_timestamp = publish_timestamp
            formatted_date = dt.now(IST).strftime('%Y-%m-%d')
        
        entry = {
            "headline": headline_text,
            "timestamp": formatted_timestamp,
            "date": formatted_date
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
        headline_text = h['headline']
        timestamp = h['timestamp']
        
        # Remove stock price percentages for deduplication (they change constantly)
        import re
        headline_for_hash = re.sub(r'\([+-]?\d+\.\d+%\)', '', headline_text)
        headline_for_hash = re.sub(r'\s+', ' ', headline_for_hash).strip()
        
        h_id = hashlib.md5(headline_for_hash.encode()).hexdigest()
        if h_id not in seen_ids:
            new_ones.append((headline_text, timestamp, h_id))
            seen_ids.add(h_id)

    if new_ones:
        print(f"***** {len(new_ones)} NEW HEADLINES *****")
        # Reverse the list so oldest headlines are sent first (chronological order in Telegram)
        new_ones.reverse()
        for headline_text, timestamp, h_id in new_ones:
            print(f"Sending: {headline_text[:70]}...")
            save_headline_to_db(headline_text, timestamp)  # Save to database with actual timestamp
            if send_telegram(headline_text):
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

# Initial baseline - load existing seen IDs to prevent re-sending old headlines
print("Setting baseline...")
initial_headlines = get_headlines()
# IMPORTANT: Load existing seen IDs to preserve deduplication across restarts
initial_seen = load_seen()  # This prevents re-sending old headlines after restart
for h in initial_headlines:
    headline_text = h['headline']
    timestamp = h['timestamp']
    
    # Remove stock price percentages for deduplication (they change constantly)
    import re
    headline_for_hash = re.sub(r'\([+-]?\d+\.\d+%\)', '', headline_text)
    headline_for_hash = re.sub(r'\s+', ' ', headline_for_hash).strip()
    
    h_id = hashlib.md5(headline_for_hash.encode()).hexdigest()
    # Only save to database if this is truly a new headline (not seen before)
    if h_id not in initial_seen:
        save_headline_to_db(headline_text, timestamp)
    initial_seen.add(h_id)
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

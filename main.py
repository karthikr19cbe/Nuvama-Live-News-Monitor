"""
Nuvama News Monitor - Enhanced Version with Timestamp-Based Filtering
Runs continuously, checking every 1 minute
Handles restarts gracefully using persistent timestamp tracking
"""

import sys
import hashlib
import time
import json
import os
from datetime import datetime, timedelta, timezone
import requests
from playwright.sync_api import sync_playwright
import re
from dotenv import load_dotenv

# Fix Windows console encoding to support Unicode (₹, emojis, etc.)
if sys.stdout.encoding != 'utf-8':
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
if sys.stderr.encoding != 'utf-8':
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')

# Load environment variables from .env file
load_dotenv()

# CONFIGURATION
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
NUVAMA_URL = "https://www.nuvamawealth.com/live-news"
CHECK_INTERVAL_SECONDS = 60

# RESULTS FILTER - skip earnings/results headlines from Telegram (still saved to dashboard)
EXCLUDE_RESULTS_ALERTS = os.getenv("EXCLUDE_RESULTS_ALERTS", "false").lower() == "true"

# STATE FILES
HISTORY_FILE = "headlines_seen.json"
HEADLINES_DB_FILE = "headlines_database.json"
LAST_CHECK_FILE = "last_check_timestamp.json"
ERROR_LOG_FILE = "error_log.json"

# IST timezone (UTC+5:30)
IST = timezone(timedelta(hours=5, minutes=30))


def log_error(error_type, message, details=None):
    """Log errors to file with timestamp"""
    try:
        try:
            with open(ERROR_LOG_FILE, 'r') as f:
                errors = json.load(f)
        except:
            errors = []
        
        error_entry = {
            "timestamp": datetime.now(IST).isoformat(),
            "type": error_type,
            "message": str(message),
            "details": details
        }
        errors.append(error_entry)
        
        # Keep only last 100 errors
        errors = errors[-100:]
        
        with open(ERROR_LOG_FILE, 'w') as f:
            json.dump(errors, f, indent=2)
    except Exception as e:
        print(f"Error logging failed: {e}")


def parse_timestamp_to_datetime(publish_timestamp):
    """
    Convert Nuvama timestamp to IST datetime object for comparison
    Handles: "Just Now", "15 mins ago", "2 hours ago", "03 Nov 08:26 AM"
    Returns: datetime object in IST or None if parsing fails
    """
    try:
        timestamp_str = publish_timestamp.strip()
        
        # Handle "Just Now"
        if re.match(r'^Just\s+Now$', timestamp_str, re.IGNORECASE):
            return datetime.now(IST)
        
        # Handle relative timestamps like "15 mins ago" or "2 hours ago"
        relative_match = re.match(r'^(\d+)\s+(min|mins|hour|hours)\s+ago$', timestamp_str)
        if relative_match:
            amount = int(relative_match.group(1))
            unit = relative_match.group(2)
            
            now_ist = datetime.now(IST)
            if unit in ['min', 'mins']:
                return now_ist - timedelta(minutes=amount)
            else:  # hours
                return now_ist - timedelta(hours=amount)
        
        # Handle absolute timestamps like "03 Nov 08:26 AM"
        try:
            parsed_time = datetime.strptime(timestamp_str, '%d %b %I:%M %p')
            current_year = datetime.now(IST).year
            # Create timezone-aware datetime in IST
            parsed_time = parsed_time.replace(year=current_year, tzinfo=IST)
            return parsed_time
        except:
            pass
        
        return None
    except Exception as e:
        log_error("timestamp_parse", f"Failed to parse timestamp: {publish_timestamp}", str(e))
        return None


def send_telegram(headline):
    """Send one headline to Telegram with error handling"""
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

        if response.status_code == 200:
            return True
        else:
            log_error("telegram_api", f"Non-200 response: {response.status_code}", headline[:100])
            return False
    except Exception as e:
        log_error("telegram_send", str(e), headline[:100])
        print(f"Telegram error: {e}")
        return False


def get_headlines():
    """Get headlines from Nuvama with timestamps and datetime objects"""
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()

            print("Loading page...")
            page.goto(NUVAMA_URL, wait_until="domcontentloaded", timeout=45000)
            time.sleep(8)

            all_text = page.inner_text("body")
            browser.close()

            lines = all_text.split('\n')
            headlines = []
            
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
                            # Skip navigation items
                            if line_clean.lower() in skip_exact:
                                i += 1
                                continue
                            
                            # Skip generic patterns
                            if any(skip_word in line_clean.lower() for skip_word in skip_patterns):
                                i += 1
                                continue
                            
                            # Skip legal patterns
                            if any(legal in line_clean.lower() for legal in legal_patterns):
                                i += 1
                                continue
                            
                            # This looks like a real headline
                            timestamp_str = next_line
                            datetime_obj = parse_timestamp_to_datetime(timestamp_str)

                            # Check line after timestamp for category tag (Result, Equity, etc.)
                            category = ""
                            if i + 2 < len(lines):
                                category = lines[i + 2].strip()

                            headlines.append({
                                'headline': line_clean,
                                'timestamp': timestamp_str,
                                'datetime': datetime_obj,  # For comparison
                                'category': category
                            })
                            i += 3  # Skip headline, timestamp, and category
                            continue
                
                i += 1
            
            # Don't reverse - Nuvama already shows newest headlines first
            # Headlines will be processed in order: newest first
            
            print(f"Found {len(headlines)} headlines")
            return headlines

    except Exception as e:
        log_error("scraping", str(e), NUVAMA_URL)
        print(f"Scraping error: {e}")
        return []


def load_seen():
    """Load seen headlines with validation"""
    try:
        with open(HISTORY_FILE, 'r') as f:
            data = json.load(f)
            if isinstance(data, list):
                return set(data)
            return set()
    except:
        return set()


def save_seen(seen_ids):
    """Save seen headlines"""
    try:
        with open(HISTORY_FILE, 'w') as f:
            json.dump(list(seen_ids), f, indent=2)
    except Exception as e:
        log_error("save_seen", str(e), f"Seen IDs count: {len(seen_ids)}")
        print(f"Save error: {e}")


def load_last_check_timestamp():
    """Load last successful check timestamp"""
    try:
        with open(LAST_CHECK_FILE, 'r') as f:
            data = json.load(f)
            timestamp_str = data.get('last_check')
            if timestamp_str:
                return datetime.fromisoformat(timestamp_str)
        return None
    except:
        return None


def save_last_check_timestamp():
    """Save current timestamp as last successful check"""
    try:
        data = {
            'last_check': datetime.now(IST).isoformat(),
            'last_check_readable': datetime.now(IST).strftime('%d %b %Y %I:%M:%S %p IST')
        }
        with open(LAST_CHECK_FILE, 'w') as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        log_error("save_last_check", str(e), None)
        print(f"Failed to save last check timestamp: {e}")


def load_headlines_db():
    """Load headlines database with validation"""
    try:
        with open(HEADLINES_DB_FILE, 'r') as f:
            data = json.load(f)
            if isinstance(data, list):
                return data
            return []
    except:
        return []


def save_headline_to_db(headline_text, publish_timestamp):
    """Save a headline to the database with actual publish timestamp in IST"""
    try:
        db = load_headlines_db()
        
        datetime_obj = parse_timestamp_to_datetime(publish_timestamp)
        if datetime_obj:
            formatted_timestamp = datetime_obj.strftime('%d %b %I:%M %p')
            formatted_date = datetime_obj.strftime('%Y-%m-%d')
        else:
            # Fallback
            formatted_timestamp = publish_timestamp
            formatted_date = datetime.now(IST).strftime('%Y-%m-%d')
        
        entry = {
            "headline": headline_text,
            "timestamp": formatted_timestamp,
            "date": formatted_date
        }
        
        # Always insert at position 0 (newest first)
        # This ensures new headlines always appear at the top of the dashboard
        db.insert(0, entry)
        
        # Keep only last 100 headlines
        db = db[:100]
        with open(HEADLINES_DB_FILE, 'w') as f:
            json.dump(db, f, indent=2)
    except Exception as e:
        log_error("database_save", str(e), headline_text[:100])
        print(f"Database save error: {e}")


def is_results_headline(headline_text, category=""):
    """
    Detect if a headline is a results/earnings headline.
    Uses both category tag AND content-based detection for reliability.
    The category tag from Nuvama is often not captured correctly,
    so content-based detection is the primary method.
    """
    # Check category tag first (may be unreliable)
    cat = category.strip().lower()
    if cat in ("result", "results", "earning", "earnings"):
        return True

    # Content-based detection: look for earnings/results patterns in headline text
    text_lower = headline_text.lower()

    # Quarterly results pattern: "Q3 Net Profit", "Q2 Revenue", "Q1 EBITDA", etc.
    if re.search(r'\bq[1-4]\b', text_lower):
        # Confirm it's actually an earnings headline (not just mentioning Q1-Q4 casually)
        earnings_keywords = [
            'net profit', 'net loss', 'revenue', 'ebitda', 'ebitda margin',
            'rupees vs', 'rupees vs.', 'yoy', 'qoq', 'est ',
            'margin', 'topline', 'bottomline', 'bottom line', 'top line',
            'profit after tax', 'pat ', 'sales ',
        ]
        if any(kw in text_lower for kw in earnings_keywords):
            return True

    # Direct financial results patterns (without Q1-Q4 prefix)
    results_patterns = [
        r'\bnet profit\b.*\brupees\b',
        r'\bnet loss\b.*\brupees\b',
        r'\brevenue\b.*\brupees\b.*\byoy\b',
        r'\bebitda\b.*\brupees\b.*\byoy\b',
        r'\bsl net profit\b',
        r'\bcons net profit\b',
        r'\bsl net loss\b',
        r'\bcons net loss\b',
    ]
    for pattern in results_patterns:
        if re.search(pattern, text_lower):
            return True

    return False


def normalize_headline_for_dedup(headline_text):
    """
    Normalize headline for deduplication
    Handles all known Nuvama format variations
    """
    # Remove stock price percentages (they change constantly)
    text = re.sub(r'\([+-]?\d+\.\d+%\)', '', headline_text)
    # Normalize Nuvama's "- :" placeholder (used when no stock price)
    text = re.sub(r'[-–—]\s*:\s*', ': ', text)
    # Collapse multiple spaces
    text = re.sub(r'\s+', ' ', text).strip()
    # Lowercase for case-insensitive matching
    return text.lower()


def check_and_notify():
    """Check for new headlines and send notifications with timestamp filtering"""
    print("=" * 60)
    print(f"Checking... {datetime.now(IST).strftime('%Y-%m-%d %I:%M:%S %p IST')}")

    seen_ids = load_seen()
    headlines = get_headlines()

    if not headlines:
        print("No headlines found")
        return

    # Load last check timestamp for filtering
    last_check = load_last_check_timestamp()
    if last_check:
        print(f"Last check was: {last_check.strftime('%d %b %I:%M %p IST')}")

    new_ones = []
    for h in headlines:
        headline_text = h['headline']
        timestamp = h['timestamp']
        datetime_obj = h['datetime']
        category = h.get('category', '')

        # Normalize for deduplication
        headline_normalized = normalize_headline_for_dedup(headline_text)
        h_id = hashlib.md5(headline_normalized.encode()).hexdigest()

        if h_id not in seen_ids:
            new_ones.append((headline_text, timestamp, datetime_obj, h_id, category))
            seen_ids.add(h_id)

    if new_ones:
        print(f"***** {len(new_ones)} NEW HEADLINES *****")
        # new_ones is already in newest-first order from Nuvama
        # Send them in this order to Telegram (newest first)

        for headline_text, timestamp, datetime_obj, h_id, category in new_ones:
            # Check if this headline is truly newer than last check
            should_send_alert = True
            if last_check and datetime_obj:
                # Only send alert if headline is newer than last check
                if datetime_obj <= last_check:
                    should_send_alert = False
                    print(f"Skipping old: {headline_text[:50]}... [{timestamp}]")

            # Always save to database
            save_headline_to_db(headline_text, timestamp)

            # Only send to Telegram if it's truly new and not filtered
            if should_send_alert:
                if EXCLUDE_RESULTS_ALERTS and is_results_headline(headline_text, category):
                    print(f"Filtered [Result]: {headline_text[:70]}...")
                else:
                    print(f"Sending: {headline_text[:70]}...")
                    if send_telegram(headline_text):
                        time.sleep(2)

        save_seen(seen_ids)
    else:
        print("No new headlines")
    
    # Update last check timestamp after successful check
    save_last_check_timestamp()


# Main loop
print("=" * 60)
print("NUVAMA NEWS MONITOR - ENHANCED VERSION")
print("=" * 60)
print(f"Checking every {CHECK_INTERVAL_SECONDS} seconds")
print(f"IST Timezone: UTC+5:30")
print(f"Exclude results alerts: {EXCLUDE_RESULTS_ALERTS}")
print("=" * 60 + "\n")

# Check if this is a restart
last_check = load_last_check_timestamp()
if last_check:
    downtime = datetime.now(IST) - last_check
    print("[!] RESTART DETECTED")
    print(f"Last check: {last_check.strftime('%d %b %Y %I:%M:%S %p IST')}")
    print(f"Downtime: {downtime}")
    print(f"Will send ONLY headlines newer than last check to Telegram\n")
else:
    print("[*] FIRST RUN - Initializing baseline\n")

# Send startup message
send_telegram(
    "🔄 Monitor restarted! Running 24/7. Only new headlines will be sent."
)

# Initial baseline with timestamp-based filtering
print("Setting baseline...")
initial_headlines = get_headlines()
initial_seen = load_seen()  # Load existing seen IDs

for h in initial_headlines:
    headline_text = h['headline']
    timestamp = h['timestamp']
    datetime_obj = h['datetime']
    category = h.get('category', '')

    # Normalize for deduplication
    headline_normalized = normalize_headline_for_dedup(headline_text)
    h_id = hashlib.md5(headline_normalized.encode()).hexdigest()

    # Check if truly new (not in previous runs)
    is_new = h_id not in initial_seen
    should_alert = False

    if is_new and last_check and datetime_obj:
        # Check if headline is newer than last check
        if datetime_obj > last_check:
            should_alert = True

    # Always save to database to rebuild with correct order
    save_headline_to_db(headline_text, timestamp)

    # Send alert only if truly new and newer than last check
    if is_new and should_alert:
        if EXCLUDE_RESULTS_ALERTS and is_results_headline(headline_text, category):
            print(f"[FILTERED] Result skipped: {headline_text[:60]}...")
        else:
            print(f"[ALERT] New during downtime: {headline_text[:60]}...")
            send_telegram(headline_text)
            time.sleep(2)

    initial_seen.add(h_id)

save_seen(initial_seen)
save_last_check_timestamp()
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
        print("\n[!] Stopped by user")
        save_last_check_timestamp()
        break
    except Exception as e:
        log_error("main_loop", str(e), f"Check #{check_count}")
        print(f"Error: {e}")
        print("Retrying in 60 seconds...")
        time.sleep(60)

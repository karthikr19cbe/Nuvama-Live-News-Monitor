"""
Unified News Monitor - Nuvama + Stockwatch
Runs continuously, checking every 1 minute
Handles restarts gracefully using persistent timestamp tracking
Two-layer deduplication: exact (MD5) + contextual (similarity scoring)
"""

import sys
import hashlib
import time
import json
import os
import csv
import threading
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from urllib.parse import unquote, parse_qs, urlparse
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
STOCKWATCH_URL = "https://stockwatch.live/in/live"  # human-facing "Live" feed (for reference)
# Public JSON API behind the Live feed — fetched directly (no browser). Returns
# ALL corporate-action categories (Board Meeting, Dividend, Merger, ESG, ...).
STOCKWATCH_API_URL = "https://api.stockwatch.live/api/keyEvents/"
# How far back to fetch each cycle. Newest-first; exact-dedup handles overlap.
STOCKWATCH_LOOKBACK_DAYS = int(os.getenv("STOCKWATCH_LOOKBACK_DAYS", "2"))
# Cap on events pulled per cycle (paginated 50 at a time).
STOCKWATCH_MAX_ITEMS = int(os.getenv("STOCKWATCH_MAX_ITEMS", "100"))
CHECK_INTERVAL_SECONDS = 60

# RESULTS FILTER - skip earnings/results headlines from Telegram (still saved to dashboard)
EXCLUDE_RESULTS_ALERTS = os.getenv("EXCLUDE_RESULTS_ALERTS", "false").lower() == "true"
# Skip advance board-meeting notices ("Board Meeting on <date>", "Board to Consider ...")
EXCLUDE_BOARD_MEETING_NOTICES = os.getenv("EXCLUDE_BOARD_MEETING_NOTICES", "true").lower() == "true"

# STOCKWATCH FEED TOGGLE
ENABLE_STOCKWATCH = os.getenv("ENABLE_STOCKWATCH", "true").lower() == "true"

# STOCKWATCH COMPANY FILTER (matches against both CSVs combined)
NIFTY500_CSV_PATH = os.getenv("NIFTY500_CSV_PATH", "nifty500_companies.csv")
ADDITIONAL_CSV_PATH = os.getenv("ADDITIONAL_CSV_PATH", "")

# CONTEXTUAL DEDUP
ENABLE_EMBEDDING_DEDUP = os.getenv("ENABLE_EMBEDDING_DEDUP", "false").lower() == "true"
EMBEDDING_MODEL_NAME = os.getenv("EMBEDDING_MODEL_NAME", "sentence-transformers/all-MiniLM-L6-v2")
CONTEXTUAL_DEDUP_THRESHOLD = float(os.getenv("CONTEXTUAL_DEDUP_THRESHOLD", "0.68"))
# Same-company items where this share of one headline's key words appear in the
# other's headline + Stockwatch description are treated as duplicates
DESCRIPTION_DEDUP_THRESHOLD = float(os.getenv("DESCRIPTION_DEDUP_THRESHOLD", "0.7"))

# STATE FILES
HISTORY_FILE = "headlines_seen.json"
HEADLINES_DB_FILE = "headlines_database.json"
LAST_CHECK_FILE = "last_check_timestamp.json"
ERROR_LOG_FILE = "error_log.json"
CONTEXT_MEMORY_FILE = "alerts_context_memory.json"

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


# ---------- WATCHDOG ----------
# A hung Playwright/Chromium scrape can block the whole process indefinitely
# (its own 45s timeout doesn't always fire when the browser zombies), which
# silently freezes the monitor with no error logged. The watchdog force-exits
# the process if no progress heartbeat is seen for WATCHDOG_TIMEOUT seconds.
# run_all.py then respawns main.py automatically, so a hang self-heals.
WATCHDOG_TIMEOUT = int(os.getenv("WATCHDOG_TIMEOUT", "300"))  # seconds
_last_heartbeat = None
_heartbeat_lock = threading.Lock()


def heartbeat():
    """Mark that the monitor is making progress (called at safe points)."""
    global _last_heartbeat
    with _heartbeat_lock:
        _last_heartbeat = time.time()


def _watchdog_loop():
    while True:
        time.sleep(30)
        with _heartbeat_lock:
            last = _last_heartbeat
        if last is None:
            continue
        elapsed = time.time() - last
        if elapsed > WATCHDOG_TIMEOUT:
            msg = f"No progress for {int(elapsed)}s (likely hung scrape) — forcing restart"
            print(f"[WATCHDOG] {msg}")
            try:
                log_error("watchdog", msg, None)
            except Exception:
                pass
            # Hard-exit so run_all.py's supervisor respawns a fresh main.py
            os._exit(1)


def start_watchdog():
    """Start the background watchdog thread."""
    heartbeat()
    t = threading.Thread(target=_watchdog_loop, daemon=True)
    t.start()
    print(f"Watchdog started (force-restart if no progress for {WATCHDOG_TIMEOUT}s)")


def parse_timestamp_to_datetime(publish_timestamp):
    """
    Convert timestamp to IST datetime object for comparison.
    Handles Nuvama: "Just Now", "15 mins ago", "2 hours ago", "03 Nov 08:26 AM"
    Handles Stockwatch: "4m ago | 07:42 PM 12-02-2026"
    Returns: datetime object in IST or None if parsing fails
    """
    try:
        timestamp_str = publish_timestamp.strip()
        
        # Handle "Just Now"
        if re.match(r'^Just\s+Now$', timestamp_str, re.IGNORECASE):
            return datetime.now(IST)
        
        # Handle relative timestamps like "15 mins ago", "2 hours ago", "12h ago", "5m ago"
        relative_match = re.match(r'^(\d+)\s*(min|mins|minute|minutes|m|hour|hours|h|d|day|days)\s*ago$', timestamp_str, re.IGNORECASE)
        if relative_match:
            amount = int(relative_match.group(1))
            unit = relative_match.group(2).lower()

            now_ist = datetime.now(IST)
            if unit in ['min', 'mins', 'minute', 'minutes', 'm']:
                return now_ist - timedelta(minutes=amount)
            elif unit in ['hour', 'hours', 'h']:
                return now_ist - timedelta(hours=amount)
            elif unit in ['d', 'day', 'days']:
                return now_ist - timedelta(days=amount)
        
        # Handle absolute timestamps like "03 Nov 08:26 AM"
        try:
            parsed_time = datetime.strptime(timestamp_str, '%d %b %I:%M %p')
            current_year = datetime.now(IST).year
            parsed_time = parsed_time.replace(year=current_year, tzinfo=IST)
            return parsed_time
        except:
            pass

        # Handle Stockwatch short date format: "4 Apr", "12 Mar"
        try:
            parsed_time = datetime.strptime(timestamp_str, '%d %b')
            current_year = datetime.now(IST).year
            parsed_time = parsed_time.replace(year=current_year, tzinfo=IST)
            return parsed_time
        except:
            pass

        # Handle Stockwatch format: "4m ago | 07:42 PM 12-02-2026" (legacy)
        stockwatch_match = re.match(
            r'(\d+)m\s+ago\s*\|\s*(\d{1,2}:\d{2}\s*[AP]M)\s+(\d{2}-\d{2}-\d{4})',
            timestamp_str
        )
        if stockwatch_match:
            time_part = stockwatch_match.group(2).strip()
            date_part = stockwatch_match.group(3).strip()
            try:
                parsed = datetime.strptime(f"{date_part} {time_part}", '%d-%m-%Y %I:%M %p')
                return parsed.replace(tzinfo=IST)
            except:
                pass

        return None
    except Exception as e:
        log_error("timestamp_parse", f"Failed to parse timestamp: {publish_timestamp}", str(e))
        return None


def send_telegram(headline_text, source="", company="", description=""):
    """Send one headline to Telegram with source tag, company info, and optional description"""
    try:
        headline_clean = headline_text.strip()

        parts = []
        if company:
            parts.append(f"<i>{company}</i>")
        parts.append(headline_clean)
        if description:
            # Trim description to ~200 words max
            words = description.split()
            if len(words) > 200:
                description = ' '.join(words[:200]) + '...'
            parts.append(f"\n{description}")
        if source:
            parts.append(f"<b>[{source}]</b>")

        message = "\n".join(parts) if len(parts) > 1 else parts[0]

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
            log_error("telegram_api", f"Non-200 response: {response.status_code}", headline_text[:100])
            return False
    except Exception as e:
        log_error("telegram_send", str(e), headline_text[:100])
        print(f"Telegram error: {e}")
        return False


# Persistent Nuvama browser: launched once and reused across checks (the page
# is just reloaded each cycle) so a Chrome window doesn't pop in and out of the
# taskbar every minute. Any failure tears it down and the next check relaunches.
_nv_playwright = None
_nv_browser = None
_nv_page = None


def _close_nuvama_browser():
    global _nv_playwright, _nv_browser, _nv_page
    for obj in (_nv_browser, _nv_playwright):
        try:
            if obj is _nv_playwright and obj is not None:
                obj.stop()
            elif obj is not None:
                obj.close()
        except Exception:
            pass
    _nv_playwright = _nv_browser = _nv_page = None


def _get_nuvama_page():
    global _nv_playwright, _nv_browser, _nv_page
    if _nv_page is not None and not _nv_page.is_closed() and _nv_browser.is_connected():
        return _nv_page
    _close_nuvama_browser()
    _nv_playwright = sync_playwright().start()
    # Nuvama's SPA detects headless Chromium and never loads the news
    # feed (only the page shell renders). A headed browser loads it
    # instantly. Launch headed but positioned far off-screen so no
    # visible window disrupts the desktop.
    _nv_browser = _nv_playwright.chromium.launch(
        headless=False,
        args=[
            "--window-position=-32000,-32000",
            "--window-size=1366,900",
            "--disable-blink-features=AutomationControlled",
        ],
    )
    _nv_page = _nv_browser.new_page()
    return _nv_page


def _fetch_nuvama_text():
    """Load (or reload) the Nuvama live feed and return the page body text.

    Returns None if Nuvama blocked the request. Raises on browser errors after
    tearing the browser down so the next call starts fresh.
    """
    try:
        page = _get_nuvama_page()

        print("Loading page...")
        page.goto(NUVAMA_URL, wait_until="domcontentloaded", timeout=45000)

        # Wait for the live feed to actually render (timestamps appear)
        # rather than a fixed blind sleep. Poll up to ~24s.
        feed_pat = r'(Just\s+Now|\d+\s+(?:min|mins|hour|hours)\s+ago|\d{2}\s+[A-Za-z]{3}\s+\d{2}:\d{2}\s+[AP]M)'
        all_text = ""
        for _ in range(12):
            time.sleep(2)
            all_text = page.inner_text("body")
            if "403 ERROR" in all_text or "Request blocked" in all_text:
                log_error("scraping", "Nuvama returned 403 (blocked/rate-limited)", NUVAMA_URL)
                # Drop the session so the next attempt starts with a clean browser
                _close_nuvama_browser()
                return None
            if len(re.findall(feed_pat, all_text, re.IGNORECASE)) >= 3:
                break
        return all_text
    except Exception:
        _close_nuvama_browser()
        raise


def scrape_nuvama():
    """Scrape headlines from Nuvama with timestamps and datetime objects"""
    try:
        all_text = _fetch_nuvama_text()
        if all_text is None:
            return []


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


def _parse_iso_utc_to_ist(iso_str):
    """Parse an ISO-8601 UTC timestamp (e.g. '2026-07-21T17:50:19.000Z') to IST."""
    if not iso_str:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            dt = datetime.strptime(iso_str, fmt)
            return dt.replace(tzinfo=timezone.utc).astimezone(IST)
        except ValueError:
            continue
    return None


def scrape_stockwatch():
    """Fetch corporate-action headlines from the Stockwatch public JSON API.

    Uses the same endpoint the stockwatch.live/in/live feed calls
    (api.stockwatch.live/api/keyEvents). This replaces the old browser scrape of
    /dashboard, which the 2026 redesign reduced to text-less compact rows (so
    dividends/splits/bonuses were silently dropped and the browser routinely hung).

    Each event carries: a clean title, full summary, category (Board Meeting,
    Dividend, Merger, ESG, Quarterly Result, ...), a real publish timestamp, a
    unique uuid, and the company's ticker + full name. Newest-first, paginated.
    """
    try:
        now_utc = datetime.now(timezone.utc)
        start = (now_utc - timedelta(days=STOCKWATCH_LOOKBACK_DAYS)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        end = (now_utc + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%S.000Z")

        headers = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
        headlines = []
        seen_uuids = set()
        offset = 0
        PAGE = 50

        while offset < STOCKWATCH_MAX_ITEMS:
            params = {
                "offset": offset,
                "limit": PAGE,
                "startDate": start,
                "endDate": end,
            }
            resp = requests.get(STOCKWATCH_API_URL, params=params, headers=headers, timeout=30)
            resp.raise_for_status()
            items = resp.json().get("data") or []
            if not items:
                break

            for it in items:
                uuid = (it.get("uuid") or "").strip()
                if uuid and uuid in seen_uuids:
                    continue
                seen_uuids.add(uuid)

                title = (it.get("title") or "").strip()
                if not title:
                    continue

                summary = (it.get("summary") or "").strip()
                category = (it.get("category") or "").strip()

                stock = it.get("stock") or it.get("bseStock") or {}
                code = (stock.get("code") or "").strip()
                name = (stock.get("name") or "").strip()

                # Real publish time (UTC -> IST); fall back to now if missing
                dt = _parse_iso_utc_to_ist(it.get("createdAt")) or datetime.now(IST)

                headlines.append({
                    'headline': title,
                    'description': summary,
                    'timestamp': dt.strftime('%d %b %I:%M %p'),
                    'datetime': dt,
                    'category': category,
                    'source': 'STOCKWATCH',
                    'company': code,
                    'company_name': name,
                    'news_id': uuid,
                })

            if len(items) < PAGE:
                break
            offset += PAGE

        print(f"Found {len(headlines)} Stockwatch headlines")
        return headlines

    except Exception as e:
        log_error("scraping_stockwatch", str(e), STOCKWATCH_API_URL)
        print(f"Stockwatch scraping error: {e}")
        return []


def get_all_headlines():
    """Fetch and combine headlines from all sources into standardized format"""
    all_headlines = []

    # Scrape Nuvama (returns old format: {headline, timestamp, datetime, category})
    nuvama_raw = scrape_nuvama()
    for h in nuvama_raw:
        all_headlines.append({
            'headline': h['headline'],
            'timestamp': h['timestamp'],
            'datetime': h['datetime'],
            'category': h.get('category', ''),
            'source': 'NUVAMA',
            'company': '',
            'news_id': ''
        })

    # Scrape Stockwatch (already returns standardized format)
    stockwatch_raw = []
    if ENABLE_STOCKWATCH:
        stockwatch_raw = scrape_stockwatch()
        all_headlines.extend(stockwatch_raw)
    else:
        print("Stockwatch feed disabled")

    # Sort combined list by datetime descending (newest first)
    all_headlines.sort(
        key=lambda x: x['datetime'] if x['datetime'] else datetime.min.replace(tzinfo=IST),
        reverse=True
    )

    print(f"Combined: {len(nuvama_raw)} Nuvama + {len(stockwatch_raw)} Stockwatch = {len(all_headlines)} total")
    return all_headlines


# ---------- NIFTY 500 FILTER (Stockwatch only) ----------

_nifty500_companies = None
_nifty500_aliases = None


def _strip_suffixes(name):
    """Strip common company name suffixes for alias generation."""
    suffixes = [' ltd.', ' ltd', ' limited', ' co.', ' corp.', ' inc.']
    lower = name.lower().strip()
    for s in suffixes:
        if lower.endswith(s):
            lower = lower[:-len(s)].strip()
    return lower


def _normalize_company(name):
    """Normalize company name for matching: strip suffixes, remove punctuation, lowercase."""
    lower = _strip_suffixes(name).lower()
    # Remove dots, apostrophes, and normalize ampersands (varies between sources)
    lower = lower.replace('.', '').replace("'", '').replace('&', ' and ')
    lower = re.sub(r'\s+', ' ', lower).strip()
    return lower


def _load_companies_from_csv(csv_path, companies, aliases):
    """Load companies from a single CSV into the provided set and dict.
    Supports NSE format (Company Name, Symbol) and simple format.
    Returns (companies_in_file, new_search_terms_added).
    """
    count_before = len(companies)
    rows = 0
    with open(csv_path, 'r', encoding='utf-8-sig') as f:
        reader = csv.DictReader(f)
        for row in reader:
            # Header names are matched case-insensitively ("COMPANY NAME" == "Company Name")
            row = {(k or '').strip().lower(): (v or '') for k, v in row.items()}
            symbol = row.get('symbol', '').strip().upper()
            name = row.get('company name', '').strip()

            canonical = name or symbol
            if canonical:
                rows += 1

            # Add symbol lowercase (e.g., "hdfcbank", "tcs", "reliance")
            if symbol and len(symbol) >= 2:
                companies.add(symbol.lower())
                aliases[symbol.lower()] = canonical

            if name:
                # Normalized name (e.g., "hdfc bank", "dr reddys laboratories")
                normalized = _normalize_company(name)
                if normalized and len(normalized) >= 2:
                    companies.add(normalized)
                    aliases[normalized] = canonical

                # Concatenated form (e.g., "hdfcbank" from "HDFC Bank")
                no_spaces = normalized.replace(' ', '')
                if no_spaces and no_spaces != normalized and len(no_spaces) >= 3:
                    companies.add(no_spaces)
                    aliases[no_spaces] = canonical

            # If CSV has an Aliases column (optional), parse it too
            alias_str = row.get('aliases', '')
            if alias_str:
                for alias in alias_str.split(','):
                    alias_clean = alias.strip().lower()
                    if alias_clean:
                        companies.add(alias_clean)
                        aliases[alias_clean] = canonical

    return rows, len(companies) - count_before


def load_nifty500_companies():
    """
    Load company filter lists from CSV files (Nifty 500 + additional).
    Auto-generates aliases from company names and symbols.
    Supports NSE CSV format: Company Name, Industry, Symbol, Series, ISIN Code
    Just drop a new CSV to update — no code changes needed.
    Returns (set_of_names, dict_of_aliases).
    Uses EXACT matching only — no substring matching to avoid false positives.
    """
    global _nifty500_companies, _nifty500_aliases

    if _nifty500_companies is not None:
        return _nifty500_companies, _nifty500_aliases

    companies = set()
    aliases = {}  # alias_lowercase -> canonical_name

    # Load primary list (Nifty 500)
    try:
        rows, terms = _load_companies_from_csv(NIFTY500_CSV_PATH, companies, aliases)
        print(f"Loaded {rows} companies ({terms} search terms) from {NIFTY500_CSV_PATH}")
    except FileNotFoundError:
        print(f"Warning: {NIFTY500_CSV_PATH} not found. Stockwatch filter disabled.")
    except Exception as e:
        log_error("nifty500_load", str(e), NIFTY500_CSV_PATH)
        print(f"Error loading Nifty 500 CSV: {e}")

    # Load additional company list (if configured)
    if ADDITIONAL_CSV_PATH:
        try:
            rows, terms = _load_companies_from_csv(ADDITIONAL_CSV_PATH, companies, aliases)
            print(f"Loaded {rows} additional companies ({terms} new search terms) from {ADDITIONAL_CSV_PATH}")
        except FileNotFoundError:
            print(f"Warning: {ADDITIONAL_CSV_PATH} not found. Skipping additional list.")
        except Exception as e:
            log_error("additional_csv_load", str(e), ADDITIONAL_CSV_PATH)
            print(f"Error loading additional CSV: {e}")

    print(f"Total company filter: {len(companies)} search terms")

    _nifty500_companies = companies
    _nifty500_aliases = aliases
    return companies, aliases


def is_nifty500_match(company_name, headline_text="", alt_name=""):
    """Check if a Stockwatch company matches a Nifty 500 / additional company.
    Uses EXACT matching only — no substring matching.
    Checks both identifiers the API gives us: the ticker code (matches the CSV
    Symbol column) and the full company name (matches the CSV Company Name).
    """
    companies, aliases = load_nifty500_companies()

    if not companies:
        return True  # Fail-open: if CSV not loaded, allow all through

    # Try the ticker code first, then the full company name as a fallback.
    for candidate in (company_name, alt_name):
        if not candidate:
            continue
        # Normalize the candidate the same way as Nifty 500 names
        normalized = _normalize_company(candidate)

        # Exact match of normalized name (e.g., "hdfc bank" == "hdfc bank")
        if normalized in companies:
            return True

        # Exact match of concatenated form (e.g., "hdfcbank" == "hdfcbank")
        if normalized.replace(' ', '') in companies:
            return True

    # No substring matching, no headline text matching — exact only
    return False


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


def save_headline_to_db(headline_text, publish_timestamp, source="", company=""):
    """Save a headline to the database with actual publish timestamp in IST"""
    try:
        db = load_headlines_db()

        datetime_obj = parse_timestamp_to_datetime(publish_timestamp)
        if datetime_obj:
            formatted_timestamp = datetime_obj.strftime('%d %b %I:%M %p')
            formatted_date = datetime_obj.strftime('%Y-%m-%d')
        else:
            # Fallback: use current time if timestamp is empty or unparseable
            if not publish_timestamp or len(publish_timestamp) > 50:
                formatted_timestamp = datetime.now(IST).strftime('%d %b %I:%M %p')
            else:
                formatted_timestamp = publish_timestamp
            formatted_date = datetime.now(IST).strftime('%Y-%m-%d')

        entry = {
            "headline": headline_text,
            "timestamp": formatted_timestamp,
            "date": formatted_date,
            "source": source,
            "company": company
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


def cleanup_database():
    """Clean up database at startup: remove non-Nifty500 Stockwatch entries and duplicates."""
    try:
        db = load_headlines_db()
        if not db:
            return

        nifty500_companies, _ = load_nifty500_companies()
        seen_headlines = set()
        cleaned = []
        removed = 0

        for entry in db:
            headline = entry.get('headline', '')
            source = entry.get('source', '')
            company = entry.get('company', '')

            # Remove duplicates (same headline text)
            if headline in seen_headlines:
                removed += 1
                continue
            seen_headlines.add(headline)

            # Remove non-Nifty500 Stockwatch entries
            if source == 'STOCKWATCH' and nifty500_companies:
                if not is_nifty500_match(company):
                    removed += 1
                    continue

            # Fix broken timestamps (headline text stored as timestamp)
            ts = entry.get('timestamp', '')
            if len(ts) > 50:
                entry['timestamp'] = entry.get('date', datetime.now(IST).strftime('%Y-%m-%d'))

            cleaned.append(entry)

        if removed > 0:
            with open(HEADLINES_DB_FILE, 'w') as f:
                json.dump(cleaned, f, indent=2)
            print(f"Database cleanup: removed {removed} entries (duplicates/non-Nifty500)")
        else:
            print("Database cleanup: no entries to remove")
    except Exception as e:
        log_error("cleanup_database", str(e), None)
        print(f"Database cleanup error: {e}")


_BOARD_NOTICE_RE = re.compile(
    r'\bboard\s+meeting\b'
    r'|\bboard\s+(?:to|will|shall)\s+(?:consider|approve|discuss|evaluate|review)\b',
    re.IGNORECASE,
)


def is_board_meeting_notice(headline_text):
    """True for advance board-meeting notices (not actual board decisions), e.g.
    "Ideaforge Technology Board Meeting on Nov 2, 2026",
    "Engineers India Ltd: Board to Consider Interim Dividend FY27".
    "Avalon Technologies Board Approves Director Appointments" is NOT a notice.
    Nuvama sometimes joins several items with "||"; such a line is only treated
    as a notice if every item in it is one, so real news isn't dropped with it.
    """
    parts = [p for p in headline_text.split('||') if p.strip()]
    return bool(parts) and all(_BOARD_NOTICE_RE.search(p) for p in parts)


def is_results_headline(headline_text, category=""):
    """
    Detect if a headline is a results/earnings headline.
    Uses both category tag AND content-based detection for reliability.
    The category tag from Nuvama is often not captured correctly,
    so content-based detection is the primary method.
    """
    # Check category tag first (may be unreliable)
    cat = category.strip().lower()
    if cat in ("result", "results", "earning", "earnings") or "result" in cat:
        return True  # incl. Stockwatch's "Quarterly Result" category

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
        # Results announcements without figures, e.g.
        # "Bansal Wire Industries Declares Un-Audited Financial Results for ..."
        r'\bfinancial results?\b',
        r'\b(?:un-?\s?audited|audited)\b.*\bresults?\b',
        r'\b(?:quarterly|half[- ]yearly|annual|q[1-4](?:\s*fy\s*\d+)?)\s+results?\b',
    ]
    for pattern in results_patterns:
        if re.search(pattern, text_lower):
            return True

    return False


def normalize_headline_for_exact_dedup(headline_text):
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


# ---------- CONTEXTUAL DEDUP (Layer B) ----------

# Stop words to filter out before computing content-word overlap.
# These dilute Jaccard/containment scores without adding signal.
STOP_WORDS = frozenset({
    'a', 'an', 'the', 'in', 'on', 'at', 'to', 'for', 'of', 'by', 'from',
    'with', 'as', 'is', 'was', 'are', 'were', 'be', 'been', 'being',
    'and', 'or', 'not', 'it', 'its', 'this', 'that', 'has', 'have', 'had',
    'will', 'would', 'can', 'could', 'should', 'may', 'might', 'shall',
    'do', 'does', 'did', 'but', 'if', 'so', 'no', 'up', 'out', 'into',
    'than', 'then', 'also', 'about', 'after', 'before', 'between',
    'through', 'during', 'above', 'below', 'over', 'under', 'per', 'via',
    'vs', 'all', 'each', 'every', 'both', 'more', 'most', 'other',
    'some', 'such', 'only', 'own', 'same', 'very', 'just', 'being',
    's', 'us',  # fragments left after punctuation removal
})

VERB_CANON = {
    'secured': 'wins', 'bagged': 'wins', 'got': 'wins', 'receives': 'wins',
    'awarded': 'wins', 'clinches': 'wins', 'grabs': 'wins', 'obtains': 'wins',
    'rises': 'increases', 'surges': 'increases', 'jumps': 'increases',
    'soars': 'increases', 'climbs': 'increases', 'gains': 'increases',
    'falls': 'decreases', 'drops': 'decreases', 'declines': 'decreases',
    'dips': 'decreases', 'slips': 'decreases', 'tumbles': 'decreases',
    'plunges': 'decreases', 'slumps': 'decreases',
    'acquires': 'buys', 'purchases': 'buys',
    'launches': 'introduces', 'unveils': 'introduces', 'rolls out': 'introduces',
    'appoints': 'names', 'designates': 'names', 'elevates': 'names',
}

UNIT_CANON = {
    'cr': 'crore', 'crs': 'crore', 'crores': 'crore',
    'mn': 'million', 'mln': 'million', 'mil': 'million',
    'bn': 'billion', 'bln': 'billion',
    'lk': 'lakh', 'lks': 'lakh', 'lakhs': 'lakh', 'lac': 'lakh',
    'k': 'thousand', 'tn': 'trillion',
    'rs': 'rupees', 'inr': 'rupees',
}


def canonicalize_for_context(text):
    """
    Normalize headline text for contextual comparison.
    More aggressive than exact-dedup normalization.
    Designed for cross-source dedup (Nuvama vs Stockwatch).
    """
    text = text.lower().strip()
    # Remove stock price percentages like (-0.49%) or (+2.10%)
    text = re.sub(r'\([+-]?\d+\.\d+%\)', '', text)
    # Remove Nuvama's "- :" placeholder
    text = re.sub(r'[-\u2013\u2014]\s*:\s*', ' ', text)
    # Remove standalone colons (left after price removal)
    text = re.sub(r'\s*:\s*', ' ', text)
    # Remove non-breaking spaces
    text = text.replace('\xa0', ' ')
    # Normalize comma-separated numbers BEFORE removing special chars: 2,000 → 2000
    text = re.sub(r'(\d),(\d)', r'\1\2', text)
    text = re.sub(r'(\d),(\d)', r'\1\2', text)  # Run twice for Indian format (10,00,000)
    # Remove ALL special chars except alphanumeric and spaces
    text = re.sub(r'[^\w\s]', ' ', text)
    # Strip common suffixes from company names
    text = re.sub(r'\bltd\b', '', text)
    text = re.sub(r'\blimited\b', '', text)
    # Normalize common plural/singular financial terms
    text = re.sub(r'\bcrores?\b', 'crore', text)
    text = re.sub(r'\bncds?\b', 'ncd', text)
    text = re.sub(r'\bshares?\b', 'share', text)
    text = re.sub(r'\borders?\b', 'order', text)
    text = re.sub(r'\brunits?\b', 'unit', text)

    # Canonicalize financial units and verbs
    words = text.split()
    canonical_words = []
    for w in words:
        if w in UNIT_CANON:
            canonical_words.append(UNIT_CANON[w])
        elif w in VERB_CANON:
            canonical_words.append(VERB_CANON[w])
        else:
            canonical_words.append(w)

    text = ' '.join(canonical_words)
    text = re.sub(r'\s+', ' ', text).strip()
    return text


def extract_numbers(text):
    """Extract all numeric values from text as a set of floats.
    Handles comma-separated numbers like 2,000 and Indian format 10,00,000.
    """
    numbers = set()
    # Normalize commas in numbers first: 2,000 → 2000
    cleaned = re.sub(r'(\d),(\d)', r'\1\2', text)
    cleaned = re.sub(r'(\d),(\d)', r'\1\2', cleaned)  # Run twice for Indian format
    for match in re.findall(r'(\d+(?:\.\d+)?)', cleaned):
        try:
            val = float(match)
            if val > 0:  # Skip 0 (noise from cleaned text)
                numbers.add(val)
        except ValueError:
            pass
    return numbers


def company_alias_overlap(text_a, text_b):
    """
    Compare two texts for company name overlap using Nifty500 aliases.
    Uses word-boundary matching to avoid false positives.
    E.g., alias "rec" must NOT match inside "Receives".
    Returns containment score 0.0-1.0 (fraction of shorter set in longer).
    """
    _, aliases = load_nifty500_companies()

    companies_a = set()
    companies_b = set()

    text_a_lower = text_a.lower()
    text_b_lower = text_b.lower()
    # Tokenize for single-word alias matching (O(1) set lookup, no substring)
    tokens_a = set(re.findall(r'\b\w+\b', text_a_lower))
    tokens_b = set(re.findall(r'\b\w+\b', text_b_lower))

    for alias, canonical in aliases.items():
        if len(alias) < 3:
            continue  # Skip 2-char aliases — too many false matches
        canonical_lower = canonical.lower()
        if ' ' in alias:
            # Multi-word alias (e.g., "natco pharma"): substring match is safe
            if alias in text_a_lower:
                companies_a.add(canonical_lower)
            if alias in text_b_lower:
                companies_b.add(canonical_lower)
        else:
            # Single-word alias (e.g., "natco", "rec"): word-level match only
            if alias in tokens_a:
                companies_a.add(canonical_lower)
            if alias in tokens_b:
                companies_b.add(canonical_lower)

    if not companies_a or not companies_b:
        return 0.0

    intersection = companies_a & companies_b
    # Use containment (shorter set into longer) instead of Jaccard.
    # If the shorter headline mentions 1 company and it's in the longer headline, score = 1.0.
    # This avoids dilution from extra false-positive matches in one headline.
    min_size = min(len(companies_a), len(companies_b))
    return len(intersection) / min_size if min_size else 0.0


def contextual_similarity_score(headline_a, headline_b):
    """
    Compute weighted contextual similarity between two headlines.
    Returns float 0.0-1.0.
    Designed to catch cross-source duplicates (same news from Nuvama + Stockwatch).

    Uses 5 signals:
    1. SequenceMatcher ratio (character-level similarity)
    2. Content-word Jaccard (stop words removed, meaningful words only)
    3. Content-word containment (what % of shorter headline's words appear in longer)
    4. Numeric overlap (shared amounts/figures — extracted from canonical text)
    5. Company alias overlap (word-boundary matching, containment-based)

    Containment (#3) is critical because cross-source duplicates are often
    one short + one long version of the same news. E.g.:
      Short: "Natco Pharma Receives Establishment Inspection Report from USFDA"
      Long:  "Natco Pharma Gets US FDA's Establishment Inspection Report For
              Chennai API Unit, Inspections Marked As Voluntary Action..."
    The short headline is almost fully contained in the long one.
    """
    canon_a = canonicalize_for_context(headline_a)
    canon_b = canonicalize_for_context(headline_b)

    # 1. SequenceMatcher text similarity
    seq_score = SequenceMatcher(None, canon_a, canon_b).ratio()

    # 2 & 3. Content-word Jaccard + Containment (filter stop words for better signal)
    words_a = canon_a.split()
    words_b = canon_b.split()
    content_a = set(w for w in words_a if w not in STOP_WORDS and len(w) > 2)
    content_b = set(w for w in words_b if w not in STOP_WORDS and len(w) > 2)

    if content_a and content_b:
        common_content = content_a & content_b
        # Jaccard: symmetric overlap
        content_jaccard = len(common_content) / len(content_a | content_b)
        # Containment: what % of the SHORTER headline's content is in the longer one
        # Key insight: cross-source dups are often short vs long versions of same news
        content_containment = len(common_content) / min(len(content_a), len(content_b))
    else:
        content_jaccard = 0.0
        content_containment = 0.0

    # 4. Numeric overlap — use CANONICAL text (not original) to avoid
    #    picking up stock price changes like (-1.60%) which are noise
    nums_a = extract_numbers(canon_a)
    nums_b = extract_numbers(canon_b)
    if nums_a or nums_b:
        num_overlap = len(nums_a & nums_b) / len(nums_a | nums_b)
    else:
        num_overlap = 0.5  # No numbers in either — neutral

    # 5. Company alias overlap (word-boundary matching, containment-based)
    company_score = company_alias_overlap(headline_a, headline_b)

    # 6. Embedding similarity (optional)
    embedding_score = 0.0
    embedding_enabled = False
    if ENABLE_EMBEDDING_DEDUP:
        try:
            embedding_score = compute_embedding_similarity(headline_a, headline_b)
            embedding_enabled = True
        except Exception:
            pass

    # Weighted combination
    if embedding_enabled:
        score = (
            0.15 * seq_score +
            0.15 * content_jaccard +
            0.15 * content_containment +
            0.10 * num_overlap +
            0.25 * company_score +
            0.10 * embedding_score
        )
    else:
        score = (
            0.20 * seq_score +
            0.20 * content_jaccard +
            0.20 * content_containment +
            0.15 * num_overlap +
            0.25 * company_score
        )

    # Same-company bonus: when the same company appears in both headlines,
    # they're likely about the same event across sources.
    if company_score >= 0.5:
        score += 0.08

    return min(score, 1.0)


def load_context_memory():
    """Load contextual dedup memory from file"""
    try:
        with open(CONTEXT_MEMORY_FILE, 'r') as f:
            data = json.load(f)
            if isinstance(data, list):
                return data
            return []
    except:
        return []


def save_context_memory(memory):
    """Save contextual dedup memory, pruning old entries"""
    try:
        cutoff = datetime.now(IST) - timedelta(hours=24)
        pruned = []
        for entry in memory:
            try:
                entry_time = datetime.fromisoformat(entry['timestamp'])
                if entry_time > cutoff:
                    pruned.append(entry)
            except:
                pruned.append(entry)

        pruned = pruned[-200:]

        with open(CONTEXT_MEMORY_FILE, 'w') as f:
            json.dump(pruned, f, indent=2, default=str)
    except Exception as e:
        log_error("save_context_memory", str(e), f"Memory entries: {len(memory)}")


def _content_words(text):
    return set(w for w in canonicalize_for_context(text).split()
               if w not in STOP_WORDS and len(w) > 2)


def _company_name_words():
    """All words that make up a known company name/symbol (e.g. "patel", "retail").
    Excluded when comparing key words, so a shared company name alone can't
    make two different stories look alike."""
    _, aliases = load_nifty500_companies()
    words = set()
    for alias in aliases:
        words.update(alias.split())
    return words


def description_containment(headline_a, desc_a, headline_b, desc_b):
    """
    Catch same-company duplicates that are worded differently, using the
    Stockwatch description. E.g. Nuvama "Patel Retail Chooses Two New Sites For
    Future Stores In Maharashtra: Ghoti In Nashik..." vs Stockwatch "Patel Retail
    Finalises 2 New Store Locations in Maharashtra" (description names Ghoti,
    Nashik, Mangaon/Mahad, Raigad). Returns the fraction of one headline's
    content words found in the other item's headline + description, or 0.0 if
    neither side has a description, the companies differ, or too few words match.
    """
    if not desc_a and not desc_b:
        return 0.0
    if company_alias_overlap(headline_a, headline_b) < 0.5:
        return 0.0

    name_words = _company_name_words()
    best = 0.0
    for head, other_full in ((headline_a, f"{headline_b} {desc_b}"),
                             (headline_b, f"{headline_a} {desc_a}")):
        words = _content_words(head) - name_words
        if not words:
            continue
        common = words & _content_words(other_full)
        if len(common) >= 4:
            best = max(best, len(common) / len(words))
    return best


def is_context_duplicate(headline_text, context_memory, source="", description=""):
    """
    Check if headline is a contextual duplicate of a recent alert from the OTHER source.
    Only compares cross-source (Nuvama vs Stockwatch), not within the same source.
    Returns (is_duplicate, best_match_score, matched_headline)
    """
    if not context_memory:
        return False, 0.0, ""

    best_score = 0.0
    best_match = ""

    for entry in context_memory:
        # Only compare against headlines from a DIFFERENT source
        entry_source = entry.get('source', '')
        if source and entry_source and entry_source == source:
            continue  # Skip same-source entries

        stored_headline = entry.get('headline', '')
        score = contextual_similarity_score(headline_text, stored_headline)
        # Same company + most key words appear in the other side's description
        if description_containment(headline_text, description, stored_headline,
                                   entry.get('description', '')) >= DESCRIPTION_DEDUP_THRESHOLD:
            score = max(score, 1.0)
        if score > best_score:
            best_score = score
            best_match = stored_headline

    is_dup = best_score >= CONTEXTUAL_DEDUP_THRESHOLD
    return is_dup, best_score, best_match


def add_to_context_memory(context_memory, headline_text, source, company="", embedding=None, description=""):
    """Add a sent headline to contextual memory for future dedup"""
    canon = canonicalize_for_context(headline_text)
    entry = {
        'headline': headline_text,
        'description': description,
        'canonical': canon,
        'tokens': canon.split(),
        'timestamp': datetime.now(IST).isoformat(),
        'source': source,
        'company': company,
        'embedding': embedding
    }
    context_memory.append(entry)
    return context_memory


# ---------- OPTIONAL EMBEDDING DEDUP ----------

_embedding_model = None


def get_embedding_model():
    """Lazy-load the sentence transformer model"""
    global _embedding_model
    if _embedding_model is not None:
        return _embedding_model

    try:
        from sentence_transformers import SentenceTransformer
        _embedding_model = SentenceTransformer(EMBEDDING_MODEL_NAME)
        print(f"Loaded embedding model: {EMBEDDING_MODEL_NAME}")
        return _embedding_model
    except ImportError:
        print("Warning: sentence-transformers not installed. Embedding dedup disabled.")
        return None
    except Exception as e:
        log_error("embedding_load", str(e), EMBEDDING_MODEL_NAME)
        print(f"Warning: Failed to load embedding model: {e}")
        return None


def compute_embedding_similarity(text_a, text_b):
    """Compute cosine similarity between two texts using embeddings"""
    model = get_embedding_model()
    if model is None:
        raise RuntimeError("Embedding model not available")

    embeddings = model.encode([text_a, text_b])
    from numpy import dot
    from numpy.linalg import norm
    cos_sim = dot(embeddings[0], embeddings[1]) / (norm(embeddings[0]) * norm(embeddings[1]))
    return float(cos_sim)


def get_embedding(text):
    """Get embedding vector for a single text (for storage in context memory)"""
    model = get_embedding_model()
    if model is None:
        return None
    try:
        return model.encode(text).tolist()
    except:
        return None


def check_and_notify():
    """Check for new headlines from all sources and send notifications"""
    print("=" * 60)
    print(f"Checking... {datetime.now(IST).strftime('%Y-%m-%d %I:%M:%S %p IST')}")

    heartbeat()
    seen_ids = load_seen()
    context_memory = load_context_memory()
    headlines = get_all_headlines()

    if not headlines:
        print("No headlines found from any source")
        return

    # Load last check timestamp for filtering
    last_check = load_last_check_timestamp()
    if last_check:
        print(f"Last check was: {last_check.strftime('%d %b %I:%M %p IST')}")

    # Load Nifty 500 for Stockwatch filtering
    nifty500_companies, _ = load_nifty500_companies()

    for h in headlines:
        heartbeat()
        headline_text = h['headline']
        timestamp = h['timestamp']
        datetime_obj = h['datetime']
        category = h.get('category', '')
        source = h.get('source', 'NUVAMA')
        company = h.get('company', '')
        description = h.get('description', '')

        # --- LAYER A: Exact dedup ---
        headline_normalized = normalize_headline_for_exact_dedup(headline_text)
        h_id = hashlib.md5(headline_normalized.encode()).hexdigest()

        if h_id in seen_ids:
            continue
        seen_ids.add(h_id)

        # --- Nifty 500 filter (Stockwatch only) ---
        if source == 'STOCKWATCH' and nifty500_companies:
            if not is_nifty500_match(company, headline_text, h.get('company_name', '')):
                print(f"Filtered [Not Nifty500]: {headline_text[:60]}...")
                continue

        # --- Timestamp filter ---
        should_send_alert = True
        if last_check and datetime_obj:
            if datetime_obj <= last_check:
                should_send_alert = False
                print(f"Skipping old [{source}]: {headline_text[:50]}... [{timestamp}]")

        # --- Always save to database ---
        save_headline_to_db(headline_text, timestamp, source, company)

        if not should_send_alert:
            continue

        # --- Board meeting notice filter ---
        if EXCLUDE_BOARD_MEETING_NOTICES and is_board_meeting_notice(headline_text):
            print(f"Filtered [Board Meeting Notice] [{source}]: {headline_text[:70]}...")
            continue

        # --- Results/earnings filter ---
        if EXCLUDE_RESULTS_ALERTS and is_results_headline(headline_text, category):
            print(f"Filtered [Result] [{source}]: {headline_text[:70]}...")
            continue

        # --- LAYER B: Contextual dedup ---
        is_dup, score, matched = is_context_duplicate(headline_text, context_memory, source, description)
        if is_dup:
            print(f"Filtered [Cross-Source Dup {score:.2f}] [{source}]: {headline_text[:60]}...")
            print(f"  Matched: {matched[:60]}...")
            continue

        # --- SEND TO TELEGRAM ---
        print(f"Sending [{source}]: {headline_text[:70]}...")
        if send_telegram(headline_text, source, company, description):
            embedding = get_embedding(headline_text) if ENABLE_EMBEDDING_DEDUP else None
            context_memory = add_to_context_memory(context_memory, headline_text, source, company, embedding, description)
            time.sleep(2)

    # Persist state
    save_seen(seen_ids)
    save_context_memory(context_memory)
    save_last_check_timestamp()

    print(f"Check complete. Context memory: {len(context_memory)} entries")


# Main loop
print("=" * 60)
print("UNIFIED NEWS MONITOR - NUVAMA + STOCKWATCH")
print("=" * 60)
print(f"Checking every {CHECK_INTERVAL_SECONDS} seconds")
print(f"IST Timezone: UTC+5:30")
print(f"Stockwatch feed: {ENABLE_STOCKWATCH}")
print(f"Exclude results alerts: {EXCLUDE_RESULTS_ALERTS}")
print(f"Exclude board meeting notices: {EXCLUDE_BOARD_MEETING_NOTICES}")
print(f"Embedding dedup: {ENABLE_EMBEDDING_DEDUP}")
print(f"Contextual dedup threshold: {CONTEXTUAL_DEDUP_THRESHOLD}")
print("=" * 60 + "\n")

# Load Nifty 500 at startup
load_nifty500_companies()

# Clean up database: remove non-Nifty500 entries, duplicates, broken timestamps
cleanup_database()

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
    "🔄 Unified Monitor restarted! Tracking Nuvama + Stockwatch. Only new headlines will be sent."
)

# Start the watchdog so even a hung baseline scrape self-recovers
start_watchdog()

# Initial baseline with timestamp-based filtering
print("Setting baseline...")
initial_headlines = get_all_headlines()
initial_seen = load_seen()  # Load existing seen IDs
initial_context_memory = load_context_memory()
nifty500_companies, _ = load_nifty500_companies()

for h in initial_headlines:
    heartbeat()
    headline_text = h['headline']
    timestamp = h['timestamp']
    datetime_obj = h['datetime']
    category = h.get('category', '')
    source = h.get('source', 'NUVAMA')
    company = h.get('company', '')
    description = h.get('description', '')

    # Nifty 500 filter (Stockwatch only) — apply to ALL headlines, not just new
    if source == 'STOCKWATCH' and nifty500_companies:
        if not is_nifty500_match(company, headline_text, h.get('company_name', '')):
            headline_normalized = normalize_headline_for_exact_dedup(headline_text)
            h_id = hashlib.md5(headline_normalized.encode()).hexdigest()
            initial_seen.add(h_id)
            continue

    # Layer A: Exact dedup
    headline_normalized = normalize_headline_for_exact_dedup(headline_text)
    h_id = hashlib.md5(headline_normalized.encode()).hexdigest()

    is_new = h_id not in initial_seen
    should_alert = False

    if is_new:
        # Only save NEW headlines to database (don't re-insert old ones on restart)
        save_headline_to_db(headline_text, timestamp, source, company)

        if last_check and datetime_obj:
            if datetime_obj > last_check:
                should_alert = True

    # Send alert only if truly new and newer than last check
    if is_new and should_alert:
        if EXCLUDE_BOARD_MEETING_NOTICES and is_board_meeting_notice(headline_text):
            print(f"[FILTERED] Board meeting notice skipped [{source}]: {headline_text[:60]}...")
        elif EXCLUDE_RESULTS_ALERTS and is_results_headline(headline_text, category):
            print(f"[FILTERED] Result skipped [{source}]: {headline_text[:60]}...")
        else:
            # Layer B: Contextual dedup
            is_dup, score, matched = is_context_duplicate(headline_text, initial_context_memory, source, description)
            if is_dup:
                print(f"[Cross-Source Dup {score:.2f}] [{source}]: {headline_text[:60]}...")
            else:
                print(f"[ALERT] New during downtime [{source}]: {headline_text[:60]}...")
                if send_telegram(headline_text, source, company, description):
                    embedding = get_embedding(headline_text) if ENABLE_EMBEDDING_DEDUP else None
                    initial_context_memory = add_to_context_memory(
                        initial_context_memory, headline_text, source, company, embedding, description
                    )
                    time.sleep(2)

    initial_seen.add(h_id)

save_seen(initial_seen)
save_context_memory(initial_context_memory)
save_last_check_timestamp()
print(f"Baseline set: {len(initial_headlines)} current headlines\n")

# Run forever
check_count = 0
while True:
    try:
        heartbeat()
        check_count += 1
        print(f"\n--- Check #{check_count} ---")
        check_and_notify()
        heartbeat()
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

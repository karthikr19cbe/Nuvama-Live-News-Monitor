# Overview

This is a **Nuvama News Monitor** system that scrapes financial news headlines from Nuvama Wealth's live news page and delivers them via Telegram notifications. The system includes a Flask-based web dashboard for viewing collected headlines in real-time. It runs continuously, checking for new headlines every 60 seconds, and maintains a persistent database of all headlines with timestamps in IST (Indian Standard Time).

**Key Features:**
- Real-time financial news monitoring from Nuvama Wealth
- Intelligent deduplication prevents duplicate alerts
- Timestamp-based filtering ensures only new headlines are sent on restart
- IST timezone support matching Nuvama's display
- Graceful restart handling with downtime detection
- Comprehensive error logging

The application consists of two main components:
1. A background news monitoring service (`main.py`) that scrapes headlines and sends Telegram alerts
2. A web dashboard (`app.py`) that displays the collected headlines in a user-friendly interface

# User Preferences

Preferred communication style: Simple, everyday language.

# System Architecture

## Application Structure

**Multi-Process Architecture**: The system uses a dual-process design orchestrated by `run_all.py`, which spawns two separate Python processes running concurrently:
- News monitoring service (`main.py`) - Runs in a daemon thread
- Web server (`app.py`) - Runs in the main thread

**Rationale**: This separation allows the CPU-intensive web scraping to run independently from the web server, preventing blocking and ensuring the dashboard remains responsive. Threading is used instead of multiprocessing to simplify state management and reduce resource overhead.

## Web Scraping Strategy

**Headless Browser with Playwright**: Uses Playwright's Chromium browser for scraping instead of simple HTTP requests.

**Rationale**: Nuvama's live news page likely uses JavaScript to render content dynamically, making a headless browser necessary to execute client-side code and access the DOM. Playwright was chosen over Selenium for better performance and more reliable automation.

## Data Storage & State Management

**JSON File-Based Persistence**: Four JSON files manage system state:
- `headlines_database.json` - Stores last 100 headlines with metadata (headline text, timestamp, date)
- `headlines_seen.json` - Tracks MD5 hashes of processed headlines to prevent duplicates (unlimited history)
- `last_check_timestamp.json` - Records last successful check timestamp for restart filtering
- `error_log.json` - Maintains last 100 error events for debugging

**Rationale**: For this use case with relatively small data volumes and simple querying needs, JSON files provide sufficient performance without the overhead of setting up a database server. This approach also ensures portability and simplifies deployment on Replit.

**Trade-offs**: 
- Pros: Zero configuration, portable, version-control friendly, human-readable state
- Cons: Not suitable for high-volume concurrent writes, lacks query optimization, entire file must be loaded into memory

**State Persistence Strategy**: 
- Deduplication tracking persists forever (no limit on `headlines_seen.json`)
- Dashboard shows last 100 headlines to prevent memory issues
- Last check timestamp enables smart restart behavior (only send truly new headlines)

## Notification System

**Telegram Bot API**: Direct HTTP API calls to Telegram's bot service for push notifications.

**Rationale**: Telegram provides a free, reliable messaging platform with a simple REST API. This eliminates the need for email server configuration or SMS costs. The bot approach allows users to receive notifications on any device with Telegram installed.

**Implementation Details**: Uses HTML parse mode for formatted messages with bold headers and clickable links, with web preview disabled to keep notifications compact.

## Deduplication Mechanism

**Multi-Layer Normalization + MD5 Hash Tracking**: Headlines undergo normalization before hashing to handle format variations:

1. **Stock Price Removal**: Strip percentages like "(+1.92%)" or "(-2.91%)" that change constantly
2. **Placeholder Normalization**: Convert Nuvama's "- :" placeholder to standard ": " format
3. **Whitespace Collapsing**: Reduce multiple spaces to single space
4. **Case Normalization**: Lowercase all text for case-insensitive matching
5. **MD5 Hashing**: Generate fixed-length fingerprint for O(1) lookup

**Example:**
```
Original: "ROUTE MOBILE (+1.92%) :  Q2 NET LOSS"
Normalized: "route mobile : q2 net loss"
Hash: 104508cef95cc50da7f44274379caf2e
```

**Rationale**: 
- Hashing provides fixed-length fingerprint regardless of headline length
- Normalization prevents false negatives when Nuvama changes formats
- MD5 is sufficient since cryptographic security isn't required
- Handles Nuvama's dynamic price updates without duplicate alerts

**Known Format Variations Handled**:
- "STOCK (+1.92%) :" vs "STOCK - :" vs "STOCK :"
- Case variations (uppercase vs mixed case)
- Multiple whitespace variations

## Frontend Architecture

**Server-Side Rendering with Flask Templates**: Uses Jinja2 templates to render the dashboard with initial data.

**Rationale**: For a simple dashboard that primarily displays static data, SSR is simpler than a full SPA framework. The design likely includes client-side JavaScript for auto-refresh functionality via the `/api/headlines` endpoint.

**Progressive Enhancement**: The API endpoint (`/api/headlines`) suggests the dashboard can fetch updates without full page reloads, providing a better user experience.

## Error Handling & Reliability

**Comprehensive Error Management**:

1. **Infinite Retry Loops**: Main loop wrapped in `while True` with exception catching
2. **Error Logging**: All errors logged to `error_log.json` with timestamps and context
3. **Graceful Degradation**: Scraping failures skip iteration; Telegram failures logged but don't crash system
4. **State Preservation**: Last check timestamp saved on clean shutdown (KeyboardInterrupt)
5. **Restart Detection**: System detects restarts and calculates downtime

**Error Types Tracked**:
- `scraping` - Playwright/network failures
- `telegram_send` - Telegram API errors
- `timestamp_parse` - Timestamp parsing failures
- `database_save` - File write errors
- `main_loop` - Unexpected exceptions in main loop

**Rationale**: For a long-running monitoring service, automatic recovery from transient failures (network issues, site changes, etc.) is essential. Error logging provides debugging visibility without crashing the system.

**Restart Behavior**:
- Detects last check timestamp on startup
- Calculates downtime duration
- Only sends Telegram alerts for headlines newer than last check
- Prevents duplicate spam on restart

**Example Restart Flow**:
```
Last check: 04 Nov 2025 11:30:00 PM IST
App stopped overnight
App restarted: 05 Nov 2025 09:00:00 AM IST
Downtime: 9 hours 30 minutes
Action: Fetch headlines, compare timestamps, send only those > 11:30 PM
```

# External Dependencies

## Third-Party Services

- **Telegram Bot API** (`api.telegram.org`) - Push notification delivery system
  - Configuration: Requires `TELEGRAM_TOKEN` and `TELEGRAM_CHAT_ID` environment variables
  - Fallback: No fallback mechanism; notifications fail silently if Telegram is unavailable

- **Nuvama Wealth Live News** (`https://www.nuvamawealth.com/live-news`) - Primary data source
  - Scraping frequency: Every 60 seconds
  - Risk: Breaking changes to page structure will cause scraping failures

## Python Libraries

- **Flask** - Web framework for dashboard server (port 5000)
- **Playwright** - Headless browser automation for web scraping
- **requests** - HTTP client for Telegram API calls
- **Standard library**: `json`, `hashlib`, `datetime`, `threading`, `subprocess`

## Runtime Environment

- **Port Binding**: Flask server binds to `0.0.0.0:5000`, making it accessible externally
- **Environment Variables**: 
  - `TELEGRAM_TOKEN` - Bot authentication token (hardcoded fallback present)
  - `TELEGRAM_CHAT_ID` - Target chat for notifications (hardcoded fallback present)

## File System Dependencies

- Requires read/write access to current directory for JSON state files
- No database server required
- Persistent storage needed to maintain headline history across restarts

## Timestamp Management

**IST (Indian Standard Time) Throughout**: All timestamps use UTC+5:30 to match Nuvama's display

**Timestamp Formats Handled**:
1. **"Just Now"** → Current IST time
2. **Relative**: "15 mins ago", "2 hours ago" → Calculated IST time
3. **Absolute**: "04 Nov 08:26 AM" → Parsed with current year + IST timezone

**Timestamp Parsing Flow**:
```python
"Just Now" → datetime.now(IST)
"15 mins ago" → datetime.now(IST) - timedelta(minutes=15)
"04 Nov 08:26 AM" → datetime(2025, 11, 4, 8, 26, tzinfo=IST)
```

**Timestamp-Based Filtering**:
- Last check timestamp stored in ISO format: `"2025-11-04T08:44:24.440820+05:30"`
- On restart, headlines with `datetime > last_check` are sent to Telegram
- All headlines saved to database regardless of timestamp
- Prevents missing headlines during downtime

## Known Issues & Limitations

### Issue 1: Missing Headlines During Long Downtime
**Problem**: Nuvama live news page only shows ~20 most recent headlines
**Impact**: If app is down >1 hour and >20 headlines published, some are lost forever
**Mitigation**: Run app 24/7 or accept potential gaps
**Root Cause**: Nuvama doesn't provide archive/pagination API

### Issue 2: Scraping Intermittent Failures
**Problem**: Playwright occasionally returns 0 headlines (network/timing issues)
**Impact**: Logged as error, retried on next check (60 seconds later)
**Mitigation**: Error logged to `error_log.json`, system auto-recovers

### Issue 3: Timestamp Granularity
**Problem**: Relative timestamps like "15 mins ago" lose precision
**Impact**: Two headlines at 8:15 AM might both show "15 mins ago" at 8:30 AM
**Mitigation**: Normalization prevents duplicates; dashboard shows calculated exact time

## Historical Issues (RESOLVED)

✅ **Duplicate Alerts on Restart** - Fixed with persistent `headlines_seen.json` loading
✅ **Format Variation Duplicates** - Fixed with multi-layer normalization
✅ **UTC/IST Mismatch** - Fixed with IST timezone throughout
✅ **Old Headlines Sent on Restart** - Fixed with timestamp-based filtering
# Overview

This is a **Nuvama News Monitor** system that scrapes financial news headlines from Nuvama Wealth's live news page and delivers them via Telegram notifications. The system includes a Flask-based web dashboard for viewing collected headlines in real-time. It runs continuously, checking for new headlines every 60 seconds, and maintains a persistent database of all headlines with timestamps.

The application consists of two main components:
1. A background news monitoring service that scrapes headlines and sends Telegram alerts
2. A web dashboard that displays the collected headlines in a user-friendly interface

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

## Data Storage

**JSON File-Based Persistence**: Two JSON files serve as the data layer:
- `headlines_database.json` - Stores all headlines with metadata (headline text, timestamp, date)
- `headlines_seen.json` - Tracks MD5 hashes of processed headlines to prevent duplicates

**Rationale**: For this use case with relatively small data volumes and simple querying needs, JSON files provide sufficient performance without the overhead of setting up a database server. This approach also ensures portability and simplifies deployment on Replit.

**Trade-offs**: 
- Pros: Zero configuration, portable, version-control friendly
- Cons: Not suitable for high-volume concurrent writes, lacks query optimization, entire file must be loaded into memory

## Notification System

**Telegram Bot API**: Direct HTTP API calls to Telegram's bot service for push notifications.

**Rationale**: Telegram provides a free, reliable messaging platform with a simple REST API. This eliminates the need for email server configuration or SMS costs. The bot approach allows users to receive notifications on any device with Telegram installed.

**Implementation Details**: Uses HTML parse mode for formatted messages with bold headers and clickable links, with web preview disabled to keep notifications compact.

## Deduplication Mechanism

**MD5 Hash-Based Tracking**: Each headline is hashed using MD5, and hashes are stored in `headlines_seen.json` to identify duplicates.

**Rationale**: Hashing provides a fixed-length fingerprint regardless of headline length, making comparisons efficient. MD5 is sufficient since cryptographic security isn't required for this use case.

**Alternative Considered**: Direct string comparison was rejected due to potential memory issues with large datasets and inability to handle minor variations in whitespace or formatting.

## Frontend Architecture

**Server-Side Rendering with Flask Templates**: Uses Jinja2 templates to render the dashboard with initial data.

**Rationale**: For a simple dashboard that primarily displays static data, SSR is simpler than a full SPA framework. The design likely includes client-side JavaScript for auto-refresh functionality via the `/api/headlines` endpoint.

**Progressive Enhancement**: The API endpoint (`/api/headlines`) suggests the dashboard can fetch updates without full page reloads, providing a better user experience.

## Error Handling & Reliability

**Infinite Retry Loops**: Both services are wrapped in `while True` loops with exception catching, automatically restarting after failures with a 10-second cooldown.

**Rationale**: For a long-running monitoring service, automatic recovery from transient failures (network issues, site changes, etc.) is essential. The 10-second delay prevents rapid restart loops that could exhaust resources.

**Trade-offs**: This approach masks errors and could hide persistent problems. A production system would benefit from logging, alerting, and circuit breakers.

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

- Requires read/write access to current directory for JSON database files
- No database server required
- Persistent storage needed to maintain headline history across restarts
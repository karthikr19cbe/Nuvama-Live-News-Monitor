# Nuvama News Monitor - Complete Issue Analysis

## Issues Encountered

### 1. Duplicate Alerts on System Restart (CRITICAL)
**Root Cause:** Baseline setting created empty seen_ids set, treating all current headlines as "new"
**Impact:** 15-20 duplicate Telegram messages on every restart
**Fix Applied:** Load existing headlines_seen.json during baseline
**Status:** ✅ Fixed

### 2. Headline Format Variations Causing Duplicates (CRITICAL)
**Root Cause:** Nuvama changes format from "STOCK (+1.92%) :" to "STOCK - :" when price unavailable
**Impact:** Same headline sent multiple times as price format changed
**Symptoms:** Route Mobile and other headlines sent 12 times
**Fix Applied:** Normalize "- :" placeholder and lowercase all text before hashing
**Status:** ✅ Fixed

### 3. Missing Headlines on Overnight Stop (ARCHITECTURAL LIMITATION)
**Root Cause:** Nuvama live news page only shows ~20 most recent headlines
**Impact:** Headlines that appear and disappear while app is stopped are lost forever
**Workaround:** Keep app running 24/7
**Status:** ⚠️ Inherent limitation - needs timestamp-based solution

### 4. UTC vs IST Timestamp Mismatch
**Root Cause:** System using UTC, Nuvama displays IST
**Impact:** Dashboard timestamps didn't match live news page
**Fix Applied:** Convert all timestamps to IST (UTC+5:30)
**Status:** ✅ Fixed

### 5. Baseline Headlines Not Sent on Restart
**Root Cause:** All baseline headlines marked as "seen" to prevent spam
**Impact:** Headlines that appeared during downtime saved to DB but no Telegram alert
**Fix Applied:** Intentional behavior to prevent spam
**Status:** ⚠️ Needs timestamp-based filtering

### 6. Scraping Intermittent Failures
**Root Cause:** Playwright occasionally returns 0 headlines (network/timing issues)
**Impact:** "No headlines found" in logs, missed checks
**Current Handling:** Retry on next cycle (60 seconds later)
**Status:** ⚠️ Needs better error handling

### 7. No Persistent "Last Check" Timestamp
**Root Cause:** System doesn't track when it was last running
**Impact:** Can't distinguish truly new headlines from old ones on restart
**Status:** ❌ Not implemented - CRITICAL MISSING FEATURE

## Recurring Patterns

1. **State Loss on Restart:** Fixed issues reappear because state isn't fully persisted
2. **Format Sensitivity:** Deduplication breaks when Nuvama changes formats
3. **Time-Based Logic Missing:** No way to filter by "what's new since timestamp X"

## Recommended Architecture Changes

### Priority 1: Timestamp-Based Baseline System
- Store last successful check timestamp in `last_check.json`
- On restart, compare headline timestamps vs last check
- Send only headlines newer than last check to Telegram
- Handles overnight stops gracefully

### Priority 2: Enhanced Error Handling
- Log all errors to file with timestamps
- Retry logic with exponential backoff
- Never lose state on errors

### Priority 3: Bulletproof Normalization
- Handle all known Nuvama format variations
- Future-proof against new formats
- Add debug logging for normalization

### Priority 4: State Validation
- Verify all JSON files on startup
- Auto-repair corrupted state
- Backup mechanism

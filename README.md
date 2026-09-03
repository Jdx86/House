# House

Automated monitor for houses ("moradia") for sale in the District of Porto,
Portugal, priced between €50,000 and €180,000, across Idealista, Imovirtual,
RE/MAX, ERA, and Century 21.

Runs every 2 hours via GitHub Actions (.github/workflows/monitor.yml), which
executes monitor.py on GitHub's own runners (real internet access, unlike a
Claude cloud sandbox).

## Files

- listings_db.json - source of truth: every qualifying listing ever detected,
  keyed by normalized URL, with title, price, source, first-seen timestamp.
- status.json - health of the most recent run: per-portal outcome
  (ok / empty / blocked / error), and whether the one-time silent backfill
  has completed (bootstrapped).
- monitor.py - the scanner: fetches current listings per portal, verifies
  each candidate against its own listing page (price, Porto District
  municipality, not rented, not sold/reserved), dedupes against
  listings_db.json, emails new qualifying listings via Resend.

## Design notes

- Never fabricates: every fact reported comes from a page actually fetched
  that run. An unreachable portal is recorded as blocked/error and skipped.
- First run is silent: backfills listings_db.json with everything currently
  live and qualifying, but sends no emails - only genuinely new listings from
  the second run onward get emailed.
- Century 21 coverage is partial: its search results only reliably expose a
  first batch of listings, so it will often report empty even when matching
  listings exist there. The other four portals have full coverage.
- Record before notify: a new listing is committed/pushed before its email
  is sent, so a failed email never causes a duplicate notification on retry.

## Secrets

RESEND_API_KEY (GitHub Actions repo secret) - a send-only Resend API key.

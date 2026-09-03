# House

Automated monitor for houses ("moradia") for sale in the District of Porto,
Portugal, priced between €50,000 and €180,000, across Idealista, Imovirtual,
RE/MAX, ERA, and Century 21.

- `listings_db.json` is the source of truth: every qualifying listing ever
  detected, keyed by its normalized URL.
- `status.json` records the health of the last run per portal (`ok`,
  `blocked`, `empty`, or `error`), plus the last run timestamp.

This repo is written to automatically by a scheduled Claude cloud routine
that runs every 2 hours. New qualifying listings trigger an email
notification; the first run only backfills the database silently.

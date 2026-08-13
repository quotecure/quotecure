# QuoteCure

Pool-remodeling cost-engine and quoting platform, built for a single pool company (owner: Jim, not a customer of ours — this repo *is* his business tool). Flask + PostgreSQL, Jinja2 templates, vanilla JS, one CSS file (`static/style.css`). The core differentiator is that a real cost engine (actual sub labor rates, actual material costs) drives every quote, with margin floors enforced in code — not a static price list, not just a policy someone can forget to follow.

## Read this first

**`CHANGELOG.md`** in this same directory is a plain-English running log of what's been built and why, newest first. Read it at the start of any nontrivial session to get oriented fast. **Add a new dated entry after finishing anything notable** (a feature, a pricing-model change, a deploy) — this is what lets continuity survive a context compaction that wipes session memory. If you (a future Claude session) don't remember something Jim references, check here before asking him to re-explain it.

## Where things run

- **Production**: https://quotecure.onrender.com — real customer data. Connected to this repo's `main` branch with auto-deploy: a plain `git push` is the entire deploy step, nothing else to run. Render builds from `Dockerfile` (not `Procfile` — needed for Playwright's Chromium to work for PDF generation). Real login accounts: `jim`, `doug`, `coordinator`. **Never enter production credentials on the user's behalf** — ask Jim to check something live himself rather than logging in as him.
- **Local dev**: reads `DATABASE_URL` from `.env` (a local Postgres db, `quotecure_dev`) — sample/seed data only, never real customers. Run with `python3 app.py` (loads `.env` via `python-dotenv` automatically). If port 5000 is already taken, that's usually macOS's AirPlay Receiver, not a real conflict — use `PORT=5050 python3 app.py` instead of touching system settings.

## Architecture, briefly

- `db_compat.py` — thin psycopg2 wrapper so the whole app can keep using SQLite-style `?` placeholders and `sqlite3.Row`-like dict/index access against real Postgres.
- `database.py` — schema plus an append-only `@migration` decorator system (tracked in a `schema_migrations` table); migrations run automatically on the first request after a deploy. Never edit an already-applied migration's SQL — add a new one, following the existing `PRAGMA table_info(...)` guard pattern for `ALTER TABLE`.
- `line_item_logic.py` — `calc_component()` / `build_line_item()`, the actual pricing primitives (cost × qty × markup, with a markup floor that can't be undercut without an explicit override permission).
- `app.py` — routes. Look for `_price_catalog_item`, `_compute_commission`, `_recalc_quote` / `_recalc_change_order` before writing new pricing or commission logic — these are "one calculation, multiple callers" helpers specifically extracted to avoid two copies of the same formula drifting apart. If you're about to duplicate one, it probably already exists.

## Working conventions established on this project

- Test against the local `quotecure_dev` database via `app.test_client()`: create test data, assert against it, then delete it in the same script. Never test destructively against production. Look at recent commits for the exact pattern used repeatedly.
- Prefer extending an existing shared helper over adding a second copy of pricing/commission/status logic.
- Jim generally wants to see the plan before large multi-file features (see how Change Orders & Contracts was scoped via a real back-and-forth before any code) — for anything touching money calculations or changing existing screen behavior, talk it through and confirm the approach first.

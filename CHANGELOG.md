# QuoteCure Changelog

Plain-English running log of what's been built and why — kept so a fresh session (Claude or human) can get oriented fast without re-deriving everything from `git log`. **Add a new dated entry here after finishing anything notable** (a feature, a pricing-model change, a deploy). Newest entries at the top.

---

## 2026-08-13 — Tiered gross-profit commission model

Replaced the flat commission rate with a tiered structure based on gross-profit *margin*, not price: below 20% margin pays 8%, 20–30% pays 10%, 30%+ pays 12%. Fully editable in Admin → Commission (thresholds and rates, not just on/off). Sales reps see a live "distance to next tier" nudge on the quote screen — e.g. *"1% more margin gets you to Tier 3 — that's +$54 on this job"* — that recalculates as markup changes. One shared `_compute_commission()` helper is used by both regular quotes and Change Orders, so payout is always consistent between the two rather than two copies of the same formula drifting apart.

## 2026-08-13 — Change Orders & Contracts

Signed quotes now become **Contracts** (`draft → sent → contract`), and their line items plus customer/dimension details lock hard once signed — no more direct edits, by design. Any scope change after signing goes through a **Change Order**:
- **Add** a catalog work type — priced through the exact same engine as a normal quote line (sub rates, materials, markup floors — `_price_catalog_item`, one function, not a duplicate).
- **Remove** one — credits it back as a negative line.
- **Amend** one — a paired remove (old spec) + add (new spec).
- **Freeform** one-off line — label + price + cost (cost required, for things like "cut and re-pour beam" that don't map to the catalog).

A signed CO document reproduces the real paper change-order format exactly: itemized changes, Original Contract Total, Previous Change Orders, Total Paid, Remaining Balance, New Remaining Balance — verified against a real example to the dollar. Payment collection is now tracked (checkbox + amount + date per payment-schedule line) so "Total Paid" is real, not estimated — this only applies going forward, nothing retroactive. New "Contracts" tab on the Quotes screen; signed Change Orders live nested under their parent contract, never as their own top-level row.

## 2026-08-11 — Loss-leader pricing, dual tile picks, customer-view item editing

Surface Removal repriced as a deliberate loss-leader (15% markup, down from 25%) with a $2,800 minimum job price floor so small jobs don't go underwater. The tile-selection guide now handles a quote with *both* a Cap Tile and a Waterline Tile line, letting staff pick each independently instead of only the waterline tile. The simplified customer-facing quote screen can now have items added/removed by staff without ever exposing cost/margin/commission internals.

## 2026-08-10 / 2026-08-11 — Customer-facing tile & quote screens

Built a price-tier-filtered tile picker (LUV Tile and Artistry In Mosaics kept as separate manufacturer tabs, matched to real product photos via web scraping / PDF price-sheet extraction) shown right after a customer picks a package tier, followed by a simplified customer-facing quote screen — editable by staff, but with all cost/margin/commission internals hidden from view.

## 2026-08-09 — AI pool visualization, tax, pricing fixes

Added "what your pool could look like" AI image generation (Gemini `gemini-3-pro-image`) from a customer's own photo. Fixed a Freight qualifier markup bug, added material sales tax (%), and added water-surface-area-based pricing for applicators that need it (SWPF).

## 2026-08-08 — Migrated to PostgreSQL, deployed to Render

Moved off SQLite entirely — `db_compat.py` transparently translates the app's SQLite-style `?`-placeholder queries and `sqlite3.Row` access to Postgres, so the ~300 existing call sites in `app.py`/`database.py` needed zero changes.

**Deployed to production at https://quotecure.onrender.com**, connected to this GitHub repo with auto-deploy on push to `main` — a plain `git push` is the entire deploy step, nothing else to run. Render builds from the `Dockerfile` (not the `Procfile` — buildpacks couldn't get Playwright's Chromium working for PDF generation, so this switched to a Docker deploy partway through). Real login accounts: `jim`, `doug`, `coordinator` — passwords were left as setup defaults (`owner123`/`sales123`/`coordinator123`) with a to-do to change them via Admin → Permissions; don't assume they're still defaults without checking.

---

### Standing facts (update in place if they change, don't re-state every entry)

- **Local dev** (`quotecure_dev`, via `DATABASE_URL` in `.env`) has sample/seed data only — never real customers. All testing this project has used `app.test_client()` against it: create test data, assert, delete the test data. Never test destructively against production.
- If local port 5000 is taken when running `python3 app.py`, that's usually macOS's AirPlay Receiver/ControlCenter, not a real conflict — run `PORT=5050 python3 app.py` instead rather than changing system settings.

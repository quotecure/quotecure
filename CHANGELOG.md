# QuoteCure Changelog

Plain-English running log of what's been built and why — kept so a fresh session (Claude or human) can get oriented fast without re-deriving everything from `git log`. **Add a new dated entry here after finishing anything notable** (a feature, a pricing-model change, a deploy). Newest entries at the top.

---

## 2026-08-13 — Editable package items + Robert as default skimmer sub

Skimmer Installation had no default sub on any of the three packages (blank `default_sub_id`), so a package-generated quote always priced skimmer labor at $0. Set Robert (sub S9, already rated at $400/each) as the default across Refresh/Signature/Resort. While fixing this, noticed Admin → Packages had no way to *edit* an existing item's sub/material at all — only add a new item or toggle one active/optional — so a wrong default like this had no fix short of removing and re-adding the whole line. Added a real edit route + inline edit form (mirrors the Add Item cascading dropdowns) so any package item's sub and material can be changed directly.

## 2026-08-13 — Package pricing refresh + Flagstone Pavers

Updated package price ranges: Refresh $10,000–$20,000 (unchanged scope, no decking), Signature $20,000–$30,000 (now includes Textured Decking – Knockdown, auto-priced off `deck_sqft` — added to the quote at $0 if no decking dimension was entered, same as any other dimension-driven item, not omitted), Resort $30,000+ (now includes **Flagstone Pavers**, a new dedicated work type; Coping Installation was already in Resort's original seed and needed no change since it already derives from pool perimeter).

Flagstone Pavers is its own catalog entry rather than a material bolted onto the existing Paver Installation work type — a material's price always inherits its work type's single shared `default_markup`, and changing Paver Installation's default markup to hit Jim's price target would have silently repriced any other future plain Paver Installation line, on any quote, not just Resort's. Cost is $3.50/sqft material (new Flagstone supplier + material row, taxable) + $2.10/sqft labor (S3's existing paver-install rate, reused as-is) = $5.60/sqft; `default_markup` is solved backward (87.5%) so it lands on Jim's ~$10.50/sqft target exactly before tax. Gets its own Freight qualifier ($300, mirrors Coping/Paver Installation) since qualifiers are scoped per work type; the Tax toggle is already generic and needed no new wiring.

Also fixed a real pre-existing bug surfaced while building this: `init_materials()` seeds suppliers/materials (LUV Tile, Flagstone, NPT, SCP, and every tile/glass material) only if the `suppliers` table is completely empty, but it runs *after* migrations in `app.py`'s bootstrap — on at least one environment, a migration (`add_light_fixtures`) inserted its own suppliers first, causing `init_materials()` to silently no-op and skip its entire seed list. Not fixed at the root (`init_materials()`'s call order / guard) since that's out of scope here and risks masking real data in an environment that already has it — flagging for a dedicated look.

## 2026-08-13 — Decking dimension

Added `deck_sqft` to the dimension page (both New Quote and Edit Details) — a direct-entry field, since a deck has no fixed shape relative to the pool the way pool_sqft does (perimeter × depth formula). It's tucked behind a closed-by-default toggle (same switch pattern as Spa/Sunshelf), opening automatically on Edit Details when a quote already has a deck sqft on file. It drives quantity for any deck-related work type wherever line items get auto-priced from dimensions (`_compute_line_item_pricing` — quick-add, package auto-populate, customer add-item): Paver Installation, Paver Sealing, and the two new Textured Decking work types below — matched by work-type *name* now (`DECK_SQFT_WORK_TYPES`), not by ID, since later-seeded catalog rows don't get deterministic IDs across environments the way the original seed data does. Coping Installation needed no change — it already derives correctly from pool perimeter (+ spa perimeter) via the existing `total_lf` calculation.

Added **Textured Decking** as a new catalog item, sold in two finish options — Knockdown ($3.50 cost → $6.50 price /sqft) and Variegated ($5.50 → $9.00 /sqft), Jim's exact numbers. Modeled as two separate work types (one per option) rather than one work type with a material picker, because a material's price always inherits its work type's single shared `default_markup`, which can't hit two different cost:price ratios at once — each option's `default_markup` is instead solved backward from Jim's numbers (85.71% / 63.64%) so quick-add produces the exact target price automatically. Added the first sub for this, **Design-a-Deck (Jeff)**, seeded directly via migration since Jim gave the name in conversation rather than through the Subs admin page.

## 2026-08-13 — Sub contact info + work-type duration estimates

Prep work for two future capabilities (sub work orders delivered by text/WhatsApp, and a customer-facing job timeline) without building either yet. Subs and surface applicators now have a `phone` field, editable inline on the Subs admin page (was previously add-only — there was no way to edit an existing sub's name at all, so an Edit affordance was added alongside it). Work types now have an `estimated_days` field (Admin → Work Types), and `edit_quote.html` shows a summed "Est. Timeline" card — the total of each *distinct* work type's estimated days across a quote's accepted line items (not multiplied by quantity; two line items of the same work type only count once). Explicitly not built yet: the sub work-order delivery mechanism itself, and sub-availability/booking-conflict scheduling (needs a Contract start date, which doesn't exist) — both remain separate, larger, future features.

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

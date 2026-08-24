# QuoteCure Changelog

Plain-English running log of what's been built and why — kept so a fresh session (Claude or human) can get oriented fast without re-deriving everything from `git log`. **Add a new dated entry here after finishing anything notable** (a feature, a pricing-model change, a deploy). Newest entries at the top.

---

## 2026-08-24 — Cap Tile now has its own material list, separate from Waterline Tile

Cap Tile, Waterline Tile, and Coping have always shared one tile material catalog (`materials.work_type_id=4`), so picking a material for a Cap Tile line item showed the exact same dropdown as Waterline Tile — all 4 existing categories (6x6 Porcelain, Glass, Mosaic Waterline, Upgrade Waterline) turned out to be waterline-only products, cross-referenced against the actual LUV Tile May 2026 price sheet Jim supplied. There were **zero real Cap Tile products in the catalog at all** before this — the picker wasn't mislabeled, the products genuinely didn't exist yet.

Added 15 real Cap Tile trim products from the price sheet's "Specialty Trims" page (LUV Tile, supplier_id 6) under a new `Cap Tile Trim` category: the "2x6 Frost Proof Trims" group (6 items, $4.55–$14.50/piece) and the "6x6 Gloss & Non-Skid" A360 series (9 items, $3.65–$3.90/piece) — confirmed with Jim, excluding Depth Markers (not cap tile) and Porcelain Flush (out of scope for now). Cap Tile is quoted per linear foot like Waterline Tile, so each material's `cost_per_quote_unit` uses a 2-pieces-per-linear-foot conversion factor (confirmed with Jim: standard 6"-run trim pieces, 2 per lf).

`edit_quote.html`'s material picker (~line 413) now scopes by category as well as `work_type_id`: Cap Tile items (`work_type_id=5`) see only `Cap Tile Trim`; Waterline Tile and Coping see everything else in the shared catalog but that. Verified live via the real Add Item flow — Cap Tile's picker shows exactly `Cap Tile Trim(15)`, Waterline Tile's is unchanged at the original 4 categories.

The same-day-of-work-but-second bug: `select_materials.html` (the "Choose Your Tile" screen shown right after package selection) draws from the same shared catalog through `_eligible_tiles_for_package`, which bands tile options by the package's own `tile_price_threshold` — a waterline-tile-specific pricing tier concept. With no work_type_id awareness, the new Cap Tile Trim rows would've just gotten swept into whichever waterline price band their `cost_per_quote_unit` happened to fall in, on that screen specifically (the edit_quote.html fix above didn't touch this route at all — separate code path). Added a `cap_tile` flag to `_eligible_tiles_for_package`: when the tile item being picked for is Cap Tile Installation (`work_type_id=5`), it bypasses price-tier banding and the LUV/AIM manufacturer split entirely and returns only `Cap Tile Trim` materials; the waterline path also now explicitly excludes that category as defense in depth. Verified live: the Cap Tile tab on this screen shows all 15 trim products (`LUV Tile (15)`), the Waterline Tile tab is unaffected (`LUV Tile (46)`, Glass/Porcelain/etc., no trim products mixed in).

**Same-day correction**: 6 of the original 15 items (`6x6 Mud`, `6x6 Wall`, `6x6 Non-Skid`, `6x6 Non-Skid Int.`, `6x12 Non-Skid`, `4x4 Wall`) turned out to be wrong — a misread of the price sheet's jumbled two-column PDF layout paired the "2 X 6 FROST PROOF TRIMS" header with the wrong column; those 6 are actually under that page's "Depth Markers" header (pool depth signage, not cap tile trim), confirmed by none of them resolving to a real product on luvtile.com. Removed them and added the 13 real "2x6" trim products instead (`A42-2220` through `A42-9974`, $2.65–$2.90/piece) — each one verified live against `luvtile.com/search-results/a42<code>` before being added (e.g. `a422685` → "A42-2685 / NON SKID SAPPHIRE BLUE MIX"), with `product_url` populated from the confirmed link. The 9 A360 "6x6" items from the original seed were correct and are unchanged. `init_cap_tile_trim_materials` now deletes the 6 wrong item_codes on startup (same retroactive-fix pattern as `rename_marquis_manufacturer`) so production self-heals on next deploy without a manual DB fix. Final catalog: 22 Cap Tile Trim products (13 "2x6", 9 "6x6"), matching Jim's own description of what's actually used.

## 2026-08-21 — Fixed Paver Installation (and other deck-sqft items) pulling pool sqft when added manually

The automatic pricing path (package/template auto-populate) has always correctly used the deck/paver area for Paver Installation, Paver Sealing, Flagstone Pavers, and Textured Decking (`DECK_SQFT_WORK_TYPES` in app.py) — but the manual "Add Item" form's quantity auto-fill never knew about that distinction and always used the pool's own interior surface sqft for any sqft-unit work type. Exposed `DECK_SQFT_WORK_TYPES` to Jinja as a shared global (one list, not a duplicated one in the template) so the work-type dropdown can flag deck-sqft items with a `data-deck` attribute, added a `DECK_SQFT` JS constant, and updated all four quantity auto-fill sites in the Add Item form to use it. Verified live: Paver Installation now correctly auto-fills from `deck_sqft` (900) instead of the pool's `total_surface_sqft` (640); confirmed no regression on a normal sqft item (Surface Removal still uses the pool figure).

## 2026-08-21 — AI visualization now appears on the quote PDF

If a customer's before/after AI visualization was generated for a quote, it now shows up as its own page at the very end of both the customer preview and the emailed PDF — a "Before" / "After" pair with the package name, on its own page (`page-break-before`) after the signature and validity notice, not squeezed in anywhere else. Only a *completed* visualization shows (a pending or failed generation is silently excluded); if a customer tried more than once, the most recent completed one is used. Since `/preview` and the PDF export share the same renderer (`_quote_preview_html`), this needed one query and one template block, not a separate PDF-specific code path — images are already stored as base64 data URIs in `quote_visualizations`, so Playwright renders them natively, no PDF-merge step like the Terms Library needed for uploaded PDFs.

## 2026-08-21 — Job Scheduler

The time equivalent of the Job Ledger's quoted-vs-actual cost tracking: a PM assigns a sub and a scheduled start date to each work item on a signed contract (duration comes from `work_types.estimated_days`, wired up for the first time since it was added purely as a display total on 2026-08-13), gets a reminder to confirm a day before it starts, and actual start/finish dates get tracked against the estimate. Confirming is the same click that notifies the sub — no separate "send" step.

Two new pages: a per-contract schedule (`/quotes/<id>/schedule`, structurally a copy of the Job Ledger) and a cross-job view (`/schedule`, new top-level nav item) listing every scheduled item across all active contracts sorted by sub then date — a plain sorted list rather than a calendar grid, since two rows for the same sub with overlapping dates sitting next to each other already surfaces the double-booking problem without needing calendar UI (which doesn't exist anywhere else in this app). A reminders section at the top of that page surfaces everything currently needing a PM's confirmation.

The reminder itself is computed lazily on page load, not pushed — this app has no background/cron infrastructure at all (a single web process, confirmed via full-repo search), and adding a true "arrives Friday morning whether or not anyone opens the app" push would need a new Render Cron Job. That's a reasonable later upgrade, not needed to ship this.

Sub notification is email, sent when the sub has one on file (`subs.email`, new field) — but `add_sub_contact_phone`'s own docstring from 2026-08-13 already flagged that most subs don't really use email and the real intent was eventual text/WhatsApp delivery, which has no infrastructure to reach for yet. So this is honest about being best-effort: when a sub has no email, confirming shows the PM a plain "no email on file — let them know yourself" note instead of silently doing nothing.

**The bulk of the actual work was correctness, not new UI.** Adding `in_progress`/`complete` to `quotes.status` looked like a safe two-value addition but wasn't — "is this a signed contract" turned out to be checked via a literal `status == 'contract'` string comparison in roughly 20 places across `app.py` and three templates, not one shared concept. Left unfixed, a job moving past `'contract'` would have silently unlocked its own line items (a client-side `LOCKED` JS flag would have flipped to `false`, the most consequential of the bunch — the backend routes were still safe, but the UI would have looked editable with saves silently failing), lost Job Ledger access, been unable to receive new Change Orders, vanished from the Contracts tab, and reappeared in the regular Quotes tab. All of these now route through `_is_locked_contract`, updated to recognize the full signed lifecycle, or check the same three-value set directly. Status advances `contract → in_progress → complete` automatically as actual dates get entered, and self-heals from the underlying schedule data rather than being a one-way flag, so no extra guard was needed on the existing "Unlock Contract" button.

## 2026-08-17 — Added Tile Removal qualifier ($3/lf); qualifiers can now be per-unit

New "Tile Removal" qualifier on Cap Tile and Waterline Tile Installation at $3/linear foot — the first *per-unit* qualifier in the app; every prior one (Leak Detection, Freight) is a flat dollar amount regardless of the item's size. `qualifiers.per_unit` marks a qualifier as $/unit instead of flat; a new shared `_qualifier_cost()` helper computes each qualifier's dollar contribution fresh every time an item's totals are recalculated (not snapshotted once at toggle time), so if staff edit the item's linear footage after applying Tile Removal, the charge stays correct automatically instead of going stale. Verified end-to-end: toggling it on at 80 lf added $240; editing the quantity to 100 lf afterward updated it to $300 with no re-toggle needed.

## 2026-08-17 — Fixed tile material dropdown showing 185 unrelated materials

Reported as "can't change a tile in place, have to delete and re-add the line." The in-place edit mechanism itself (click the material name → dropdown → pick a different one → saves and recomputes cost/price) was already working correctly — verified by driving it directly. The real problem: for Cap Tile/Waterline Tile/Coping items, the dropdown showed *every* active material system-wide (185 of them — pavers, pool lights, skimmer parts, all mixed in with the tile options), because those three work types all draw from one shared tile catalog seeded under a single work_type_id, and the template's scoping condition bypassed filtering entirely for them instead of narrowing to that catalog specifically. Narrowed it to just the ~122 actual tile materials, and grouped them into `<optgroup>`s by category (6x6 Porcelain, Glass, Mosaic Waterline, Upgrade Waterline) so the list is actually browsable. A wall of 185 alphabetized, uncategorized options across every material type in the business is exactly the kind of thing that looks broken even when the underlying save logic isn't.

## 2026-08-17 — Per-sub markup override; corrected G&B pavers/tile rates

**Sub-specific markup.** Previously every sub doing a given work type shared the same markup% (`work_types.default_markup`) — only cost could differ between subs, never margin. Added `sub_rates.markup_pct` (nullable — NULL keeps today's shared-default behavior for every existing rate) so one sub can carry its own margin. Robert's Surface Prep now uses his real numbers: $400 cost at 100% markup → $800 price, replacing an earlier back-calculated "fake cost" of $615.38 that was reverse-engineered to hit $800 under the old shared 30% markup instead of reflecting what Robert actually charges. Blue Gator Pool Preps (same work type, different sub) is untouched — still $800 cost at the normal 30%. Wired into both the automatic pricing path (`_compute_line_item_pricing`) and the manual "Add Item" form, which now pre-fills the sub's own markup via `/api/rate` instead of always defaulting to the work type's shared markup.

**Correction**: G & B Flooring's rates got mixed up in the previous entry below — tile and pavers are both priced "per foot/sqft" so the numbers got swapped. Waterline Tile Installation is back to a per-linear-foot price, now $13.00/lf (not the $2.50 entered by mistake, which was actually meant for pavers). Both of G&B's paver rates (general Paver Installation and the separate Flagstone Pavers line) are now $2.50/sqft (was $2.10/sqft).

## 2026-08-17 — Sub rate updates: Robert's/Blue Gator's Surface Prep, G&B tile

Three rate updates at Jim's request:
- Robert's Surface Prep rate set so the **default price** (at the standard 30% markup) comes out to $800 — cost set to $615.38 (800 / 1.3), landing the actual default price at $799.99 due to unavoidable 2-decimal rounding at both the cost and price steps. Confirmed via the real pricing engine.
- Blue Gator Pool Preps added as a second Surface Prep sub at an $800 flat **cost** (not price) — same flat-rate 'each' pattern as Robert's row, just a different number since it's cost rather than target price.
- G & B Flooring's Waterline Tile Installation rate corrected to $2.50/lf (was $12.00/lf).

## 2026-08-17 — Fixed doubled "Marquis" label; editable email body before sending

Two unrelated fixes:

**Marquis surface finish showing as "Marquis (Premix Marbletite)Marquis – Bluestone".** Every surface product label anywhere in the app is built as `f"{manufacturer_name} {product_line} – {finish}"`. Marquis was seeded with `manufacturer_name='Marquis (Premix Marbletite)'` and `product_line='Marquis'`, so the word "Marquis" appeared twice, run together with no space. Renamed the manufacturer to "Premix Marbletite" (the real manufacturer, PMM) and the product line to "Marquis Series" (matching what's actually printed on the product), via a migration that updates the existing rows retroactively — `init_pebble_pros_surfaces` only inserts-if-missing on every startup, so editing just the seed code would have inserted a second manufacturer row instead of fixing the one already there.

**Email body is now editable before sending.** "Email Quote" used to send immediately with a hardcoded subject/body. It now opens an inline compose panel (mirroring the existing description-editor pattern) pre-filled with the same default subject/body as before — staff can edit either before hitting Send, or just send as-is with no extra clicks. Backend gained a `/quotes/<id>/email_defaults` endpoint and `email_quote` now accepts optional `subject`/`body` overrides, falling back to the same defaults it always used when nothing's provided.

## 2026-08-17 — Swapped the PDF header title font to bold sans

Follow-up to the header redesign below: the italic serif "QUOTE"/"CHANGE ORDER" title read as too frilly. Swapped to bold black DM Sans (weight 800) instead — big and chunky, matching the rest of the document's sans-serif system instead of introducing a second typeface.

## 2026-08-17 — Redesigned Quote/Change Order PDF header

Header on both the Quote and Change Order PDFs is now three columns instead of the old two-column logo-left/title-right layout: logo stays large on the left, the document title ("QUOTE" / "CHANGE ORDER") sits centered in an italic serif display font (DM Serif Display) instead of the old bold sans-serif, and company info (address, phone, email, license/CPC number) moved to the right alongside the date and valid-until date. Dropped the QT-#### number from both — Change Orders keep their own CO #N since that's operationally necessary to tell multiple COs on one contract apart, but the parent quote number added no value on the printed document itself.

## 2026-08-17 — Terms & Conditions Library

Replaced the single on/off "Include Terms" checkbox with a library of named terms documents. Started as a bug report (terms weren't showing on the PDF — turned out `company_settings.terms_text` had just never been filled in), then grew into a real feature request: Jim wants full formatting control (upload an actual PDF instead of fighting plain-text paragraph rules) and needs multiple different terms sets depending on the job (a general set, plus narrower ones like "Paver Only Terms" for jobs that are just pavers).

New `terms_documents` table (label, plain-text body, optional uploaded PDF, one flagged as default) managed from a new "Terms & Conditions Library" card in Admin → Company Settings — add a document with either a PDF or plain text (PDF wins if both are present), mark one as default, delete non-default ones (soft-deleted, so any quote still pointing at a removed one keeps working). The quote edit page's checkbox is now a dropdown selecting which document applies, defaulting new quotes to whichever is flagged default. A document with an uploaded PDF gets those pages appended directly onto the generated quote PDF via `pypdf`; one with only plain text renders inline exactly like the old checkbox did. Change Orders don't get a terms document merged in — they get a fixed sentence instead ("All terms and conditions of the original signed contract remain in full force and effect except as expressly modified by this Change Order"), since a CO is a short addendum to an already-terms-bearing contract, not a place to restate everything.

Migration seeds one "General Terms" document from whatever was in the old `terms_text` field and backfills existing quotes' selection from their old `include_terms` flag, so nothing changes for anyone who hadn't touched anything — `include_terms` and `terms_text` are left in place, unused, per this codebase's no-drop-column convention.

## 2026-08-17 — Fixed labor markup control not updating on split (Skimmer-style) rows

Reported: on Skimmer, moving the material markup arrows updated the material cost/price correctly, but moving the labor markup arrows changed the item's overall total behind the scenes without the labor %, margin, or price ever visibly updating.

Root cause: `adjMarkup()` in `edit_quote.html` looks up the on-page elements to update by id, but its id-guessing logic (`key = component === 'labor' ? '' : '-mat'`) only matched the *non-split* row layout, where there's a single unsuffixed markup control (`mv-{id}`) since labor is the only component. Split rows (labor and material shown as two separate lines — Skimmer is the case that surfaced this, but any work type with both a labor and material cost hits it) suffix every id, `mv-labor-{id}` and `mv-mat-{id}`. Material's `-mat` guess happened to match the real id; labor's empty-string guess didn't, so its elements were never found and silently never updated. The server-side price change was real (confirmed `min_markup` is still enforced backend-side regardless of what's displayed) — this was a display-only bug, not a pricing one. Fixed by trying the split id first and falling back to the non-split id.

## 2026-08-16 — Wiped all quotes for a fresh start

At Jim's request: cleared every quote/contract and everything tied to one (line items, change orders + their items, payment schedules, visualizations) so the app starts clean. Catalog data — work types, subs, materials, pricing, packages, commission policy — is untouched, only the transactional quote data is gone.

Implemented as a one-time migration (`wipe_all_quotes_2026_08_16`) rather than a manual DB command, so it runs automatically and identically on local dev and production via the normal deploy path, and is tracked in `schema_migrations` like every other change here. Before deleting, it copies each affected table into a same-named `_archive_20260816` table in the same database — genuinely irreversible for the live tables otherwise, so that's the undo path if anything turns out to be needed later. Also restarts the `quotes` id sequence at 1, so the next quote created is QT-0001 again.

## 2026-08-16 — Leak Detection qualifier auto-selects by spa; fixed two more Skimmer bugs

Follow-up to the Leak Detection qualifiers shipped just before this: Jim wanted the correct
variant (pool vs. pool & spa) chosen automatically from the quote's own `has_spa` dimension
instead of staff picking between two visible chips. `edit_quote()`'s `qualifiers_by_wt` now
filters out whichever Leak Detection qualifier doesn't match the quote's `has_spa` value, so
only one ever renders on Surface Application.

While verifying the Skimmer fixes from earlier today against real usage, found two more real bugs, both specific to *adding a Skimmer line item after the fact* (the auto-populated default-template/package path was already fine):

1. **Manually-added items came in labor-only.** The "Add optional item"/"Add line item" form (`addNrRow` in `edit_quote.html`) always POSTed `material_id: null, material_cost_per_unit: 0` regardless of which work type was selected — unlike the auto-populate paths, it never looked up the work type's own material default. Skimmer (and anything else with a `default_material_id`) silently lost its material every time staff added it manually instead of via a package/template. Fixed by having `onNrWorkType` fetch the selected work type's material default (extended the previously-unused `/api/work_type_defaults` endpoint to return it) and `addNrRow` send it.
2. **Changing a line item's sub did nothing but flash.** `editCell()`'s dropdown is opened via an `onclick` on the `<td>` itself; clicking the resulting `<select>` to open its native dropdown re-fires that same `onclick` (the click bubbles), which tore the select back down and rebuilt it mid-click — so picking a sub other than the default never registered, it just flashed and reverted. Fixed with a re-entrancy guard (`if (td === activeCell) return;`) so a click on the field already being edited no longer restarts it. This affected every editable dropdown field (sub, unit), not just Skimmer — Skimmer's default (Robert) just happened to be the case that surfaced it, since most other items are added via package defaults and rarely have their sub changed afterward.

Both verified live against the local dev server (not just `test_client`): opened a real quote in the browser, drove the actual `<select>`/form-add flow, and confirmed the DB rows end up correct.

## 2026-08-16 — Surface Removal min cost, Advanced Leak Detection sub, Leak Detection qualifiers

Three small pricing/catalog additions requested together:

1. **Surface Removal minimum cost of $2,600 for Pro Hydroblasters.** Added `sub_rates.min_total_cost` (nullable, sub+work-type specific — only set for Pro Hydroblasters/Surface Removal so far). Small jobs where `rate × qty` would compute below the floor now get bumped up to it, with price rescaled at the item's existing markup%; jobs already above the floor, and other subs doing the same work type, are unaffected. Had to add the same floor check to *two* independent pricing code paths — `_compute_line_item_pricing` (the automatic, dimension-driven path used by packages/quick-add) and `_price_catalog_item`/`build_line_item` (the manual "Add Item" path, which never queries `sub_rates` at all) — since they don't share a pricing implementation.
2. **Advanced Leak Detection** added as a new sub, wired as the default sub for the existing Leak Detection work type (id 19).
3. **Leak Detection qualifiers** added to Surface Application (pool: $400, pool & spa: $500) — plain qualifier rows, no new mechanism needed; qualifiers already get marked up by the item's own markup% automatically.

## 2026-08-16 — Fixed Skimmer material never selectable in the UI

Follow-up to the same-day Skimmer pricing fix: even with the Skimmer material now existing in the database, it could never appear in the material picker on `edit_quote.html`, because that route's materials query was hardcoded to `WHERE work_type_id IN (4,5,6)` (tile/coping only). Removed the restriction — the template already scopes materials correctly per line item (`mat.work_type_id == item.work_type_id`), so this just needed the Python query to stop pre-filtering. Same fix applied to the Change Order builder's materials query, though that page turned out to already work correctly via a separate, unrestricted `/api/materials_for_work_type` endpoint — the hardcoded query there was simply unused dead code.

## 2026-08-16 — Fixed Skimmer Installation pricing at $0

Reported: Skimmer's labor was missing when added via a package, and its material was missing when added as an individual line item. Root cause was two bugs stacked on each other:

1. `init_materials()` (seeds LUV Tile, NPT, SCP Pool Supply, Flagstone, and 100+ materials) guarded on "does *any* supplier already exist" rather than checking for one it actually seeds — on this environment, a later migration (`add_light_fixtures`) inserted its own suppliers first, so this guard went true prematurely and the entire seed list silently never ran. Fixed the guard to check for `'LUV Tile'` specifically, and made the supplier insert itself resilient to a partial prior seed (`'Flagstone'` already existed here from an earlier defensive migration — a blind `executemany` would have hit the `UNIQUE` constraint and thrown).
2. `init_skimmer_material()` — the function that seeds the actual Skimmer material and wires up `work_types.default_sub_id`/`default_material_id` — was imported in `app.py` but never called in `setup()`. Dead code; the Skimmer material has never existed in any environment. Also fixed its `default_sub_id` from `''` to `'S9'` (Robert) — it was written before Robert became the skimmer-install default elsewhere this session and never got updated to match.

Both are now wired up and idempotent. Verified: Skimmer Installation now shows real labor ($400, Robert) and material ($200, SCP Pool Supply) whether added via a package, the default quote template, or manually as an individual line item.

## 2026-08-16 — Larger logo on Quote/Change Order PDFs

Bumped the logo from max-height 90px/max-width 200px to 140px/260px on both the Quote and Change Order preview/PDF templates — the previous size (chosen when the logo was first added) still read a little small on the printed page. Verified via a real generated PDF; header layout has no fixed height so it just grows to fit, no overlap with the QUOTE/date block.

## 2026-08-16 — Job Ledger dashboard redesign

Replaced the ledger's original two-column cost-only summary with a 3-box dashboard, sketched as a mockup first and refined with Jim before building: **Quoted** (frozen at signing — cost, price, margin, tier, commission), **Actual** (the running numbers — cost so far, over/under, fixed price), **Payoff** (gross profit, margin, tier, commission, each with its delta from quoted inline). Tier is its own labeled row (a small badge) rather than a parenthetical next to margin, per Jim's note that if you want to know the tier, it should be a real data point. Added a progress bar in the header (X of Y items entered) for an at-a-glance read on how complete the picture is. The old separate Expected/Actual commission footer is gone — folded into the two boxes it belongs to.

## 2026-08-16 — Fixed stale commission on existing quotes/contracts

Found via the Job Ledger's Expected-vs-Actual comparison: `quotes.commission` (and `change_orders.commission`) only recalculate when a line item is edited. When the tiered gross-profit commission model was deployed, it changed the *active policy*, but never touched any quote or CO that wasn't edited afterward — those kept their stale pre-tiered commission (flat % of price) forever, and a signed Contract can never recalculate again once locked, so this was permanent. Confirmed with Jim's real numbers: a contract showing $1,730.25 (10% flat of $17,302.49 price — the old formula) should have shown $424.22 (10% of $4,242.24 gross profit at a 24.52% margin — the correct tiered result). One-time backfill migration recomputes commission for every existing quote and change order from its own already-frozen cost/price, under today's active policy — nothing else (cost, price, line items) changes. Likely affects other real signed contracts too, not just test quotes.

## 2026-08-16 — Job cost ledger (experimental)

New per-contract Job Ledger: once a quote is signed, every effective work item (original line items + anything added/removed by signed Change Orders, reusing `_contract_effective_items`) can get a real actual labor/material cost entered as work happens, alongside the frozen quoted cost — visible over/under at a glance. Reached via a "📒 Job Ledger" button next to Create Change Order on a signed contract.

Design choices, per Jim's steer: actuals mirror whatever split the item already has (Surface Application shows one cost field, Tile shows two); an item left blank falls back to its quoted cost in the running total rather than counting as $0, so the total is always a current best estimate, not all-or-nothing; every actual entry records who entered it and when, for the eventual invoice-upload idea. Sales gets full view access including commission (pro-transparency), but only Owner/Coordinator (new `roles.can_enter_actuals` permission) can actually enter numbers.

**Open design question surfaced while building this, not yet resolved**: "Expected Commission" is the sum of two independently-tiered calculations (the quote's own margin tier + each signed CO's own margin tier, exactly as originally paid). "Actual Commission" reruns the tiered formula ONCE against the combined running actual cost and combined price. Because the commission model has tier breakpoints by margin %, these two methodologies can disagree even at zero cost variance — a blended margin can cross a tier boundary that neither original document crossed on its own. Went with the blended approach (reflects true combined job economics) but flagging this is a real judgment call, not settled.

Schema: `actual_labor_cost`, `actual_material_cost`, `actual_entered_by`, `actual_entered_at` added directly to `quote_line_items` and `change_order_items` (not a separate ledger table) — both already share identical column shapes, so the existing effective-item resolver keeps working unchanged with no parallel table to sync.

## 2026-08-16 — Larger Visualize images with tap-to-enlarge

Before/after renderings on the Visualize page were capped at 340px tall — small, especially on an iPad. Bumped the display size substantially and added a tap-to-enlarge lightbox (full-screen overlay, tap backdrop/✕/Escape to close). Originally tried a plain `<a target="_blank">` to pop the image into its own tab, but every image in this app (visualizations, logo) is stored as a base64 data URI, and Chrome blocks `target="_blank"` navigation to `data:` URLs as an anti-phishing measure — silently does nothing, no error. A same-page lightbox sidesteps that entirely and works better on iPad besides (no tab-juggling).

## 2026-08-15 — Pool surface finish picker

Added a picker for the pool interior surface finish (Surface Application), living on the same post-package-selection screen as the existing waterline tile picker (`select_materials.html`), tiered by package the same way tile is. Deliberately **not** a browsing grid by default — Jim's read on real usage was "90% just confirming the assigned surface, 5% want something different, 5% don't know what a surface finish even is." So the default view is a reveal card ("Your Pool Surface: PebbleTec Pebble Sheen – French Grey") with a "Change It" button that opens the full manufacturer-tabbed, price-tiered browsing grid (PebbleTec/StoneScapes/Marquis/WetEdge), plus a collapsed "What's a surface finish?" explainer for the confused 5%. New `packages.surface_price_threshold` column bands eligible finishes by Pebble Pros rate, same mechanic as `tile_price_threshold`, editable in Admin → Packages.

Surfaced a real gap while scoping this: the catalog has 172 surface finishes seeded, but only 83 have an actual Pebble Pros rate — the rest (mostly Elite/Premium/Entry-Level tier-prefixed lines across all four manufacturers) have no price at all and aren't sellable. The picker correctly excludes unpriced finishes rather than showing something that would quote at $0, but getting real rates for the other 89 is a separate, not-yet-scheduled task.

Also researched and wired in real product photo URLs from each manufacturer's own site for 155 of the 172 catalog finishes (mirrors the existing `materials.product_url` "View Photo" outbound-link pattern already used for tile — no images are hosted or hotlinked in-app). Matched by (manufacturer, product_line, finish) name rather than `product_id`, since these rows were seeded via auto-increment and don't have a stable ID across environments. Notably, StoneScapes' own domain no longer resolves to the pool-finish company — that line is actually manufactured by National Pool Tile now, so those URLs point to nptpool.com instead. The other 17 finishes (a handful of StoneScapes colors no longer in NPT's current lineup, two Marquis Elite colors, one each of PebbleTec's Jade/Seafoam Green/White Crystal) found no confirmed match on the current sites and are left blank rather than guessed.

## 2026-08-14 — Company logo on quote/CO PDFs

Added the Bikini Pools of Florida logo as the default company logo on Quote and Change Order PDFs, seeded via migration from `static/bikini_pools_logo.webp` — stored in `company_settings.logo_path` as a base64 data URI (same format `upload_logo()` already produces for a manual upload via Admin → Settings), not a `/static/...` file reference, because Playwright's `page.set_content()` has no base URL to resolve a relative path against when rendering the PDF. Only sets it if `logo_path` is empty, so it never overwrites a real upload. Bumped the logo's max-height from 60px to 90px in both PDF templates — the original size assumed a wide banner-style logo; this one's nearly square (2000×1707) and was rendering too small to read the "of Florida · CPC1461488" subtext at 60px.

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

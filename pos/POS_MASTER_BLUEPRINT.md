# Aura POS: Master Blueprint

**Purpose:** this is the single reference for taking the POS to the next level. It covers what exists today (reviewed, audited and checked against the real data), what it must become (everything you confirmed), and how we get there without breaking anything that works.

**Status:** revision 2 (2026-10-12), for owner review. **No application code has been changed.**

**Revision 2 adds:**
- A5 real data facts;
- A3 findings R17–R25;
- A6 open defects carried over from the 9 Oct deep read, HANDOVER and GO-LIVE, each re-checked against today's code;
- B10 preservation checklist;
- B11 data migration and clean-up plan;
- C5 open questions;
- several corrections:
  - memberships limited to the selling branch (#36);
  - value-card example at 5%;
  - closing-edit loophole;
  - transfer-create crash.

It **supersedes** `POS_MODIFICATION_PLAN.md`. These stay as supporting documents:
- `POS_ARCHITECTURE_AND_BUSINESS_LOGIC.md`: detailed description of today's code with file and line references.
- `UNDERSTANDING_LOG.md`: every answer you gave (#1–#87, F1–F22, D1–D15).
- `../POS-DEEP-READ-2026-10-09.md`: the earlier deep audit.

**Evidence labels:**

| Label | Meaning |
|---|---|
| **[V]** | Verified by reading today's code |
| **[T]** | Verified by running it on the safe copy (SAP off, test database only) |
| **[D]** | Verified by a read-only look at the real database (2026-10-12) |
| **[I]** | Inferred |

---

## Contents

- **Part A: Current state review**
  - A1 Structure
  - A2 Module health scorecard
  - A3 Roles and permissions audit
  - A4 Hard-coded values
  - A5 Data facts
  - A6 Open defects carried from earlier audits
- **Part B: Target design**
  - B1 Principles
  - B2 Architecture
  - B3 Roles and permissions
  - B4 Settings catalogue
  - B5 Module by module
  - B6 Data model
  - B7 API
  - B8 Frontend
  - B9 Performance, security, reliability
  - B10 **Preservation checklist**
  - B11 **Data migration and clean-up**
- **Part C: Delivery**
  - C1 Phases
  - C2 Testing and acceptance
  - C3 Risks
  - C4 Decision register
  - C5 **Open questions for you**

---

# Part A: Current state review

## A1. Structure today

| Layer | Today |
|---|---|
| Frontend | Angular 22, standalone, zoneless, lazy routes. 19 pages under `/admin/*` plus login. One API service (`services/api.ts`). Installable web-app (PWA) service worker (`ngsw-config.json`). |
| Backend | Django 6.1 + DRF + Channels. 11 business apps (accounts, salon_admin, clients, appointments, services, inventory, billing, staff, finance, marketing, audit_logs) plus `pos_backend` (core, permissions, scoping, money, SAP). |
| API | **390 URL routes** (incl. format variants) **[T]**. WebSockets `ws/chat/<room>/` and `ws/dashboard/`. |
| Database | SQLite, about 60 tables. The `pos_backend` migration `0002_sap_posting` from 2026-10-11 is **not yet applied** to the real database **[D]**. |
| SAP | Service Layer and HANA. Background thread with retry queue and post-once ledger (2026-10-11 code, uncommitted). |
| Mobile | Staff-app and client-app APIs: 11 open (AllowAny) endpoints. **To be removed (#3).** |
| Repository | Git, branch `main`, about 450 uncommitted changes. About 160 ad-hoc scripts in `backend/`, 25 of them holding SAP credentials. Stray React files `frontend/src/pages/Dashboard.tsx` and `src/components/ReportDocument.tsx`. |

**Permission tree today** **[V]**:

```
overview      home
dashboard     analytics
logs          activity
admin         bills · changes · manager_discounts · centers · users · roles · services · clients
billing       invoices · new_invoice
appointments  calendar
staff         directory · profile · logs · reports · management · payrolls
inventory     products · vendors · purchase_orders · po_history · checkout · stock_history · audit
              stock_transfers · stock_requests · fulfill_stock_requests
marketing     promotions · cards · memberships · packages · whatsapp · usage
finance       register_summary · monthly_sales · detailed_revenues · refunds · procurement · multi_salon
              incentives · pettycash · manage
```

## A2. Module health scorecard

| Module | Works today | Main gaps (details in B5 / A6) |
|---|---|---|
| Login / users | Login, 7-day tokens, users with several branches | Hard-coded "owner"; users can be hard-deleted; slow SAP login at sign-in; token in browser storage, never revoked at logout |
| Roles & permissions | Strong engine, single tree, 37 tests pass **[T]** | Findings R1–R25 (A3) |
| Branches | CRUD, GST flag, targets, SAP warehouse | No branch code, state code, location type or opening hours |
| Clients | CRUD, blacklist, perks, bulk upload | Unique per branch only; 34,334 blank records **[D]**; prefix-only search; no consent flags |
| Services | Central + branch price, consumable block | BOM pull manual; a branch user editing without a branch changes the central price |
| Appointments | Timeline, clash guard on create | Fixed hours; restore/update skip checks; service lines are free text |
| Billing | Exact pricing, idempotent checkout, wallets, perks | Status settable by the browser; payment edits can lose wallet money; double coupon discount; no approvals; product GST ₹0; value-card maths; tips/Nearbuy/refunds to remove; drafts undeletable; invoice number too long; receipt not a valid tax invoice |
| Marketing | Promotions, cards, memberships, packages, usage | Rules only on screen; single-service packages; WhatsApp mock; no consent |
| Inventory | POs, goods receipt, transfers, checkout, audit | 155,927 per-branch product copies **[D]**; MRP missing on nearly all; whole-number stock (consumables in ml/g truncated); no warehouse, batches, in-transit or locks; negative stock; transfer create crashes for non-owners |
| Staff / payroll | Directory, logs, transfers, tools, payroll lock | Keyword role groups; owner-only payroll; flat incentive split evenly (not by contribution); Aadhaar stored in plain text; salary edits not audited |
| Finance | Reports, petty cash, closing, shifts, incentives | Closing typed in, never computed; past closings editable without trace; shifts not linked to bills; tax report not GSTR-1 ready |
| SAP | Post-once ledger and retry queue (new) | 46 hard-coded GL accounts; 12% → 5% downgrade; auto-patching of SAP item masters; HSN fixed at 1 on one document; credit memo not linked to its invoice; TLS verification off |
| Notifications / approvals / settings | **Do not exist** | All new |

## A3. Roles and permissions audit

### How it works today (sound) **[V]**

- A role stores `{all_centers, module: {submodule: {read, create, update, delete}}}`.
- `HasModulePermission`:
  - maps HTTP methods to verbs;
  - allows per-action verb and submodule overrides;
  - grants module-level **read** so the tabs of a module share lists;
  - keeps **writes** strictly per submodule;
  - lets shared reference data (clients, services, branches) be read and created from other modules.
- One tree file drives the Roles screen, sidebar, route guard and server, and a test keeps the two copies identical.
- **No roles exist in the data yet, and both users are superusers [D].** The new model can therefore be introduced without breaking anyone's daily access.

### Behaviour probe on the safe copy **[T]**

| Endpoint | No permissions | Cashier (billing.new_invoice) | Closer (finance.pettycash) | Requester (stock_requests) | Payroll mgr (staff.payrolls all) |
|---|---|---|---|---|---|
| GET dashboard summary | **200** | 200 | 200 | 200 | 200 |
| GET staff revenue / consumption report | **200** | 200 | 200 | 200 | 200 |
| GET low stock / SAP branches / chat users | **200** | 200 | 200 | 200 | 200 |
| GET stock requests | 403 | 403 | 403 | **crash** | 403 |
| POST open shift | 403 | 403 | **403** | 403 | 403 |
| POST payroll lock | 403 | 403 | 403 | 403 | **403** |
| GET invoices | 403 | 200 | 403 | 403 | 403 |
| GET promotions | 403 | 200 (shared read) | 403 | 403 | 403 |

### Findings

| # | Finding | Evidence | Fix (phase 1 unless noted) |
|---|---|---|---|
| R1 | **"Owner" is hard-coded by role name** in `permissions.py`, 27 backend views and about 30 frontend files. Any role called "owner" gets everything. | [V] | Only `is_superuser` bypasses. Former owner-only actions become action permissions (B3). |
| R2 | **No module check on** 7 dashboard endpoints, 3 staff reports, low stock, SAP branches, centre import template, and chat: open to any logged-in user | [T] | Map each to its submodule (B3) |
| R3 | Billing submodules not separated on the server; cancel = "delete" on any billing submodule | [V] | Per-action mapping (B3) |
| R4 | Admin → Bills permission is decorative (its actions are checked against billing permissions) | [V] | Shared action permissions |
| R5 | Shifts are governed by `finance.manage` (incentive rules); the closer gets 403 | [T] | New `finance.shifts` |
| R6 | Stock Requests list crashes for the people allowed to use it (filters a non-existent `center` field) | [T] | Filter on `requesting_center` |
| R7 | Fulfil Stock Requests is checked in code, and read **or** create **or** update all count | [V] | Declared action permission |
| R8 | Marketing page checks the stale key `marketing.campaigns`, so non-owners can't see or create campaigns | [V] | Use the split keys; server sends normalised permissions |
| R9 | Ticks enforced nowhere: `overview.home`, `dashboard.analytics`, `admin.bills`, `billing.invoices`, `billing.new_invoice`, `staff.profile`, `staff.reports`, `inventory.po_history`, `marketing.usage` | [T] | Real enforcement |
| R10 | Tax, Service drilldown, Staff performance and Multi-salon export all sit under `finance.monthly_sales` | [V] | Own submodules |
| R11 | Payroll generate/lock/unlock/paid/delete, closing overwrite, branch bulk import are owner-only | [T] | Action permissions |
| R12 | Login returns raw stored permissions; the frontend normalises inconsistently | [V] | Server returns normalised permissions |
| R13 | Chat has no permission | [V] | New `chat` module |
| R14 | No permissions yet for the new modules | — | B3 |
| R15 | Write-protected records allow writes only to "owner" | [V] | Superuser only |
| R16 | Route guard, sidebar and Roles screen mirror the tree correctly | [T] | Keep; extend tests |
| **R17** | **Creating a stock transfer crashes for non-owners.** The shared create code saves a `center` field that transfers don't have. | [V] `inventory/views.py:75-109` + `serializers.py:319-323` | Transfer-specific create (phase 3) |
| **R18** | **`ws/dashboard/` has no login check and one global group.** Every invoice payment is broadcast to every connected socket, across all branches. | [V] `pos_backend/consumers.py:4-18`, `billing/views.py:229-243` | Authenticate the socket; per-branch groups; requires `dashboard.analytics` (phase 1) |
| **R19** | **Invoice `status` is writable by the browser** (in `fields`, not in `read_only_fields`). PATCH `{status:'cancelled'}` skips every reversal; PATCH `{status:'draft'}` hides a paid bill. | [V] `billing/serializers.py:128-135` | Status changes only through server actions (finalise, cancel); phase 3, **critical** |
| **R20** | Mass assignment: serializers with `fields='__all__'` accept `sap_sync_status`, `sap_sync_message`, `monthly_targets_history`, shift status and expected cash, and similar fields | [V] | Read-only audit of every serializer (phase 3) |
| **R21** | **Past daily closings can be edited** by anyone with petty-cash update, through PUT, with no amendment record (only create blocks overwrites) | [V] `finance/views.py:451-465` | Edits need `pettycash.amend_closing` and always write the old values to the audit log (phase 1) |
| **R22** | A branch user editing a service **without a branch selected** changes the **central** price and tax | [V] `services/views.py:79-103` | Central edit needs `admin.services.update` at organisation level; branch edits go through `override_price` |
| **R23** | Staff-tool list scoping doesn't fail closed for users with no branch | [V] `staff/views.py:1214-1240` | Use the shared scoping helper |
| **R24** | Django admin is exposed at `/admin/` | [V] `pos_backend/urls.py` | Superuser only, behind the proxy allowlist, or disabled in production (C5) |
| **R25** | About 15 views hand-roll branch scoping instead of the shared fail-closed helper | [V] | Use `pos_backend/scoping.py` everywhere |

## A4. Hard-coded values inventory → must become dynamic

| Hard-coded today | Where | Becomes |
|---|---|---|
| "owner" role name | `permissions.py`, 27 views, about 30 frontend files | Action permissions (B3) |
| **46 SAP GL accounts**; B2C code `B2C`; card-code fallbacks `WALKIN`, `C{id:06d}`, phone starting `CUST`; warehouse `ZYWH001`; `ItemsGroupCode 121`; debtor account; location fallback "2"; tax-code names; UDF names; GRPO batch name `POS-YYYYMMDD` | `pos_backend/sap_utils.py` | **Settings → SAP mapping** |
| Payment methods (incl. Nearbuy) | `billing/payment_methods.py` `METHOD_SPEC`, till lists | **Settings → Payment methods** per branch |
| Incentive role groups by **words in the designation name** (`manager/owner/admin/supervisor`, `lhds/uhds/stylist/hair…`, mhds, pedi) | `finance/incentives.py:70-71,127-160`, `finance.ts` `'lhds_uhds'` | Rules target designations from a list |
| Calendar 9:00–22:00; durations 45 min (screen) / 30 min (server) | `appointments.ts:235-265`, `appointments/views.py` | **Settings → Appointments** per branch |
| Line-discount audit 50%; wallet scope; package GST 0; package-redemption commission | `settings.py` (environment) | **Settings** |
| Invoice number `C{id}/{FY}/{n}` | `billing/models.py` | **Settings → Invoice series** |
| Sender e-mail **"Chilli Potato <noreply@chillipotato.com>"** | `settings.py:437` | **Integrations → Email** |
| Value-card GST fallback 5% | `billing.ts:744` | Removed (stored per card) |
| 12% → 5% tax downgrade for SAP | `sap_utils.py:975,1068,1130,1347,1548,1655` | Removed; codes from SAP mapping |
| Financial-year start (April) | Number series, reports | Setting (default April) |
| Approval thresholds, draft expiry, cancel window, payment-change window, receipt size | Don't exist | New settings |
| Throttles, token lifetime, page sizes, SAP TLS verification | `settings.py` | Technical configuration (environment); **SAP TLS verify → on** in production |

## A5. Data facts (real database, read-only, 2026-10-12) **[D]**

| Fact | Value | Consequence for the plan |
|---|---|---|
| Branches | **81** | Branch codes and state codes must be filled for all 81 (B11) |
| Users / roles | **2 users, both superusers; 0 roles** | RBAC change cannot lock anyone out; roles will be created fresh |
| Clients | **34,371**, of which **34,334 have a blank phone**, 34,349 no branch, only **22** with phone and branch; **5** duplicate-phone groups; none of the blank ones is on any bill | A clean-up decision is needed before the global unique phone (C5 Q1) |
| Products | **155,927 rows for 1,927 distinct codes** (one copy per branch); **MRP empty on 155,926** (the till sells at `price`) | Move to one catalogue with per-branch rows (B5.8). Decide which SAP price list is the MRP (C5 Q2). |
| Vendors | **45,722** rows (per-branch copies) | Same consolidation |
| Services | 206, all at **5% GST** | Value-card example uses 5%. The CA confirms the rates (C5). |
| Invoices | **37**, all paid; payments UPI 30, Value Card 14; no tips, refunds, drafts, advances or cashback | Low-risk moment to change billing rules and numbering |
| Value cards | 19 client cards; template pre-tax ₹20,000 / post-tax ₹21,000 / face ₹30,000 / GST 5% | Exactly your F5 example. Backfill ratio 2⁄3 and 5% (B11). |
| Memberships, packages, promotions, incentive rules, payrolls, closings, shifts, appointments | **0** | New rules start clean |
| Stock transfers / POs / lots | 2 / 1 delivered / 1 | Small; easy migration to batch stock |
| SAP retry queue | **6** legacy rows (old "unknown" journal entries); `SapPosting` table not created yet | Phase 0: apply migration; review the 6 rows with the SAP team |
| Staff | 25, with an `aadhar_number` column | Aadhaar must be masked or removed (C5 Q9) |

## A6. Open defects carried from earlier audits (re-checked against today's code)

These come from the 9 Oct deep read, HANDOVER and GO-LIVE. Items already fixed on 2026-10-11 (GRPO re-posting, unretried JEs, scheduler in every process, credentials in code) are left out.

| # | Defect | Evidence | Planned in |
|---|---|---|---|
| X1 | Invoice status writable by the browser | R19 | Phase 3 |
| X2 | **Editing a bill's payments deletes payment rows without reversing wallet or card debits**; a wallet payment added on edit is never debited | [V] `billing/serializers.py:593-650` | Phase 3: payments change only through server actions |
| X3 | Double coupon discount | [V] | Phase 3 (interim), phase 6 (engine) |
| X4 | Day-end cash not computed; `system_cash` and `difference` stored as 0; shift expected cash from the browser | [V] | Phase 9 |
| X5 | **Printed bill isn't a valid tax invoice:** shows the database id, no HSN/SAC, no customer GSTIN, no place of supply, no "Tax Invoice" title; lines rounded to rupees | [V] `billing.html` receipt | Phase 6 receipt |
| X6 | Products billed with ₹0 GST | [V] | Phase 3 |
| X7 | SAP: **12% → 5% downgrade**; **auto-patching SAP item masters** on GST/HSN errors; `HSNEntry: 1` on one document; credit memo has no `BaseEntry` link to the invoice; inconsistent card codes | [V] `sap_utils.py:604,975,1045,1130,1516` | Phase 2 / 11 |
| X8 | **Whole-number stock:** consumables in ml/g truncated (`int(bom.quantity * qty)` deducts 0 for fractions) | [V] `inventory/models.py:50`, `billing/services.py:285` | Phase 7 (decimal quantities + unit of measure) |
| X9 | Stock moves without row locks (checkout, audit, GRPO, transfers) | [V] | Phases 3 / 7 |
| X10 | Membership discount enforced after payments are written; status not recomputed | [V] `billing/services.py:651-678` | Phase 6 |
| X11 | Float money paths: card scaling, `_receipt_scaling_ratio` swallowing errors, SAP payloads | [V] | Phase 6 (Decimal everywhere) |
| X12 | `validate_sap_stock` is dead code (comparison never matches; errors commented out) | [V] `billing/serializers.py:368-378` | Phase 3: remove |
| X13 | Flat item incentive is split **evenly** among staff, not by the contribution % that drives incentives (#49) | [V] `finance/incentives.py:740-760` | Phase 9 |
| X14 | **Manual commission rows** (ServiceLog) can be created by hand with no bill behind them, which inflates commission | [V] | Phase 9: own action permission + audit |
| X15 | Salary edited inline from the incentive report with no audit | [V] `staff.ts:911-925` | Phase 9 |
| X16 | **Aadhaar stored in plain text** (India DPDP Act) | [D] column exists | Phase 9 (C5 Q9) |
| X17 | Audit log drops entries when its queue is full; no response status; payload not before/after | [V] `audit_logs/middleware.py:41` | Phase 2 (job queue), phase 12 |
| X18 | **Service worker caches `/api/` responses for a day**, so customer data stays on a shared till after logout | [V] `ngsw-config.json:30-39` | Phase 3 |
| X19 | **Font Awesome is used (135 icon tags) but never loaded**, so those icons render blank | [V] | Phase 3 (load it; no design change) |
| X20 | CGST/SGST shown as **editable inputs** on the bill (the server ignores them) | [V] `billing.html:1088,1100` | Phase 3: display-only |
| X21 | Finalise button labelled **[F2]** but the key is F8 / Ctrl+Enter | [V] `billing.html:1116` | Phase 3: fix the label |
| X22 | `xlsx@0.18.5` (unmaintained, known CVEs) | [V] `package.json` | Phase 12: maintained library |
| X23 | Token kept in browser storage, not revoked at logout; each login reissues it and logs out the user's other devices | [V] | Phase 12 (C5 Q8) |
| X24 | `OptionalPagination` returns whole tables; the 30-s low-stock poll downloads up to 155k rows | [V] | Phase 12 / 7 |
| X25 | 28 date filters wrap the column in `DATE()` (index lost) | GO-LIVE.md | Phase 12, per site with measurement |
| X26 | About 28–31 capacity/MySQL findings (missing indexes and similar) from the earlier audit | HANDOVER.md | Phase 12 |
| X27 | `pull_from_sap` creates a product row per branch, never updates stock or price afterwards, and deletes vendors without POs from unassigned branches | deep read §3.4 | Phase 7 (scheduled sync replaces it) |
| X28 | Stock audit forces SAP to match the POS count | deep read §3.4 | Phase 7 (C5 Q7) |
| X29 | `repair_invoice_math`: historical service lines stored at ₹0 under-paid commission; needs a dry run and approval | HANDOVER.md | Phase 0 (C5 Q12) |
| X30 | Old MySQL password in a settings comment; Playwright tests sign in with a hard-coded account | [V] | Phase 0 |
| X31 | SQLite-only hack in checkout ("WAL deadlock prevention" dummy update) | [V] `billing/views.py:669-696` | Phase 0 (removed on MySQL) |
| X32 | Blocking `alert()`/`confirm()` everywhere | deep read §3.11 | Phase 12: same look, non-blocking toasts and confirm dialogs |
| X33 | Forced boot splash of a few seconds on every page load | `index.html` | Phase 12 (C5 Q13) |

**Found during Phase 0 (2026-10-12):**

| # | Defect | Status |
|---|---|---|
| X34 | Staff app password column too short for a hashed PIN: MySQL refused it (SQLite ignores lengths) | **Fixed in Phase 0** (column widened, migration `staff.0025`) |
| X35 | Coupon-usage unique rule was conditional, which MySQL silently drops | **Fixed in Phase 0** (plain unique rule with the same guarantee, `marketing.0017`) |
| X36 | A price raised **above** the catalog was accepted but never written to the change log (#25 needs every price change traceable) | **Fixed in Phase 0** (`billing/pricing.py`) |
| X37 | Register summary and monthly sales cache results for 60 s, so a report can lag a new bill by up to a minute (#82) | Phase 9: summary tables replace this cache |
| X38 | 6 old SAP queue rows of type "unknown" (ids 8, 9, 13, 15, 16, 37; 8–9 Oct). Today's worker would silently drop them. | Phase 2: map each to its real document; post-once checks SAP first; SAP team reviews |
| X39 | The deployment check for database connection health read the wrong setting (false alarm) | **Fixed in Phase 0** |
| X41 | Login page requests `assets/images/log.png`, which doesn't exist (404, broken image) | Phase 3 (restore the asset; no design change) |
| X42 | Local only: Windows Smart App Control blocks the Angular parser's native module; the WebAssembly fallback needed its loader nested beside it. A fresh `npm install` undoes this local fix. | Phase 12: pin the fallback in `package.json` |
| X40 | Refunds-report Excel export uses a variable that is never defined (`cancelled`, `finance/views.py:1609`), so exporting crashes **[V]** | Phase 3 (the refunds report becomes Cancellations) |

---

# Part B: Target design

## B1. Principles

1. **Nothing business-related is hard-coded.** Everything is managed in Admin screens (B4).
2. **Fully role-based.** The superuser is the only built-in identity. Every screen, tab, button, API action and WebSocket maps to a permission (B3).
3. **The server is the authority** for prices, taxes, discounts, approvals, stock, status changes and permissions. The browser only displays and requests.
4. **The UI is preserved.** No redesign; only smoother and faster. New screens copy the existing look.
5. **No silent rule changes.** Everything traces to C4; anything new needs your approval (C5).
6. **Nothing that works today is lost.** B10 is the regression checklist for every phase.
7. **Compatibility.** Data and relationships kept; the SAP JE design from the SAP team kept; API changes listed (B7).

## B2. Target architecture

```
Browsers (~400 users) ─ HTTPS ─► nginx ─► Django API (gunicorn + uvicorn, ASGI)
                                             │  REST + authenticated WebSocket (approvals, notifications, live dashboard per branch)
                                             ▼
                                         MySQL 8 (utf8mb4)  ◄── business write + OUTBOX rows in ONE transaction
                                             ▲
                     worker processes ───────┤  claim outbox rows (FOR UPDATE SKIP LOCKED), in dependency order
                       • SAP worker(s)  ─────► SAP Service Layer / HANA   (session reuse, concurrency cap, never blind re-post)
                       • notify worker  ─────► Email (Microsoft 365 / SMTP), WhatsApp Cloud API
                       • scheduler      ─────► draft expiry at branch EOD, card/cashback expiry, reminders, transfer sync,
                                               SAP master sync (BOM, items, prices), nightly POS↔SAP reconciliation, summaries
                     Redis (production, optional) ── cache + websocket fan-out across servers
```

- **MySQL 8** replaces SQLite: real locks, concurrency, backups. Nothing has ever run on MySQL, so phase 0 proves it.
- **Database-backed job queue (outbox)** instead of threads. Nothing is lost, nothing is posted twice (post-once keys kept), and SAP never slows a till.
- **Summary tables** (daily by branch, item, staff), refreshed by the worker, feed dashboards and multi-salon reports instead of scanning every bill line.

## B3. Target roles and permissions

**Shape** (backward compatible):

```
{ "all_centers": bool,
  "<module>": { "<submodule>": { "read","create","update","delete": bool, "actions": { "<action>": bool } } } }
```

- The Roles screen shows the four CRUD ticks as now, plus **action ticks**.
- Old saved roles keep working through aliases.
- **Approvers** are chosen per policy (B5.12), not as a permission.
- **Discount limits** are per role, in Settings.

| Module | Submodule | CRUD meaning | Actions | Was |
|---|---|---|---|---|
| overview | home | read | | |
| dashboard | analytics | read (all dashboard endpoints and the live socket) | export | open (R2, R18) |
| logs | activity | read | export | |
| chat **(new)** | messages | read / create | | none (R13) |
| admin | bills | read | | decorative (R4) |
| | changes | read | | |
| | manager_discounts | read | | |
| | centers | CRUD (branches **and warehouses**) | bulk_import, view_sap_branches | owner-only import |
| | users | CRUD (delete = deactivate) | reset_password, assign_role | |
| | roles | CRUD | | |
| | services | CRUD (central catalogue) | override_price (branch), bulk_upload | central edit leak (R22) |
| | clients | CRUD | blacklist, bulk_upload, merge, carry_over (parked) | |
| | settings **(new)** | read / update | | |
| | approvals **(new)** | CRUD policies | | |
| | notifications **(new)** | CRUD rules and templates | send_test | |
| | integrations **(new)** | read / update | test_connection | |
| | sap_sync **(new)** | read | retry, resolve | |
| billing | new_invoice | create (bill, draft); update/delete own drafts | change_price, give_discount (approval thresholds apply), use_wallets | not separated (R3) |
| | invoices | read (history, reprint) | **cancel**, **change_payment** | (R3) |
| | drafts **(new)** | read / delete any branch draft | | |
| appointments | calendar | CRUD | assign_staff, cancel, restore, check_in, no_show | |
| staff | directory | CRUD | bulk_upload | |
| | profile | read/update sensitive fields (salary, commission, ID numbers) | | unenforced |
| | logs | CRUD service logs and perk usage | **manual_commission** (create a log with no bill) | X14 |
| | reports | read | export | open (R2) |
| | management | CRUD designations, tools, transfers | | |
| | payrolls | CRUD (Draft only) | **generate, lock, unlock, mark_paid, adjust** | owner-only |
| | perks **(new)** | read / update points | | |
| inventory | products | CRUD (catalogue and branch rows) | bulk_upload, edit_branch_price | |
| | vendors | CRUD | bulk_upload | |
| | purchase_orders | CRUD | advance_status | |
| | goods_receipt **(new)** | create | | under POs |
| | po_history | read | | |
| | checkout | create | | |
| | stock_history | read | export | |
| | audit | create | | |
| | stock_requests | read / create | | crash (R6) |
| | stock_transfers | read / create | dispatch, receive, cancel | crash (R17) |
| | batches **(new)** | read | | |
| marketing | promotions · cards · memberships · packages | CRUD | toggle_status | stale key (R8) |
| | whatsapp | read | send_campaign, manage_reminders | |
| | usage | read | export | |
| finance | register_summary · monthly_sales · detailed_revenues · procurement · multi_salon | read | export | |
| | tax_report · service_drilldown · staff_performance **(split)** | read | export | (R10) |
| | cancellations (was refunds) | read | export | |
| | incentives | read | export | |
| | manage | CRUD incentive rules | duplicate | |
| | shifts **(new)** | open / close / X / Z | | (R5) |
| | pettycash | CRUD entries, daily closing | **amend_closing**, manage_categories, top_up | R21 |
| | reconciliation **(new)** | read | | |

## B4. Settings catalogue (everything dynamic)

**Admin → Settings:**
- Each setting is scoped **Global** or **per Branch**, typed and validated.
- Every change is audited (who, when, old → new).
- Secrets are encrypted and never displayed again.
- **Defaults equal today's behaviour or your answers.**

| Group | Setting | Default |
|---|---|---|
| **Business** | Company name, logo, address; per branch: GSTIN, **branch code (≤ 4)**, **state code** (from GSTIN), **location type** (Branch/Warehouse), address for the receipt; financial-year start | April |
| **Billing** | Rounding | nearest ₹1 with a "Round off" line |
| | Draft expiry | branch end of day |
| | Cancel window | same day |
| | Payment-method change window | 6 h |
| | Cash: show change | on |
| | Staff split must total 100% | on |
| | GST on/off per branch (off → "Bill of Supply") | on |
| | Receipt size | Auto / 80 mm / 58 mm / A4 |
| | Receipt header/footer | — |
| **Invoice series** | Pattern | `{BRANCH}-{FY}-{SEQ5}`, e.g. `DL01-2627-00045` (≤ 16 characters, checked) |
| | Series per branch and financial year | yes |
| | Credit-note series for cancellations, if the CA requires one | C5 Q5 |
| **Payment methods** | Name, type (cash / online / wallet), active per branch, SAP account per method | today's list **minus Nearbuy** |
| **Discounts & approvals** | Per role per branch: max line %, max bill %, and price change, each **without** approval | none (always ask) unless set |
| | Link expiry | 15 min |
| | Approve-by-link | on |
| **Wallets & perks** | Advance and cashback usable at all branches | on (#36) |
| | **Value cards, packages and memberships** usable only at the selling branch | on (#32, #36) |
| | Expiry reminder lead time | 30 days |
| | Default value-card validity | 365 days |
| | Package redemption earns commission | off (#71) |
| **Appointments** | Hours per branch per weekday | 09:00–22:00 |
| | Slot size | 15 min |
| | Default duration | 45 min |
| | Checked-in / No-show | on |
| **Inventory** | FEFO auto-batch | on |
| | Units of measure list | pcs, ml, g, … |
| | Low-stock alerts | via Notifications |
| | Over-receipt | off (fixed rule) |
| | Negative stock | never (fixed rule) |
| **Incentives** | Designations eligible per rule | migrated from today's keyword groups |
| **Notifications** | Per event: channels, recipients, templates; quiet hours | B5.13 |
| **SAP mapping** | GL accounts per purpose and per payment method | **today's 46 accounts as defaults** |
| | B2C code | `B2C` |
| | Default warehouse | `ZYWH001` |
| | Items group | 121 |
| | Debtor account | — |
| | Tax codes per rate (CGST/SGST/IGST) | — |
| | UDF names | — |
| | Price list used as MRP | C5 Q2 |
| | Per branch: warehouse and business place | — |
| **Integrations** | SAP Service Layer (URL, company, user, password, TLS verify) | — |
| | HANA | — |
| | Email (Microsoft 365 Graph or SMTP, sender) | — |
| | **WhatsApp Cloud API: empty until you fill it** (D5) | empty |
| | Paytm | later |
| **Privacy & logs** | IP geolocation in Logs | off (C5 Q14) |
| | Mask ID numbers | on |
| | Marketing consent required before WhatsApp/SMS | on |

## B5. Module by module: today, target, change

Each block lists **Today**, **Target** (with the decision numbers) and **Change**.

### B5.1 Access, users, roles

- **Today:** owner hard-coding; findings R1–R25.
- **Target:** fully dynamic (#2, #6). Superuser only. Deactivate, never delete (#7).
- **Change:**
  - B3 tree with action permissions.
  - Fix R1–R25.
  - Login returns normalised permissions and no longer does the SAP login (D7).
  - Logout revokes the token on the server.
  - User delete → deactivate.

### B5.2 Branches and warehouses

- **Target:** unlimited branches with their own GSTIN (#1); warehouses (F17, D2); hours (#20); monthly target and history (#9); credit limit and closing SMS kept as they are (#8).
- **Change:**
  - Branch code, state code, location type, receipt address.
  - Fill them for all **81** branches (B11).

### B5.3 Clients

- **Target:**
  - **Global** clients, searchable by name or number anywhere (#11).
  - **Phone unique system-wide**, with "already used by \<name\>" (F2).
  - Name and phone required (F3).
  - B2C/B2B SAP mapping kept (F4).
  - Blacklist by permission (#12).
  - No mobile app (#3).
  - Carry-over parked (#13).
- **Change:**
  - **The 34,334 phone-less records are kept: they are B2B clients (Q1).** The phone stays mandatory and unique for **new** clients. These legacy records are allowed a blank phone until one is added, and they are found by name or GSTIN.
  - **GSTIN field on the client, editable from Billing**, for B2B bills. It is validated (format, state code, PAN part) and drives B2B vs B2C in SAP and on the invoice.
  - 5 duplicate-phone groups resolved with the merge tool.
  - Normalised phone with a unique constraint that applies when a phone is present.
  - **Search by any part of the name or phone** (today it's prefix-only), paged.
  - Bulk upload through the same validation, with a per-row result.
  - Merge tool (moves history, perks and wallets).
  - **Consent flags** for WhatsApp/SMS (kept with DND).
  - Remove client-app endpoints and PIN.
  - Welcome e-mail on creation becomes a Notification event (on/off) using the configured mailbox.

### B5.4 Services

- **Target:** central price, branch override and active flag by permission (#16); GST per service (#17); BOM from SAP (#19); consumable shortage **blocks** with missing items listed (#18).
- **Change:**
  - Fix R22.
  - Batch- and unit-aware consumable check.
  - Scheduled BOM sync with "last synced".
  - Branch incentive override kept.

### B5.5 Appointments

- **Target:** branch hours (#20); unallocated allowed and staff never double-booked (#21); restore re-checks (#22); a cancelled service bill cancels its appointment (#23); Checked-in and No-show (D14).
- **Change:**
  - One validator for create, update, assign and restore, including the blacklist and branch checks on update.
  - **Service lines linked to the service catalogue** (today free text), with an end time.
  - Billing loads appointment lines by link, not by name.

### B5.6 Billing

| Rule | Change |
|---|---|
| **Server-only status** (R19, X1) | Status moves only through server actions: draft → paid (finalise), paid → cancelled (cancel). The client can't write status, money fields or payments directly (X2). |
| **One pricing engine** | `quote` endpoint; the screen shows the server result; the local estimate shows instantly |
| Price change and discount **need approval above the role's free limit**; the approved price is final and goes to SAP (#25, #26, F11) | B5.12, enforced at save. Until phase 5 ships, discounts work as today (C1). |
| Coupons, memberships, all promotions: **services only**, on the pre-tax value, one at a time (#29, #30, D1) | Server promotion engine; the till sends only the promotion or membership id (fixes #28). Only promotions valid at the branch are listed. |
| Several memberships, the client chooses (#31); **membership used only at the selling branch** (#36) | Picker of active memberships at this branch. The cap is applied **before** payments are recorded (X10). |
| **Value card** (#33, F5, F6, F7, D4, D4b): services only (F6); selling branch only; face balance × (pre-tax ÷ face) + the card's GST; its own block on the bill and receipt; no coupon or membership on that bill; forfeited at expiry | New split logic in Decimal (X11). The card stores its ratio and tax rate. The old discount button and the mismatched debit/refund are removed. |
| **Advance:** no GST on receipt; normal GST when spent; any branch (#34, F8) | Advance line forced to 0%; payment method |
| **Cashback:** expires per campaign; any branch; services and products (#35, F9) | Expiry per credit, consumed oldest first, nightly expiry, reminders |
| **Products:** MRP includes GST, shown inside it (#27, F22) | Tax extracted per line; customer pays the MRP. Needs MRP data (C5 Q2). |
| Split payment, full amount required (#37); online exact; cash change shown (#38) | "Cash received → change" field. **Payment reference** (UPI transaction id, card last four) stored. |
| **No tips** (#39); **no Nearbuy** (#40); Paytm later (F15) | Removed; history kept readable |
| Change payment method within **6 hours**, by permission (#41) | Server window; SAP payment re-posted; audited |
| **Drafts:** deletable; expire at branch end of day; no stock effect; one mechanism (#42, #43) | Server drafts only; delete works (D13); scheduled expiry |
| Finalised: no item edits; cancel and re-bill (#44) | Kept |
| **Cancel only, same day** (#45, #46, F19); blocked if a sold perk was used (#47); appointment cancelled (#23) | Refund removed. Same-day cancel payouts recorded by tender for cash reports. Credit memo to SAP **linked to the invoice** (X7). Credit-note numbering per C5 Q5. |
| Invoice number `DL01-2627-00045` (F20, D12) | New series for new bills; old numbers unchanged |
| **Valid GST tax invoice** (X5) | Receipt shows: invoice number (not the database id), "Tax Invoice" (or "Bill of Supply"), branch GSTIN and address, customer GSTIN for B2B, place of supply, HSN/SAC per line, rate, taxable value, CGST/SGST/IGST, round-off, amounts in paise |
| Line and header **snapshots** | Each line stores item code, name, HSN/SAC and rate at sale; the invoice stores customer name/phone/GSTIN, branch GSTIN/address and place of supply. Reprints never change. |
| Rounding to ₹1 (#50); universal receipt (#51, F21) | Kept; adaptive print layout |
| Multi-staff contribution % on every line (#49) | Kept, plus a server check |
| No sale without stock (#52) | Checked under row locks; FEFO batches; decimal quantities |
| "Manager" = the biller (#83) | `created_by` stored |
| Shift link | Each bill records the open shift and till, so each cashier is accountable (B5.10) |
| GST on/off per branch (#10) | Off → "Bill of Supply", no tax lines |
| Clean-up | Remove `validate_sap_stock` dead code (X12) and the SQLite-only hack (X31); CGST/SGST display-only (X20); fix the F8 label (X21) |

**Value card example** (F5 numbers; services at 5% as in today's data):

```
COVERED BY VALUE CARD VC-000123 (face ₹30,000 → ₹0)
  Service A 30,000.00 · Value-card benefit −10,000.00 · Taxable 20,000.00 · CGST 2.5% 500 + SGST 2.5% 500 · Adjusted from card 21,000.00
PAYABLE NOW
  Service B 15,000.00 · CGST 2.5% 375 + SGST 2.5% 375 · Payable 15,750.00
Invoice total 36,750.00 = card 21,000.00 + paid 15,750.00
```

- **Partial use:** a ₹6,000 service gives benefit −₹2,000, taxable ₹4,000, GST ₹200, ₹4,200 adjusted, and the face balance becomes ₹24,000.
- **Cancelling the using bill** (same day) restores the face value used.
- **Cancelling the selling bill** is allowed only if the card is unused.

### B5.7 Marketing

- **Target:**
  - Discount, FlatPrice (flat ₹ off) and Trigger (specific services) on the server (#53, F16, D1).
  - Multi-service packages with GST and validity (#55, #56).
  - Membership = % on services (#57).
  - Allowance returned on cancel (#54).
  - **Automated reminders** (#58).
- **Change:**
  - Promotion engine.
  - Package editor.
  - Reminder jobs.
  - Real WhatsApp integration (credentials empty, D5).
  - Consent checked before any promotional message.
  - Kept as they are: usage report, toggle status, delete blocked when customers hold the template, start date not in the past, organisation vs branch level, price calculators.

### B5.8 Inventory

- **Target:**
  - Products created for all locations or one branch, with branch price (#59); MRP rule (#60).
  - PO steps by permission (#61).
  - **No over-receipt** (#62); **never negative** (#64).
  - Request → approver → dispatch → receive → fulfilled (#63, #65).
  - Branch↔branch, warehouse→branch, **inter-state IGST** (F17).
  - **FEFO automatic** (#68, F18).
  - Internal checkout (#66); audit by permission (#67); low-stock alerts (#69).
- **Change:**
  - **One product catalogue with per-branch rows** for price, stock and availability: 1,927 products instead of 155,927 copies, and vendors likewise. Behaviour stays the same: "All locations" creates rows for every branch, and branch prices still differ. Old row ids are mapped so past bills keep their links (B11).
  - **Decimal quantities and units of measure** (X8).
  - **Batch stock** with FEFO.
  - **Transaction types:** RECEIPT, SALE, RETURN, TRANSFER_OUT, TRANSFER_IN, BOM_CONSUMPTION, CHECKOUT, AUDIT_ADJUSTMENT.
  - Atomic row-locked updates (X9).
  - Dispatch and receive steps; the transfer tax document.
  - Goods-receipt cap.
  - R6 and R17 fixes.
  - A scheduled **SAP master sync** replaces `pull_from_sap` (X27).
  - Low stock as a paged count and list, not a full download (X24).
  - Stock master POS vs SAP per C5 Q7 (X28).

### B5.9 Staff and payroll

- **Target:**
  - Rules by **designation** (#81).
  - Item commission as % or a fixed amount (#70).
  - Real-money scaling, no commission on package redemption (#71).
  - Perk points added by admins, no reset (#72).
  - Temporary transfer: revenue to the branch, incentive to the person (#73).
  - Payroll D8 with **Generate, Lock, Unlock, Mark paid** (#75); lock blocks bill edits (#76).
  - No staff app (#3).
- **Change:**
  - Flat item incentive split **by contribution %**, not evenly (X13).
  - Manual commission rows need their own permission and are audited (X14).
  - Salary changes audited (X15).
  - **Aadhaar masked or removed** (X16, C5 Q9).
  - The duplicate branch move after a transfer is removed (the frontend moved it a second time).
  - Perk use linked to a product writes a stock movement.
  - Kept: activity feed, designations with defaults, image upload, tools tracker with auto-return, every report and export.

### B5.10 Finance

- **Target, "enterprise-grade, fully automatic"** (#77–#82):
  - **Shifts:** several per day per till, float, cash in/out, X and Z reports. Bills are linked to the open shift.
  - **Closing computed by the server.** Expected cash = opening + cash sales − change − same-day cancel payouts − petty cash + top-ups − deposit. Method totals auto-filled; only counted cash entered; system cash and difference stored; edits only with `amend_closing`, always audited (R21).
  - **Petty cash:** categories, imprest float and top-ups, receipt photos, running balance, edit/delete by permission with audit, SAP posting if the SAP team wants it (D9).
  - **Every report exact:** one definition per figure, plus a nightly POS ↔ SAP reconciliation.
  - **Tax report ready for GSTR-1:** HSN summary, B2B vs B2C split, rate-wise totals.
  - Manager Discounts uses the biller (#83).
  - "Refunds" report becomes "Cancellations".
  - Shifts get their own permission (R5).
  - Incentive-rule "duplicate to branch" honours the chosen branch.
  - User names shown on petty-cash and shift lists. Today the serializers call `get_full_name`, which the user model doesn't have (`finance/serializers.py:5,67-68`), so the names are most likely dropped **[I]**.
  - Legacy `IncentiveConfig`/`IncentiveTier` (empty, no rows [D]) retired after confirmation.

### B5.11 SAP

- **Target:** real time from every branch, about 400 users, never slowing the POS, always correct (#84–#87); the SAP team's JE design kept; test company (D3).
- **Change:**
  - Outbox workers.
  - **SAP mapping in Settings.**
  - Sync monitor with retry.
  - Remove the sign-in SAP login (D7).
  - **Remove the 12% → 5% downgrade and the automatic SAP item-master patching** (X7). Errors go to the monitor instead.
  - Correct HSN per line.
  - Credit memo with `BaseEntry` to its invoice.
  - One card-code rule (B2C or B2B BP) with the B2C partner verified to exist.
  - TLS verification on.
  - New documents to the SAP team's specification: value-card split, payment-method change, expiries, inter-state transfer (D9, D10).
  - Apply migration `0002_sap_posting` and review the 6 legacy queue rows (A5).

**Kept exactly** (B10): every existing document type and its post-once key.

### B5.12 Approvals (new)

- **Policies:** price change, discount, stock request, stock transfer (F10). Conditions from the free limits in Settings. Approvers are one or many users, mapped to branches many-to-many (F13).
- **Flow:**
  1. The cashier requests approval and the bill waits (F12).
  2. Approvers get **POS, email and WhatsApp** messages with Approve/Reject. Links are single-use, expire in 15 minutes and are logged (D11).
  3. The first decision wins, and the till updates live.
  4. The server accepts the bill only if the approved values match.
- **Inbox:** approvals inbox and history report.

### B5.13 Notifications and integrations (new)

- **Events:**
  - approval requested or decided;
  - stock request raised;
  - transfer dispatched or received;
  - low stock;
  - SAP posting failed;
  - closing variance;
  - payroll ready;
  - client welcome;
  - client reminders for card, membership, package or cashback expiry (30 days);
  - optional appointment reminder.
- **Channels:** POS bell, email, WhatsApp. Recipients: users, roles, branch approvers, client. Consent and DND respected.
- **Integrations panel** (F14), changeable at any time:
  - Microsoft 365 Graph (app registration by your IT) or SMTP for your Outlook company mailbox (D6);
  - WhatsApp Cloud API, **empty until you fill it** (D5);
  - SAP and HANA;
  - Paytm later.

  Each has a **Test** button.

## B6. Data model

**New tables:**
- `AppSetting`
- `ApprovalPolicy` / `ApprovalPolicyApprover` / `ApprovalRequest` / `ApprovalToken`
- `NotificationRule` / `NotificationTemplate` / `Notification` / `DeliveryLog`
- `IntegrationConfig` (encrypted)
- `ProductCatalog` + `BranchProduct` (or equivalent)
- `VendorCatalog` + branch link
- `StockBatch`
- `UnitOfMeasure`
- `PettyCashCategory`
- `PayrollAdjustment`
- `SapOutbox` (evolves `SapSyncQueue`; `SapPosting` kept)
- Daily summary tables (branch / item / staff)

**Changed tables:**

| Table | Changes |
|---|---|
| `Center` | branch code, state code, location type, receipt address |
| `Client` | normalised, globally unique phone; consent flags; PIN retired |
| `Invoice` | `created_by`; shift and till; snapshots (customer name/phone/GSTIN, branch GSTIN/address, place of supply); IGST and per-rate tax breakdown; new number series; status read-only; refund retired |
| `InvoiceItem` | snapshots (item code, name, HSN/SAC, rate); value-card split fields; inclusive-tax fields; batch link |
| `Payment` | reference (UPI transaction id / card last four), branch, proper link to the value card |
| `ClientValueCard` | face balance, ratio, tax rate, expiry, branch |
| `ClientMembership` / `ClientPackage` | branch |
| `CashbackTransaction` | expiry and consumption |
| `Package` | multiple services, GST |
| `AppointmentService` | link to the service catalogue, end time |
| `StockTransaction` | decimal quantity, unit, new types, batch |
| `StockTransfer` | dispatch/receive, tax |
| `Shift` / `DailyClosing` / `PettyCashEntry` | server-computed fields, categories, attachments |
| `IncentiveRule` | designation targets |
| `StaffMember` | masked ID number |
| `Role.permissions` | gains `actions` |

**Database constraints:** unique client phone, product code, service code and vendor code at the database level (today only in code).

## B7. API

| Type | Change |
|---|---|
| **Added** | quote; approvals; notifications; settings; integrations; SAP outbox and monitor; batches; units; shift X/Z; petty-cash categories; payroll generate/unlock in the UI; client duplicate check and merge; catalogue endpoints |
| **Changed** | Invoice save: server computes discounts; `approval_id` needed above the free limit; value-card lines selected; **status, money fields and payments not writable**. Draft DELETE allowed. Client phone globally unique. Every endpoint and socket checks its module (R2–R25). Login returns normalised permissions without SAP; logout revokes the token. **List endpoints paginated one by one, with their screens updated in the same step** (X24). |
| **Removed** | refund endpoint, tips, Nearbuy, client-app and staff-app endpoints, `validate_sap_stock` |
| **Kept** | Every other endpoint and response shape the screens use (B10) |

## B8. Frontend

- **No redesign.**
- **New screens in the existing style:**
  - Settings; Approvals (policies and inbox); Notifications and Integrations; SAP Sync; the notification bell;
  - approval waiting state on the bill; cash received and change; the value-card block; the compliant receipt layout;
  - payroll buttons; warehouse and transfer steps; shift X/Z; petty-cash categories; client merge;
  - action ticks on the Roles screen.
- **Fixes with no design change:**
  - load the icon font, so 135 blank icons appear (X19);
  - CGST/SGST display-only (X20);
  - F8 label (X21);
  - remove every `isOwner` and use one permission service (R1, R8, R12);
  - service worker no longer caches API data (X18);
  - remove the stray React files;
  - non-blocking dialogs in the same style instead of `alert()`/`confirm()` (X32).
- **Smoothness:**
  - websocket updates instead of polling;
  - cached master data and debounced search;
  - background quotes;
  - skeleton loaders and virtual scrolling;
  - subtle modal and drawer transitions;
  - boot splash only until the app is ready (C5 Q13).
- **Maintainability (invisible):** `billing.ts` (about 2,760 lines) split into services; typed API models.

## B9. Performance, security, reliability

| Area | Approach |
|---|---|
| **Scale (~400 users)** | MySQL with indexes on every report filter (X26); `DATE()` filters rewritten per site with measurement (X25); paginated lists; summary tables; master data cached; SAP, notifications and audit off the request path; **400-user load test** |
| **Security** | HTTPS; `DEBUG` off; new secret key; CORS allowlist; encrypted secrets; rotate the weak SAP password; **SAP TLS verify on**; archive the 25 credential scripts and remove the old password comment and the hard-coded test account (X30); server-side status/money/approval enforcement (R19, X2); authenticated per-branch WebSocket (R18); no API caching in the service worker (X18); token revoked at logout (X23); Django admin restricted (R24); Aadhaar masked (X16); `xlsx` replaced (X22); full audit with no dropped entries (X17) |
| **Reliability** | Outbox; idempotent checkout; post-once SAP; retries; **daily MySQL backups plus point-in-time recovery**; health checks; nightly reconciliation; **CI** (tests + build + migration check on every change) |

## B10. Preservation checklist (must keep working after every phase)

Each phase's demo re-checks this list. Details are in `POS_ARCHITECTURE_AND_BUSINESS_LOGIC.md` §3–§5.

**Login and shell:**
- Login.
- Branch selector and shared date filter across admin pages.
- Sidebar by permission.
- Toasts.
- Loading indicator.
- Double-click guard on submits.
- Session expiry back to login.
- Live dashboard updates.
- Chat: rooms, unread count, messages, images, reactions, reply, edit/delete, read receipts, typing indicator.

**Home and Dashboard:**
- Month and today KPIs.
- Target gauge with monthly target edit.
- Revenue chart.
- Activity list.
- Six dashboard tabs with month/day drill-downs.
- Chart expand and download.

**Billing:**
- Landing: today's bills, appointments, staff activity, activity feed.
- Client search (phone or name) and inline client creation.
- Client history.
- Catalogue tabs: services, products, memberships, cards, packages, advance, custom package builder.
- Config drawer: staff multi-select with %, price, quantity, discount.
- Package redemption lines.
- Membership selection.
- Promotions.
- Totals identical to the server.
- Checkout:
  - split tenders;
  - wallet and card balances shown;
  - success modal and print.
- Barcode scan (products, then services).
- F2, F4, F8 / Ctrl+Enter and Esc with confirm-before-clear.
- Appointment → bill pre-fill.
- **Idempotent checkout:** a retry returns the same bill.
- Finalise exactly once.
- Payroll-lock check.
- `BillChangeLog` entries: Create, Finalize, Price Adjusted, Cancel, Change Payment.
- Admin Bills, Changes and Manager Discounts pages with CSV export.

**Clients:**
- Paged list and search.
- Profile: invoices, advances, service history, perks with progress bars.
- Create/edit with phone, name, email, birthday, gender, GST number, notes, DND, blacklist with reason.
- Bulk upload and template.
- CSV export.

**Services:**
- Central catalogue.
- Branch price, incentive and active override.
- Bulk upload with "where it went" report.
- Template and export.

**Appointments:**
- Staff timeline with the unallocated row.
- KPIs.
- Show-cancelled toggle.
- Booking modal with recent visits.
- Back-to-back service chaining.
- Duration and price estimates.
- Cancel and restore.
- Send to billing.

**Inventory:**
- Ten tabs: checkout, stock history, POs, product master, vendor master, PO history, GRPO, transfers, requests, audit.
- Stock value and low-stock KPIs, fast movers.
- Lots.
- Vendor-product mapping.
- Stock history by date with CSV.
- Product and vendor bulk upload and templates.
- PO create/edit/delete with item maths.
- Forward-only PO status.
- "All locations" product creation and update.
- Low-stock export.

**Staff:**
- Directory with filters and activity feed.
- Profile autosave.
- Designations with default salary and commission.
- Image upload.
- Service logs and perk usage (points, then money).
- Transfers (temporary auto-revert).
- Tools (auto-return).
- Payroll list.
- Reports: revenue, usage, commission, perks, tools, transfers, incentives, all with exports.
- Bulk uploads for staff, logs, consumptions, transfers and tools.

**Finance:**
- Register summary (billed vs collected, payment buckets, liability draw-downs, expected settlement).
- Monthly sales.
- Detailed revenues (paged).
- Refunds → Cancellations.
- Procurement.
- Tax.
- Service drilldown.
- Staff performance.
- Incentive calculation.
- Multi-salon: per-branch summaries sorted by target, balances, categories, drilldowns, petty cash, clients, staff, exports.
- Petty cash.
- Daily closing with opening-balance carry-forward across gaps.
- Closing history.
- Shifts.
- Incentive-rule manager:
  - categories: services, products, cards, memberships, packages, daily-business slabs, add-ons, targets;
  - monthly/daily frequency;
  - tiers;
  - validity;
  - branch-specific rules.

**Marketing:**
- Org and branch campaigns.
- Price calculators.
- Toggle and delete (blocked when held).
- Edit modal.
- Usage report.
- WhatsApp history.

**Wallets and perks:**
- Append-only advance and cashback ledgers.
- Reversals against remaining exposure.
- Memberships stack.
- Package redemption and restore.

**Incentive engine:**
- Rule precedence (branch, then role specificity, then newest).
- Highest qualifying tier.
- Real-money base.
- Add-ons and targets additive.
- Fallback to staff rate, then item master.

**SAP:**
- Business partner (B2C generic / B2B own).
- Item, item update, vendor, warehouse.
- A/R invoice with UDFs (`U_POSINNO`, staff and customer name/phone, B2C name/phone).
- Duplicate check by `U_POSINNO`.
- Incoming payment (only the parts SAP lacks).
- Credit memo.
- JEs for advance, value card, membership, package and value-card redemption.
- PO and GRPO per receipt.
- Inventory transfer.
- Goods issue/receipt for checkout and audit.
- IGST/CGST determination from state codes.
- HANA batch selection.
- Post-once keys.
- `PaidToDate` rule for old invoices.

**Audit and money:**
- Every write logged with sensitive fields redacted.
- Money in Decimal with exact paise splitting.
- Branch scoping that fails closed.

## B11. Data migration and clean-up

Each step runs on a copy first, is checked, and is approved by you.

1. **Freeze and back up.** Git snapshot on a new branch; copy of `db.sqlite3`.
2. **Apply pending migrations** to the real database (`0002_sap_posting`). Review the 6 legacy SAP queue rows with the SAP team.
3. **SQLite → MySQL:**
   - copy every table **with its primary keys unchanged**, including `django_content_type`, because invoice lines point to items through content-type ids;
   - utf8mb4;
   - compare row counts and money totals per table;
   - remove the SQLite-only hack (X31).
4. **Clients** (Q1 answered: keep):
   - **keep** the 34,334 phone-less B2B records (no archiving);
   - resolve the 5 duplicate groups with the merge tool;
   - normalise phones;
   - switch on the unique constraint for non-blank phones;
   - give clients without a branch a home branch only when they are first billed.
5. **Catalogue consolidation:**
   - build 1,927 catalogue products from 155,927 rows;
   - keep each old row as that branch's stock and price row with **its old id preserved**, so existing invoice lines, POs, lots and transfers keep their links;
   - same for vendors;
   - set MRP from the SAP price list you choose (C5 Q2).
6. **Stock:**
   - convert quantities to decimal with units;
   - opening batch balances from SAP batches where available, otherwise an "unbatched" batch.
7. **Value cards:** backfill each card's ratio (pre-tax ÷ face = 2⁄3) and tax rate (5%) from its template; balances are already kept in face value.
8. **Branches:** fill branch code, state code, location type and receipt address for all 81.
9. **Billing history:**
   - existing invoices keep their numbers, statuses and amounts;
   - add snapshots from current data, marked "back-filled";
   - the new invoice series starts at 00001 per branch for the current financial year;
   - no historical tip, Nearbuy or refund rows exist [D], so nothing to convert.
10. **Roles:** none exist [D]. Create the starter roles you ask for (for example Cashier, Branch Manager, Accountant, Inventory Approver).
11. **`repair_invoice_math`:** dry-run report → your decision → apply (C5 Q12).
12. **Cut-over:** rehearse the whole sequence on a copy, then run it on the live data in a quiet window, with a rollback plan.

---

# Part C: Delivery

## C1. Phases

Each phase ends with tests passing, the **B10 checklist**, and a **demo for your sign-off**. Sizes: S = days, M ≈ 1 week, L = 1 week or more.

| Phase | Content | Depends on | Size |
|---|---|---|---|
| **0. Safety and platform** | Git snapshot on a new branch; archive the credential scripts, remove the old password comment and the hard-coded test account (X30); apply pending migrations; local **MySQL 8.4** with data copied and checked (B11 1–3); worker framework; rewrite the 24 out-of-date tests to the confirmed rules; `repair_invoice_math` dry-run report | — | M |
| **1. Dynamic RBAC + Settings** | B3 tree with actions; fix R1–R16, R18, R21–R25; Roles screen action ticks; Settings module (B4) with defaults equal to today | 0 | M |
| **2. SAP pipeline** | Outbox and workers; SAP mapping in Settings; monitor; remove the sign-in SAP login; remove 12→5 and item-master auto-patching; credit-memo `BaseEntry`; HSN; card codes; TLS; audit-log writes on the job queue | 0, 1 | M |
| **3. Critical correctness and security** | **Server-only status and payments (R19/X1/X2)**; serializer read-only audit (R20); double discount (interim); product GST inside MRP; drafts; remove tips, Nearbuy and refund; same-day cancel with perk-used block and appointment cancel; new invoice numbers; staff-split check; over-receipt; never-negative with row locks; transfer-create crash (R17); appointment clash rules; dead code; service-worker API cache (X18); icons (X19); CGST/SGST display (X20); F8 label (X21) | 1 | M |
| **4. Clients global** | Clean-up (B11-4), merge tool, unique phone, any-part search, consent flags, bulk-upload rules, remove mobile apps, welcome e-mail as a notification | 1 | M |
| **5. Approvals + Notifications + Integrations** | B5.12, B5.13; email; WhatsApp (empty credentials); POS bell | 1, 2 | L |
| **6. Billing engine** | Quote; promotion engine (branch-filtered); membership picker (branch, cap before payment); value-card split in Decimal; advance and cashback rules; payment references and cash change; payment-change window; **compliant receipt and snapshots**; shift link | 3, 5 | L |
| **7. Inventory** | **Catalogue consolidation (B11-5)**; decimal and unit quantities; batches and FEFO; transaction types; warehouses; request → approve → dispatch → receive; inter-state tax; SAP master sync; low-stock paging | 2, 5 | L |
| **8. Appointments** | Branch hours, catalogue-linked lines with end time, statuses | 1 | S |
| **9. Staff/payroll + Finance** | Designation rules; flat split by contribution; manual-commission control; salary audit; Aadhaar masking; payroll D8 with buttons; shifts X/Z; automatic closing; petty cash; GSTR-1-ready tax report; report definitions; summary tables; reconciliation | 6, 7 | L |
| **10. Marketing** | Multi-service packages with GST, reminders, consent, promotion editor | 5, 6 | M |
| **11. SAP end-to-end** | Every document posted to the SAP **test** company and checked with the SAP team | 2, 6, 7, 9 | M |
| **12. Performance, smoothness, security, go-live** | Pagination per endpoint with its screen; `DATE()` filters; indexes; frontend polish (X32, X33); `xlsx` replacement; token revoke and storage; Django admin restriction; CI; backups; 400-user load test; production setup; cut-over (B11-12) | all | M |

**Phase 0 status (2026-10-12): done, awaiting sign-off.**
- Snapshot commit `a1b5d92` on branch `pos-rebuild`.
- 161 scripts archived to `../pos-legacy-scripts-2026-10-12`.
- MySQL 8.4.11 (signature verified) running locally; all 70 tables copied and verified.
- Migrations `0002`, `0003`, `marketing.0017`, `staff.0025` applied.
- Database-backed job runner (`pos_backend/jobs.py`, `run_workers`, `deploy/pos-worker.service`).
- Suite: **899 tests pass on SQLite and on MySQL** (5 expected failures await Phase 3, 1 SQLite-only skip).
- `repair_invoice_math` dry run: ₹0.00 movement, so applying is not recommended.
- Findings X34–X42 recorded.

**Phase 1 status (2026-10-12): done, awaiting sign-off (not committed).**
- **Superuser is the only built-in identity.** A role named "Owner" gets exactly what its ticks say (backend `is_superuser`, frontend `isOwner()`, 16 screens). Migration `salon_admin.0014` gives existing roles named owner full ticks, so nobody loses access.
- **Action ticks** (stored as `permissions[module][sub].actions`), enforced on the server and shown or hidden in the UI:
  - cancel and change payment on bills;
  - generate, lock, unlock and mark paid on payroll;
  - set app PIN;
  - reset password;
  - bulk import of centres;
  - amend a closing.

  The Roles screen lists each tick under its submodule, in the same checkbox style.
- **Strict reads where one tab must not open another:**
  - every finance report;
  - staff reports;
  - Admin > Users, Roles, Changes and Settings.

  Shared reference data (products, services, staff names, clients) stays readable by the screens that use it.
- **Branch scoping on the server** for inventory, transfers, requests, tools and services, plus `can_use_center` on writes. Screens now default to "all centres" for any all-centres role, not only the superuser.
- **Admin > Settings** (`/app-settings/api/`, `/admin/settings`):
  - five settings so far, each per branch or global as appropriate;
  - every change is audit-logged;
  - an unchanged system resolves to today's values.
- **Fixes found during the live role check:**
  - Finance > Manage did not govern incentive rules; it does now.
  - Any Admin tab could list every login and role; it can't now.
  - Salary needed only *read* on Staff Profile to change; it needs Edit now.
  - The Admin menu entry always went to Centres; it now goes to the first Admin page the role can open.
  - Screens called chat, low-stock and appointments for roles without them; they don't now.
  - The Billing "Staff Activity" card kept its read access.
- **Proof:** four QA roles (Cashier, Accountant, Store, Settings) were checked through the API and in the browser. Each sees only its own modules, tabs and buttons, and typed-in forbidden URLs redirect.
- **Tests:** backend suite on SQLite and on MySQL, with new `pos_backend/tests_rbac_phase1.py`; frontend 110 unit tests and the build pass.

**Phase 2 status (2026-10-12): done, awaiting sign-off.**
- **Outbox.** Every SAP posting is a `sap.sync` background job written in the same transaction as the bill or stock move. All 25 call sites are unchanged, because `run_sap_sync_async` now enqueues. `run_workers` (or the in-process worker when `SAP_SCHEDULER=1`) posts the jobs. The till never waits on SAP.
- **How jobs run:**
  - priority order: masters → invoice → payments and journals → stock;
  - a payment or credit memo waits for its invoice without spending attempts, and the invoice is queued if it wasn't;
  - a bill that posts no invoice (cards only) has nothing to pay against;
  - SAP switched off means jobs wait;
  - 10 attempts (about 3 hours) before "needs attention";
  - new work revives a dead job;
  - work arriving while a job runs makes it run once more.
- **Admin › SAP Sync** (`/admin/sap-sync`, permission `admin.sap_sync` with Retry and Resolve ticks):
  - counts for needs attention, retrying, waiting and posted;
  - SAP's own error text;
  - the SAP document number once posted;
  - branch scoping;
  - Retry and "mark done by hand" with a required note;
  - both actions audit-logged.
- **SAP mapping in Settings:** 20 values (accounts, B2C partner, warehouse, item group, price list, location, sales returns). The defaults are today's values. The literals are gone from `sap_utils`.
- **X7 fixed:**
  - no 12% → 5% rewrite (SAP has the 12% codes);
  - no forced `HSNEntry: 1`;
  - no auto-patching of item masters, and no IGST/CGST flipping against GSTINs;
  - one card-code rule: `CUST…` legacy code, `C<id>` for B2B, otherwise the B2C partner from Settings.
- **Also fixed:**
  - SAP lists are read across all pages (20 rows per page). The old single-page read missed business places after the 20th and **re-created warehouse locations**.
  - The till's 3-second live SAP stock check, whose result was thrown away, is removed.
  - Deployment check W011 flags TLS verification being off.
- **Legacy queue:** migration `0005_sap_outbox` moved the old rows. The 6 "unknown" rows are value-card sale journals for bills 8, 9, 13, 15, 16 and 37 (₹21,000 each). They are held in Needs attention, and a read-only check confirmed none of them is in SAP yet.
- **Live test on the SAP test company** (test bill `PT1012135048`, ₹105):
  - A/R invoice **DocEntry 47**: `CG+SG@5`, revenue account 4130100018, `DL_Main`, B2C;
  - incoming payment **DocEntry 20**;
  - credit memo **DocEntry 7**, the first credit memo ever to reach SAP. Before this, every credit memo failed with "G/L account is missing". It now credits the sale's revenue account, or the returns account set in Settings.
- **Read-only facts:**
  - `WALKIN` partner missing: every walk-in bill was failing, now fixed by the rule above;
  - all 81 branches' warehouses and business places exist in SAP;
  - `ZYWH001` (the fallback) does not exist;
  - service items have no HSN in SAP, which SAP accepts.
- **Tests:** 970 backend tests pass on SQLite and MySQL (including the new `tests_sap_phase2.py`); 110 frontend tests and the build pass. One intermittent MySQL failure under parallel load could not be reproduced in 5 reruns. The test now names any leftover job.
- **For the SAP team (C5 Q16–Q20).**

**Phase 3 status (2026-10-12): done, awaiting sign-off** (commits `62680af`, `aaa766d`).
- **Bill integrity:**
  - Status follows the money (R19/X1). The request's status is only "hold" or "complete". Completing with no payment, holding with a payment, and PATCHing a status are all refused.
  - Only drafts are edited (X2).
  - SAP, tip and shift-close fields are server-only (R20, one guard for all SAP fields).
- **Coupon double discount (X3, interim):**
  - the till's offer discount is checked against the offer (services only, pre-tax) before payment;
  - finalising records the promotion without adding it again;
  - the membership a bill used is recorded.
- **Removed:** tips (#39) and Nearbuy (#40), with old records still readable; refunds (#45, F19).
- **Same-day cancel only.** A cancel is blocked once the value card, package, membership or advance sold on the bill has been used (#47). Cancelling now takes back an advance the bill sold; before, the client kept it. It also cancels the appointment (#23).
- **Drafts:** deletable (D13); earlier days' drafts expire hourly (#42).
- **GST inside the MRP (X6, F22).** The taxable value is worked out of the MRP and stored like every other line, so reports and SAP need no change. The till mirrors it, and the shared fixture was regenerated with one product case changed, checked by hand. Product commission is now on the value before GST (₹423.73 on a ₹500 MRP at 18%).
- **Invoice numbers `DL01-2627-00045`** (F20, D12):
  - branch codes come from the GSTIN state and are editable in Admin › Centres;
  - all 81 branches got codes (DL01–DL38, UP01–UP12, …);
  - the new series starts at 00001;
  - old numbers are unchanged.
- **Staff contribution % (#49).** It is now sent, checked (sums to 100), and drives each commission share. Before, it was never sent, so commission was always split evenly.
- **Stock:**
  - over-receipt is refused;
  - checkout and stock audit really lock product rows (X9);
  - a transfer can't make stock negative (#52).
- **Appointments.** Every update is re-checked for double-booking, including date moves and restores (#21, #22), plus the branch and blacklist checks.
- **Smaller fixes:**
  - X12 dead code removed;
  - X18 the service worker no longer caches API responses;
  - X20 CGST/SGST are display-only;
  - X21 the shortcut label reads F8;
  - X40 the cancellations export works;
  - X41 the login background (deleted by mistake) is restored.
- **Found live and fixed:** a cashier with Add on New Invoice can now complete a held bill.
- **Not done:**
  - X19 Font Awesome needs a package download (your approval);
  - X31 SQLite deadlock workaround kept, harmless on MySQL and needed for local SQLite;
  - C5 Q5 (cancellation note series) is for your CA;
  - the expected cash on a shift still comes from the till (X4, Phase 9).
- **Tests:** 1007 backend tests, no expected failures left; 110 frontend tests; build OK. Live check: bill `DL01-2627-00001`.

**Phases 4 + 8 status (2026-10-12): done, awaiting sign-off.**
- **Clients (Phase 4):**
  - Clients are global (#11). Search matches any part of the name, the phone digits or the GSTIN, from any branch.
  - One client per phone across the business (F2). Phones are normalised (`+91 98765-43210` → `9876543210`). A clash names the owner ("already used by Asha (client #12)"). Registering an archived client's number brings that client back.
  - The migration keeps the oldest active record of each shared number; the others keep their phone and appear under **Admin › Clients › Duplicates** for merging. Dev copy: 5 numbers, 6 records, all test data.
  - New clients need a name and phone (F3). The 34,334 legacy B2B records without a phone (Q1) stay editable.
  - GSTIN is checked for format, state code and check character. It can be entered on the till's client card and the Clients page. Adding one re-posts the SAP business partner.
  - The **merge tool** moves bills, appointments, perks, wallets and logs onto the kept client, fills its blank fields, and archives the duplicate with a note. It is logged.
  - New ticks: **Blacklist**, **Merge** and **Bulk upload**. The till still saves clients normally, because only a real blacklist change is checked.
  - WhatsApp and SMS consent flags were added (B5.3), on by default.
  - **Client and staff apps removed (#3):**
    - their login and data endpoints are gone;
    - the PIN hash is never returned;
    - no PIN is generated;
    - the "Chilli Potato" welcome e-mail was replaced by a background job signed with the branch name, switchable in Settings.
- **Appointments (Phase 8):**
  - Branch hours (#20) are set in **Settings › Appointments**, globally or per branch:
    - opening and closing time;
    - closed weekdays;
    - slot size;
    - default duration (09:00–22:00, 15 and 45 min unless changed).
  - Bookings outside the hours or on a closed day are refused. The calendar draws the branch's own hours.
  - **Statuses (D14):**
    - Checked-in, which still holds the stylist;
    - No-show, which frees the stylist;
    - Completed, which is set by billing only and can't be undone by hand.
  - Each status move needs its tick: check-in, no-show, cancel or restore. Changing the stylist of a booked line needs **assign_staff**. New lines and unallocated lines need no tick.
  - Service lines are linked to the catalogue, which fills the name and duration, and carry an end time. Old free-text lines were linked where the name matched exactly one service. The till loads appointment lines by that link.
  - One validator covers create, update, assign and restore. The blacklist check now finds the client however the phone is written.
- **Before go-live:** existing roles have none of the new ticks. Until the owner ticks them in Roles, only the owner can:
  - cancel, restore, check in or mark no-show;
  - change a stylist;
  - blacklist, merge or bulk-upload clients.
- **Tests:** 994 backend tests on SQLite and MySQL; 110 frontend tests; build OK. Live check on the dev MySQL copy as QA Cashier: hours, the clash message, refused GSTIN and Completed-by-hand, and ticks refused. Probe rows were removed afterwards.

**Phase 6 status (2026-10-12): done, awaiting sign-off (not committed).**
- **One engine** (`billing/engine.py`) prices the quote and the saved bill. New `POST /billing/invoices/quote/` returns lines, offer, value-card cover, tax per rate, rounding, payable now and the branch's payment methods. The till shows it; `priceCart` still gives the instant figure.
- **Offers from their id only** (X3 closed for good): the till's discount figure is ignored when a promotion or membership is sent. Services only, pre-tax, one per bill (#28–#30, D1). Discount (% or ₹, whole bill or the named service, minimum bill), FlatPrice (named service at the flat price, or flat ₹ off), Trigger (both services present → % off the services, F16), Cashback (credited at completion). The till lists only offers usable at its branch today. Each line stores its share of the bill discount, so SAP lines carry it (no header DiscountPercent; products are no longer discounted in SAP by a services offer).
- **Memberships** only at the selling branch (#36, Q3) and capped before any payment (X10). Packages redeem only at the selling branch (#32). Setting: *Wallets & perks › used only at the branch that sold them* (on).
- **Value card (F5)** in Decimal: the card's terms (face, pre-tax price paid, GST rate) and branch are fixed at sale; it covers services in bill order; face × pre/face is taxed at the card's rate; the bill takes no other discount (D4/D4b); expired cards are refused and forfeited hourly (F7). The till's "Auto-Apply Value Card Discount" became "Pay with ‹card›"; the summary shows *Covered by value card* and *Payable now*. The card's payment row carries the face value taken, so a same-day cancel restores exactly that (the old scaled-debit/unscaled-refund mismatch is gone). A part-covered line at a different rate goes to SAP as two lines.
- **Advance** line always 0% GST (#34, F8). **Cashback** credits carry the campaign's expiry and are spent soonest-expiry first; an hourly job writes off what is left (#35); a cancel gives spent cashback back to the same credits.
- **Payments:** full amount required (#37; part-paid bills from before stay payable). Branch payment list in Settings (default Cash, UPI, Card, Cheque). Cash received → change shown and stored; online exact (#38). Reference field (UPI id, slip / last four, cheque no.). Payment-method change only within **6 h** (setting) (#41); value-card parts can't be swapped; SAP gets the old incoming payment **cancelled and re-posted** (queue type `payment_repost`). Settings › SAP mapping has an account per payment method (Q18; empty = today's account).
- **Tax receipt (X5):** snapshots of customer (name, phone, GSTIN) and branch (legal name, GSTIN, address, phone), place of supply, Tax Invoice / Bill of Supply, item code and HSN/SAC per line, taxable value, CGST/SGST per rate, round-off, paise, payments with references and change, value-card block, biller. Paper: Auto / 80 / 58 mm / A4 and a footer, per branch. Bills record `created_by` (#83) and the open shift.
- **E-invoice (Q4b):** B2B tax invoices are queued (`einvoice.generate`), the portal JSON (schema 1.1) is built, and the bill shows *not configured* until the CA's IRP/GSP access is plugged in (`billing/einvoice.py › _submit`). It also lists missing data (branch PIN, client address).
- **Migrations:** `billing 0031/0032`, `clients 0019`; the backfill gave the 19 dev cards their terms and branch, snapshots to completed bills, biller from the change log. Backup: `C:/Users/HP/pos-mysql/backups/pos-before-phase6-2026-10-12.sql`.
- **Proof:** new `billing/tests_phase6.py` (32, incl. the worked example 21,000 + 17,700 = 38,700) and the existing suites; live on the dev MySQL as QA Cashier: bill `DL01-2627-00002` (card ₹2,100 for face ₹3,000 + cash ₹1, received ₹1,000 → change ₹999) printed as a full tax invoice, then cancelled (card back to ₹25,800) and its SAP jobs removed.
- **Follow-ups resolved (same day):**
  - Client **billing address + PIN** (till client card shows them once a GSTIN is entered; Clients page) and branch **PIN** (Centres). They are snapshotted onto the bill, printed for B2B, and fed to the e-invoice.
  - **Value-card forfeiture journal**: Dr value-card liability / Cr *Forfeited value-card income* at the unused face × pre-tax/face. Posts once Settings › SAP mapping names that account (empty by default, so nothing posts until the SAP team confirms); forfeitures made before then are queued automatically when it is set.
  - **Cashback expiry needs no SAP entry today**: cashback awards are not booked in SAP (only spending is, from the cashback liability account), so there is nothing to reverse. If the SAP team wants awards booked as a liability, expiry then reverses them.
  - Receipt T&C now reads "Service prices are exclusive of GST; product prices (MRP) include GST."
- **Still for your CA:** the e-invoice provider (NIC direct or a GSP) and its credentials; the portal call is plugged into `billing/einvoice.py › _submit`.

**Phases 5 + 10 status (2026-10-13): done, awaiting sign-off (not committed).**
- **Approvals (Phase 5):**
  - Admin > Approvals: switch *Discount* and *Price change* on or off, and map approvers per branch (one person for many branches, many people for one branch). Both are **off** until switched on, so an unchanged system bills as before. Stock request and stock transfer are listed in the code but hidden until the stock flow ships (Phase 7).
  - Free limits are in Settings > Approvals (0% by default = every typed discount or price change), with how long an approval can be used (60 min), how long an unanswered request lasts (30 min) and how long the e-mail / WhatsApp link works (15 min).
  - Flow: the till's quote lists what needs approval; *Complete* opens "Waiting for approval"; the server prices the bill itself and opens one request per need. Approvers see it in the **bell** (Approve / Reject), in e-mail and on WhatsApp. The first decision wins. The till finishes the bill by itself when approved.
  - The server accepts the bill only if the approved figures cover it: a discount no larger than approved, a price change to exactly the approved price; same branch, same cashier, not expired, used once. Held drafts may carry anything; **completing** (Complete or Pay) is what is checked.
  - Links are single-use, expire in 15 minutes and show the request without deciding it (mail scanners open links); only a button press decides. Every decision records who, when and by what route (history in Admin > Approvals).
  - With no approver mapped to a branch, the superusers can decide, so a switched-on policy can never leave a bill stuck.
- **Notifications and integrations (Phase 5):**
  - Bell with live push over `/ws/user/` (own channel per user) and a 60 s fallback poll.
  - Admin > Integrations: Microsoft 365 (Graph), SMTP and WhatsApp Cloud forms, each with a **Test** button (its own tick). Secrets are encrypted in the database and never sent back to the browser. **WhatsApp ships empty**: messages are logged as "not sent" and nothing is charged.
  - Notifications tab: per event, switch it, choose bell / e-mail / WhatsApp, add roles or named people. Events wired now: approval requested / decided, stock request raised, SAP posting failed (after all retries), daily low-stock summary.
  - Messages tab: every e-mail and WhatsApp with status and error. The client welcome e-mail now goes through the same mailbox.
  - New ticks: Admin > Approvals, Admin > Integrations & Notifications (+ *Send a test message*). Existing roles have neither until the owner ticks them.
  - Server setting `APP_BASE_URL` (the address approval links point to); `INTEGRATION_KEY` optionally replaces `SECRET_KEY` for encrypting integration secrets.
- **Marketing (Phase 10):**
  - **Packages** hold several services, each with its own sessions (names come from the catalogue), and carry their own **GST %** (empty = the business rate in Settings, as before).
  - **Reminders** (job `marketing.expiry_reminders`, every 6 h): value card, membership, package or cashback expiring within 30 days (Settings > Client reminders; 0 = off), once per item and channel. Optional appointment reminder (0 h = off by default).
  - **Consent first**: a client without WhatsApp consent, on DND, blacklisted or inactive is never messaged; e-mail goes only where an address exists. Campaigns now skip clients without consent and report how many.
  - **Campaigns** go through the WhatsApp channel; the Marketing > WhatsApp list shows *Queued / Sent / Not sent - WhatsApp not set up*. Who may send is the Marketing > WhatsApp tick, not a role named "marketing".
- **Choices made without asking (change on request):** a reminder is not sent on the day an item is sold; a card sold with under 30 days validity is reminded from the next day; approvals are per cashier (nobody can borrow another's).
- **Not done / later:** stock request and transfer approvals (Phase 7); closing-variance and payroll-ready notifications (Phase 9); promotion-editor changes beyond what exists (promotion types, validation and usage report were already server-side).
- **Proof:** 1076 backend tests on SQLite (+ MySQL, see below), including 33 for approvals and notifications and 13 for marketing; live on the dev MySQL: QA Cashier asked for a 13.33% discount, QA Approver approved from the bell, the till finished bill `DL01-2627-00003` by itself and the request became *used* (bill cancelled afterwards, its SAP jobs removed, policy switched back off). Backup: `C:/Users/HP/pos-mysql/backups/pos-before-phase5-10-2026-10-13.sql`. Migrations: `approvals 0001`, `notifications 0001`, `marketing 0018`.

**Phases 7 + 9 status, first part (2026-10-13): done, awaiting sign-off. Not complete - see "Not done".**
- **Inventory (Phase 7):**
  - **Decimal quantities and units** (X8): stock, ledger, PO, request and transfer quantities are decimals to the thousandth; each product has a unit (pcs, ml, g...). A service that uses 2.5 ml now takes exactly 2.5 ml (it used to truncate). SAP gets the quantity without trailing zeros.
  - **Batches, earliest expiry first** (F18, #68): a goods receipt with a batch number or expiry makes a batch; sales, service consumables, checkout, audit shortfalls and transfers take from the batch that expires first, then from stock that has no batch. The ledger gets one row per batch used and says which. Stock from before batches existed is the "unbatched" remainder and is used after the batches.
  - **Transaction types:** `TRANSFER_OUT`, `TRANSFER_IN`, `BOM_CONSUMPTION` added (service consumables were typed `SALE`).
  - **Transfers:** *Draft > In Transit (dispatch) > Completed (receive)*. Dispatch takes stock out of the sending branch (never below zero) and needs access to that branch; receive adds it to the destination in the same batches and needs access there; goods on the road are in neither. Draft straight to Completed still works and does both. SAP posts the transfer when it is received. The Inventory screen shows **Dispatch** and **Receive**.
  - **Stock approvals** (F10): *Stock request* and *Stock transfer* are now switchable in Admin > Approvals. A transfer cannot be dispatched, and a request cannot be marked fulfilled, until an approver says yes. The first attempt opens the request and tells the approvers (bell, e-mail, WhatsApp); the action is repeated once it is decided; the approval is spent once.
  - A staff perk that uses up a product now takes it off stock with a ledger row (it used to subtract silently).
- **Finance and staff (Phase 9):**
  - **Closing computed by the server** (X4): cash and each method taken that day, petty cash, opening balance, expected cash, difference and closing balance are worked out from the bills (same definition as the Register Summary); only the cash counted and the deposit come from the person. Amendments (own tick) are recomputed and logged. New `daily-closing/compute/`. A shift's expected cash is the server's too.
  - **Flat incentives follow contribution %** (X13): value-card reward, add-on bonus and flat item rates are split by the same percentages as the revenue, to the paisa.
  - **Manual service logs** need their own tick (*Add or change a service log by hand*) and are written to the activity log (X14).
  - **Salary and commission changes are logged** with old and new values (X15).
  - **Aadhaar keeps only the last four digits** (X16, Q9 recommendation): new entries are masked on save and the migration masked the 25 stored numbers.
- **Not done in this part (still Phase 7/9):** catalogue consolidation (B11-5: 155,927 rows to 1,927 - needs a rehearsal and your sign-off on the id mapping first); inter-state tax document for transfers (waits for the SAP team, D9/D10); scheduled SAP master sync and the stock-master rule (Q7); low-stock as a paged list (X24); several shifts per day with X/Z reports; petty-cash categories and receipts; designation rules; GSTR-1-ready tax report; summary tables and the nightly POS-SAP reconciliation; closing-variance and payroll-ready notifications.
- **Proof:** 1097 backend tests on SQLite (+ MySQL below), 21 new in `inventory/tests_phase7.py` and `finance/tests_phase9.py`; 110 frontend tests; build OK. Live on the dev MySQL: a product with 10.5 ml moved by transfer (dispatch left 6.25, receive added 4.25 at the other branch, ledger `TRANSFER_OUT -4.25` / `TRANSFER_IN 4.25`), then removed. Backup: `C:/Users/HP/pos-mysql/backups/pos-before-phase7-9-2026-10-13.sql`. Migrations `inventory 0023, 0024`, `staff 0026` (masking is one-way; the backup has the originals).

**Phases 7 + 9 status, second part (2026-10-13): leftovers done except two that need your decision.**
- **Finance:** several shifts per day, one open per till (`till` field, API-ready; the screen still opens "Till 1"); **X report** (shift so far, no closing) and **Z report** (returned when a shift closes); **petty-cash categories** (picked or added on the petty-cash form); **GSTR-1-ready tax report** (B2B/B2C totals, rate-wise totals, HSN/SAC summary added to `reports/tax/`); **POS-to-SAP reconciliation** per branch (`reports/sap-reconciliation/`: billed, in SAP, not in SAP; reads POS posting state, never calls SAP); user names now show on petty-cash and shift lists.
- **Rules by designation (#81):** an incentive rule's role may list several designations ("Senior Stylist, Colourist") besides the old keywords.
- **Notifications:** *closing out of balance* (daily closing and shift close) and *payroll ready*, configurable in Admin > Integrations.
- **Inventory:** low-stock badge uses a count (`low_stock/?count_only=1`); the list is paged/capped at 500 (X24). **Scheduled SAP master sync** (`inventory.sap_master_sync`): off until Settings > SAP mapping "refresh every (hours)" is set; it only adds, never overwrites a branch's stock, names or prices.
- **Still open, need you:** (1) **catalogue consolidation** (155,927 to 1,927 products) - a data move on live-shaped data; I will rehearse it on a copy and show the id-mapping check before touching anything, say go. (2) **Inter-state IGST transfer document** - needs the SAP team's document design (D9/D10).
- **Not built (judgement):** receipt photos on petty cash (needs file storage decision) and nightly summary tables (the 60 s report cache stays; no measured need yet).
- **Proof:** 1103 backend tests on SQLite and MySQL, build OK, migration `finance 0012`.

**Transitional rules:**
- Until phase 5 ships, discounts and price changes work exactly as today; approvals switch on with phase 5/6.
- Until phase 7 ships, stock stays per branch row as today.
- No phase leaves a half-finished feature visible to users.

## C2. Testing and acceptance

- **Every confirmed rule** (C4) gets an automated test. Examples:
  - the value-card split;
  - GST inside the MRP;
  - same-day cancel and perk-used block;
  - FEFO and never-negative stock;
  - the 6-hour window;
  - approval enforcement;
  - promotion scope;
  - server-only status.
- **RBAC matrix test:** the A3 probe becomes permanent. Every endpoint, action and socket must deny without the permission and allow with it.
- **B10 checklist** re-run every phase, partly automated (API smoke tests) and partly in the browser panel with you.
- **Existing suite** (888 tests) kept passing; the 24 out-of-date tests rewritten to your rules.
- **MySQL:** the whole suite runs on MySQL from phase 0. Concurrency tests (row locks) are only meaningful there.
- **SAP:** simulated-SAP tests for every document and failure path, then real postings to the **test** company, each checked: exactly once, correct amounts and taxes.
- **Data:** migration rehearsal with counts and money totals; catalogue id mapping verified against every invoice line.
- **Load:** 400-user scenario; SAP lag must never reach the till.

## C3. Risks

| Risk | Mitigation |
|---|---|
| Value-card and inter-state documents in SAP | SAP team signs off before phases 6/7 post (D9, D10) |
| Catalogue consolidation breaks links from old bills | Old row ids kept as branch rows; full link check in the rehearsal |
| Removing the SAP auto-patching surfaces SAP errors that were hidden | Errors land in the monitor with the SAP text; the SAP team fixes master data once |
| WhatsApp onboarding (Meta verification, templates) | Built with empty credentials; email and the bell work meanwhile |
| Microsoft 365 mail authentication | Graph app registration by IT; SMTP fallback |
| Client clean-up removes a real person | None of the blank records is on a bill [D]; archive, not delete; reversible |
| Batch stock vs SAP at the start | Opening balances from SAP; "unbatched" bucket; nightly comparison |
| Large scope | Phased; each phase usable and approved on its own |

## C4. Decision register

All decisions are recorded in full in `UNDERSTANDING_LOG.md`:

- **Round 1:** #1–#87.
- **Round 2:** F1–F22.
- **Round 3:** D1–D15 and D4b. D1 services only; D3 test company; D4/D4b no other discount with a value card; D5 WhatsApp credentials empty; the rest approved as recommended.

Parked for later: carry-over (#13), credit limit and closing SMS (#8), Paytm (F15), operations #88–#91 (needed before phase 12).

## C5. Open questions for you (answer before the phase shown)

| # | Question | My recommendation | Before |
|---|---|---|---|
| Q1 | ~~Archive the 34,334 phone-less clients?~~ **Answered: keep them; they are B2B. Add GSTIN entry in Billing for B2B and e-invoicing.** | — | — |
| Q2 | Products have **no MRP**. Which SAP price list is the selling MRP? **Owner doesn't know.** | I read the SAP price lists **read-only** and show which one matches today's selling prices; the SAP team confirms | Phase 3 |
| Q4b | **E-invoicing:** you want it for B2B. It needs an IRP/GSP account (credentials entered later in Integrations, like WhatsApp). | Build it with empty credentials; test once your CA provides access | Phase 6 |
| Q3 | Memberships usable **only at the selling branch**: correct reading of #36? | Yes, as written in #36 | Phase 6 |
| Q4 | **E-invoicing (IRN/QR)** for B2B bills is mandatory above ₹5 crore turnover. Needed? | Ask your CA; build it if yes | Phase 6 |
| Q5 | Does a same-day cancellation need its own **credit-note number series**? | Ask your CA; the series is a setting either way | Phase 3 |
| Q6 | GST rates: services are all 5% today; memberships and cards 5% per template; packages? | CA confirms once; stored per item | Phase 6 |
| Q7 | **Stock master:** when POS and SAP disagree, which wins? (Today a POS audit overwrites SAP.) | SAP is the book of record; the POS audit posts an adjustment document reviewed in SAP | Phase 7 |
| Q8 | Do branches share **one login across several tills**? (Each login logs out the others.) | Personal logins per cashier; tokens per device | Phase 12 |
| Q9 | **Aadhaar** numbers for staff: keep masked, or stop storing them? | Store only the last 4 digits | Phase 9 |
| Q10 | Payroll statutory items (PF, ESI, professional tax, TDS): now or later? | Later; adjustments cover it meanwhile | Phase 9 |
| Q11 | ~~Starter roles?~~ **Answered: I create test roles with different module/submodule access and prove each sees only what it was given** (screens, tabs, buttons and API) | — | Phase 1 |
| Q12 | Run `repair_invoice_math` on history after the dry-run report? | Yes, after you read the report | Phase 0 |
| Q13 | Keep the boot splash? | Show it only until the app is ready | Phase 12 |
| Q14 | IP geolocation in Logs uses an outside service. Keep it? | Off by default; a setting | Phase 1 |
| Q15 | Django admin `/admin/` in production? | Disabled, or superuser behind the proxy allowlist | Phase 12 |
| Q16 | **Cancelling a paid bill in SAP.** SAP will not base a credit memo on a paid (closed) invoice, so today's credit memo is unlinked and leaves a customer credit open. Choose: (A) cancel the bill's incoming payment, then a credit memo linked to the invoice; or (B) cancel the payment, then SAP's own "Cancel" on the invoice | (A); SAP team confirms. The cash going back to the client is then the cancelled payment, not a separate outgoing payment | Phase 3 (same-day cancel) |
| Q17 | Account **1160200001** is "Academy Debtors" in SAP but is the control account for every new B2B client partner. Right account? | SAP team names the account; set it in Settings › SAP mapping | Before go-live |
| Q18 | Account **1110100007** is "Bank A/C" in SAP; the code called it "Paytm Virtual A/C Clearing" and posts UPI, cards and card/membership sales there. Right account(s)? | SAP team names them; Settings › SAP mapping (per payment method in Phase 6) | Before go-live |
| Q19 | Release the **6 held value-card sale journals** (bills 8, 9, 13, 15, 16, 37; ₹21,000 each, none in SAP yet)? | SAP team checks, then Retry in Admin › SAP Sync | Now |
| Q20 | Turn on **SAP TLS verification**: the Service Layer is plain `http://` on port 50001, so the SAP password crosses the network unencrypted | IT puts the Service Layer behind https with a trusted certificate; then `SAP_VERIFY_TLS=1` | Before go-live |

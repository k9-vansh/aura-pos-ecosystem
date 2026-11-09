# Aura POS: Architecture and Modification Plan

> **Superseded on 2026-10-11 by `POS_MASTER_BLUEPRINT.md`**, which merges this plan with the full structure review, the roles and permissions audit, and the settings catalogue. This file is kept for history.

**Status:** DRAFT for owner review (2026-10-11). **No code has been changed.** Implementation starts only after you approve this plan, phase by phase.

**Inputs:**
- `POS_ARCHITECTURE_AND_BUSINESS_LOGIC.md`: what the code does today.
- `UNDERSTANDING_LOG.md`: your answers to rounds 1 and 2.

**Reading guide:**
- Section 1 lists the few decisions still open; please answer those first.
- Sections 2–9 describe the target system.
- Section 10 is the step-by-step implementation plan.
- Section 11 covers testing and section 12 covers risks.
- In the module sections, a **Change / Why / How** table explains every modification and how it fits the existing system.

---

## 1. Decisions to confirm before implementation

Each decision has my recommendation. Reply with "agree" or your choice.

**Answered on 2026-10-11:**
- **D1:** services only.
- **D3:** the SAP company is a test copy, so testing there is OK.
- **D4:** no coupon at all on a bill that uses a value card.
- **D5:** build it with the credentials left empty; the owner adds them later.
- **D4b:** a bill that uses a value card gets no coupon and no membership discount.
- **All other decisions:** approved as recommended. **Plan approved; Phase 0 started 2026-10-11.**

**Architecture adjustment:** Redis was not installed locally, so background jobs (SAP, notifications, schedules) run on a **database-backed queue**. Worker processes claim outbox rows with `SELECT … FOR UPDATE SKIP LOCKED` on MySQL 8, in place of Celery. This removes a moving part and suits this volume. Redis stays optional in production for caching and websockets.

| # | Decision | Recommendation |
|---|---|---|
| **D1** | You said coupons and memberships apply to **services only** (#30), but also that a Trigger deal applies to the **whole bill** incl. products (F16). | All promotion types (Discount, FlatPrice, Trigger) and memberships discount **services only**. Products are always billed at MRP. Cashback (F9) and advance (#34) still work for products. Discounts are always computed on the **pre-tax** value, which is the standard and legally clean practice. |
| **D2** | Warehouse → branch transfers (F17): is the warehouse a POS location? | Add a **location type** to Centres: *Branch* (can bill) or *Warehouse* (stock only, no billing). The warehouse then appears in transfers and stock screens with no new module. |
| **D3** | Testing in SAP: all branches post to the **same** SAP company (F1), so my test postings would sit next to real branch postings. | Ask your SAP team for a **copy of the company for testing**. If that's not possible, I prefix every test document's remarks with `POS-TEST` and post only after you say go each time. |
| **D4** | Value card covering only part of a bill: which services does it cover first? Can a coupon or membership also apply to card-covered services? | The card covers services **in cart order**, and the cashier can tick or untick lines; a line is split if the balance runs out mid-line. **No coupon or membership discount on card-covered lines**, because the card benefit already is a discount. |
| **D5** | WhatsApp automation (#58, F14) | Automated and approval messages need the **WhatsApp Business Platform (Cloud API)**. The normal WhatsApp Business *app* cannot be automated. Each message type needs a template approved by Meta. Approve/Reject buttons and links also need a **public HTTPS address** for the POS. |
| **D6** | Outlook / Microsoft 365 mailbox (F14) | The panel supports **Microsoft 365 (Graph API, app registration)**, the robust option, and plain **SMTP**. Microsoft is phasing out password-based SMTP, so your IT person will likely need to create the app registration. |
| **D7** | The SAP login on every POS sign-in (old question 86) slows sign-in and its result is never used. | **Remove it.** SAP sessions are handled by the background workers instead. |
| **D8** | Payroll (#74, "don't know much") | Net pay = base salary + item commissions + incentive-rule payouts + manual earnings − manual deductions (salary advance, staff perks paid in "Money", other). There's no attendance module now; it can be added later. |
| **D9** | Petty cash (#80) and cashback/value-card **expiry**: should they post to SAP? | Ask your SAP team which account entries they want. I build the posting the way they specify, like the existing JEs. |
| **D10** | **Inter-state** stock transfers (F17): which SAP document carries IGST? | SAP team to specify. In the POS, the transfer document computes IGST when the two locations' GSTINs are in different states, and CGST + SGST or no tax within the same GSTIN. |
| **D11** | Approving from a WhatsApp or email link without logging in (F12) | Allowed, with safeguards: the link is **single-use**, **expires in 15 minutes** (configurable), is bound to the approver, logged with IP and channel, and shows the full request before the Approve button. |
| **D12** | Invoice number format (F20: no slashes, ≤ 16 characters for GST) | **`DL01-2627-00045`**: branch code (≤ 4 characters, set per branch), financial year, then a 5-digit running number, restarting per branch each financial year. Existing invoices keep their old numbers. |
| **D13** | Deleting a draft (#42) | Remove it from every screen but keep a hidden record with who deleted it and when, as an audit trail. Drafts never affect stock, SAP or numbering, so this is invisible to users. |
| **D14** | Appointment statuses (#24, "recommend some") | Add **Checked-in** and **No-show** to Scheduled, Completed and Cancelled. Both are optional to use. |
| **D15** | Existing clients with the **same phone** (clients used to be unique per branch) | Before switching to global unique phones, I produce a **duplicate report**. You choose per pair whether to **merge**, which moves history, perks and wallets to one client. The unique rule is switched on once the list is clean. |

---

## 2. Principles and constraints (from your brief)

1. **UI stays exactly as it is:** layout, styling and identity. Work there is limited to smoother, faster, more polished behaviour with subtle transitions. Any new screen (Settings, Approvals and similar) reuses the existing components and styles.
2. **No hard-coding:** limits, windows, thresholds, hours, formats, templates and integrations live in **Admin → Settings**.
3. **Fully role-based:** the code-created **superuser** is the only built-in identity. Everything else comes from Roles & Permissions.
4. **The server is the authority** for prices, taxes, discounts, approvals and stock. The till only displays what the server computes.
5. **No silent business changes:** everything here traces to your answers (section 13). Anything new is listed for approval.
6. **Compatibility:** existing data and relationships are kept, and the SAP JE treatments defined by the SAP team stay. API changes are listed in section 7.
7. **Incremental delivery:** phase by phase, each behind tests, each approved by you before the next.

**Kept exactly as today:**
- Billing flow and screens.
- Multi-staff contribution split.
- Incentive rule engine (made role-dynamic).
- Real-money commission scaling.
- No commission on package redemption.
- Payroll lock behaviour.
- Branch scoping.
- B2C/B2B customer mapping to SAP.
- Post-once SAP ledger.
- Audit log.
- Chat.
- Dashboards.
- Reports, made exact.
- Role matrix screen, extended.

---

## 3. Target architecture

```
                         ┌──────────────────────────── nginx (HTTPS, static Angular build) ─────────────────────────┐
 Browsers (≈400 users) ──┤                                                                                          │
 WhatsApp / email links ─┤──► Django API (gunicorn + uvicorn workers, ASGI)  ◄──► Redis (cache, websockets, queue)  │
                         │        │  REST + WebSocket (live approvals, notifications, dashboard)                      │
                         │        ▼                                                                                   │
                         │     MySQL 8 (InnoDB, utf8mb4)  ◄── transactional OUTBOX rows written with each business txn│
                         │        ▲                                                                                   │
                         │   Celery workers ──► SAP B1 Service Layer / HANA   (sap queue, limited concurrency)        │
                         │   Celery workers ──► Email (M365/SMTP), WhatsApp Cloud API (notify queue)                 │
                         │   Celery beat    ──► scheduled jobs (draft expiry at branch EOD, card/cashback expiry,    │
                         │                      reminders, transfer expiry, outbox sweeper, reports cache)           │
                         └──────────────────────────────────────────────────────────────────────────────────────────┘
```

| Component | Change | Why |
|---|---|---|
| **Django + DRF + Channels** | Kept. Refactored into a clear *service layer* per module (views stay thin). | No rewrite risk. Business rules end up in one testable place. |
| **MySQL 8** (replaces SQLite) | Real row locking (`SELECT … FOR UPDATE`, `SKIP LOCKED`), concurrent writers, backups. | SQLite cannot handle ~400 concurrent users. You chose MySQL (#4). |
| **Redis** | Cache, websocket channel layer, task broker. | Already required by settings when DEBUG is off. |
| **Celery workers + beat** (replaces the in-process SAP thread and DB-lease worker) | Separate processes for the SAP and notification queues, plus scheduled jobs. | SAP must **never slow the POS** (#84–87). Work runs outside the web workers, scales by adding workers, and survives restarts. |
| **Transactional outbox** (evolves today's `SapSyncQueue` + `SapPosting`) | Each bill, stock movement and so on writes its outbox rows **in the same DB transaction**. Workers deliver them in dependency order. The existing post-once keys stay. | Nothing is lost if Redis or SAP is down; nothing is posted twice; no partial states. |
| **Angular** | Kept; UI unchanged. | Your constraint #1. |

**Real-time SAP at scale:**
- The bill commits in milliseconds.
- The outbox row is then picked up by an SAP worker within about a second.
- Workers reuse SAP sessions and cap parallel calls so SAP isn't overloaded.
- Dependent documents wait their turn. Example: the business partner posts before the invoice, the invoice before the incoming payment, and the item before the goods receipt.
- Failures retry with back-off and appear on an **SAP Sync monitor** page. An alert goes to the users chosen in Notifications.

---

## 4. Cross-cutting new modules

### 4.1 Admin → Settings (no hard-coding)

Typed settings with scope **Global** or **per Branch**: a branch value overrides the global one. Every change is audited (who, when, old → new) and cached. Secrets are stored **encrypted** and are never shown again after saving.

| Group | Settings (initial values = today's behaviour or your answers) |
|---|---|
| Billing | Rounding (nearest ₹1, with a "Round off" line); draft expiry time (branch end of day); cancel window (same day); payment-method change window (6 h); allowed payment methods per branch; invoice series (branch code, format); receipt size (Auto / 80 mm / 58 mm / A4); cash change shown (on) |
| Discounts & approvals | Per role and branch: max line discount % and max bill discount % **without approval**; whether price change needs approval; approval link expiry |
| GST | GST on/off per branch (#10); state code (from the GSTIN) for IGST decisions; HSN/SAC shown on the receipt |
| Wallets | Advance and cashback usable at all branches (on, #36); value-card and package use limited to the selling branch (on); expiry reminder lead time (30 days) |
| Appointments | Opening and closing hours per branch and weekday (#20); slot size; default duration |
| Inventory | FEFO auto-batch (on); over-receipt (off, #62); negative stock (always off, #64: a hard rule, not a setting) |
| Notifications | Lead times and quiet hours |
| Integrations | SAP connection, WhatsApp Cloud API, email (M365/SMTP), Paytm (later). Each has a "Test connection" button. |

### 4.2 Roles & Permissions: fully dynamic (#2, #6)

| Change | Why | How |
|---|---|---|
| Remove every hard-coded "owner" check (payroll generate/lock/unlock/paid, closing overwrite, staff PIN, and others) | #6 | Each becomes a named **action permission** in the permission tree, for example `staff.payrolls.lock` and `finance.pettycash.amend_closing`. A data migration gives these to the existing "owner" role, so nobody loses access on day one. |
| Superuser is the only built-in full-access identity | #6 | `is_superuser` bypass kept. The role-name check is removed. |
| New permission entries | New modules | Settings, Approvals, Notifications, Integrations, SAP Sync monitor, Locations, Shifts, Petty cash categories, Payroll adjustments |
| "All branches" stays a role flag. A user's branches come from their assignment. | Existing behaviour | Unchanged |
| Users deactivated, never deleted (#7) | #7 | DELETE on users disabled. Deactivation blocks sign-in and keeps all records. |

### 4.3 Approvals: new (#25, #26, #63, F10–F13)

Approvals cover **price change, discount, stock request and stock transfer**.

**Configuration (Admin → Approvals):**
- **Policies:** action type, scope (branches), conditions (for example "line discount above the role's free limit"), approvers (one or more users).
- Mapping is many-to-many, so one approver can cover several branches and a branch can have several approvers (F13).

**At the till:**
- A cashier changes a price or gives a discount within their free limit (Settings): no approval needed.
- Above the limit: **Request approval**. The bill shows a waiting state, and the cashier can still edit other lines.
- Approvers get the request **in the POS** (live notification), **by email** and **on WhatsApp**. Each message carries Approve/Reject (D11).
- The first approver to decide wins. The decision reaches the till instantly over the websocket.

**Server enforcement:**
- When the bill is saved, every discount or price change above the free limit must reference an **approved request whose values are equal or more favourable to the business**. Otherwise the server rejects the bill.
- The till cannot bypass this.

**Records:** each request stores who, what (a snapshot of the lines), the amounts, the decision, the decider, the channel, and the time. It is reportable.

**Stock:**
1. A branch raises a request.
2. Its approvers are notified.
3. The approver creates and dispatches the transfer.
4. On receipt, the transfer is completed and the request becomes **Fulfilled** (#63, #65).

### 4.4 Notifications and integrations: new (#58, #69, F14)

- **Rules (Admin → Notifications):** each event type is sent to recipients (specific users, a role, the branch's approvers, or the client) over chosen channels (POS, email, WhatsApp). Message templates are editable, with placeholders.
- **Events:**
  - approval requested or decided;
  - stock request raised;
  - transfer dispatched or received;
  - low stock;
  - SAP posting failed;
  - closing variance above a set limit;
  - payroll ready;
  - **client reminders**: value card, membership, package or cashback **expiring in N days** (default 30, #58); appointment reminder (optional).
- **POS notification centre:** a bell in the existing header style, with unread count, list and click-through.
- **Delivery log:** every message is stored with its status (queued, sent, delivered, failed), with retries. Clients with DND status get no promotional messages.
- **Integrations panel:** WhatsApp Cloud API (phone number ID, token, template names, webhook verify token), email (M365 or SMTP), each with **Test**. Secrets are encrypted.

### 4.5 SAP pipeline: hardened (#84–87)

| Change | Why | How |
|---|---|---|
| Outbox plus Celery SAP workers replace the thread and DB-lease worker | Speed and reliability at 400 users | §3. The existing `SapPosting` keys (`invoice:<id>`, `inpay:…`, `grpo:…` and so on) are kept, so no document is posted twice. |
| Dependency ordering | Invoice before payment, item before goods receipt | Each outbox row lists what it depends on, and the worker waits for those. |
| Session reuse and a concurrency cap | Protect SAP; faster | Each worker keeps a Service Layer session and re-logs in when it expires. |
| SAP Sync monitor (Admin) | Visibility | Queue, failures with the SAP error text, per-document retry. Failure alerts through Notifications. |
| Remove the sign-in SAP login | D7 | Sign-in no longer waits on SAP |
| New documents | New features | Value-card split invoice (§5.4), payment-method change (cancel and re-post the incoming payment), expiry entries (D9), inter-state transfers (D10). Each is built to the SAP team's specification. |
| Credit memo dated on the cancellation date | Correctness | Cancellation is same-day only (F19), so this equals the invoice date in practice |

### 4.6 Audit and observability

- The audit log is kept and moved onto the job queue.
- Structured logs, a health-check endpoint, and optional error tracking.
- Settings, approvals and permission changes are audited.

---

## 5. Module plans

### 5.1 Clients (#11–15, F2–F4)

| Change | Why | How |
|---|---|---|
| **Global clients**, usable at every branch and searchable by name or phone everywhere | #11 | Remove branch filtering from client search and lists. `center` becomes "registered at" (kept for reports). |
| **Phone unique across the system**, with the message "This number is already used by \<name\>" | F2 | Normalise the phone (digits only, country prefix), then a unique DB constraint after the duplicate clean-up (D15) |
| Name and phone mandatory | F3 | Server-side validation. The "anonymous client" option is removed from the till. |
| Bulk upload uses the same rules | #14 | Each row is validated through the same service as manual entry, with a row-by-row result (created / duplicate / error). Clients without a GSTIN map to B2C as today. |
| Blacklist by permission | #12 | `admin.clients.update` (or a dedicated `blacklist` action). Blocks billing and appointments (kept). |
| Remove the client mobile app | #3 | Remove the endpoints and the PIN field from forms. The column stays until a later clean-up. |
| Carry-over | #13 | **Parked:** untouched. |

### 5.2 Services (#16–19)

- Central price, branch override and active flag stay as they are, controlled by the Services permission.
- GST is per service (#17).
- **Consumable shortage blocks billing** of that service and lists the missing items with required vs available quantities (#18). The check becomes batch-aware (§5.6).
- The BOM keeps coming from SAP (#19). The manual pull becomes a scheduled sync with a visible "last synced" time.

### 5.3 Appointments (#20–24)

| Change | Why | How |
|---|---|---|
| Calendar hours per branch | #20 | Settings → Appointments. The timeline uses the branch's hours instead of the fixed 9:00–22:00 (same look). |
| Unallocated services allowed. Staff never double-booked, checked on **create, update, staff assignment and restore**. | #21, #22 | The clash check runs on every change that touches staff or time, including status → Scheduled. |
| A cancelled service bill sets its appointment to **Cancelled** | #23 | Part of the cancel workflow (§5.4) |
| Blacklist and branch checks also on update | Consistency | Same validator as create |
| Statuses Checked-in and No-show | D14 | Optional buttons in the existing modal |

### 5.4 Billing (#25–52, F5–F9, F16, F19–F22)

**One pricing engine.**
- The server's pricing service becomes the single source of truth.
- A new **quote** endpoint returns the fully priced cart: lines, discounts, value-card split, taxes, rounding, approvals needed.
- The till shows the server quote. The existing `priceCart` keeps giving instant feedback while the quote loads, so the screen stays fast and looks the same.

| Area | Rule (confirmed) | How |
|---|---|---|
| **Products** | MRP is GST-inclusive. Show taxable value + GST inside the MRP (#27, F22). | Line tax = MRP × r ⁄ (100 + r), taxable = MRP − tax. The customer still pays exactly the MRP. Example: ₹590 at 18% = ₹500 + ₹90. |
| **Price change and discounts** | Allowed with approval above the free limit (#25, #26) | §4.3. The approved price is final and goes to SAP. |
| **Coupons** | Server computes them; **no double discount** (#28). Specific-service and minimum-bill rules come from the campaign. Services only (D1). One coupon or one membership per bill (#29). | The till sends `promo_id` only; the server applies Discount (% or ₹), FlatPrice (flat ₹ off), Trigger (specific services present → discount), checks validity, branch, members-only and per-client limit (kept), and returns the discount. |
| **Memberships** | Several held; the client picks one per bill (#31). % discount on **services only** (#30, #57). | The till lists active memberships; the selected one is validated and applied by the server. |
| **Packages** | Usable only at the branch where bought; expired not usable (#32). Multi-service templates with GST (#55, #56). | Package redemption checks the branch and expiry. See §5.5 for templates. |
| **Value card** | F5–F7 model (worked example below). Services only. Forfeited at expiry. | The card stores its **face balance** and its **ratio** = pre-tax price ÷ face value, fixed at sale. The covered services are shown as one block on the bill and receipt. The old "value-card discount" button and the scaled/unscaled debit-refund mismatch are **removed**. |
| **Advance** | No GST on receipt; normal GST when spent; a payment method for services and products; any branch (#34, #36, F8). | The advance line is forced to 0% tax on the server. Spending is a payment row (kept). |
| **Cashback** | Expires per the campaign's validity rules (#35); any branch; services and products (F9). | Each credit carries an expiry date and is consumed oldest first. A nightly job expires the remainder, and the client is reminded beforehand. |
| **Payments** | Split tender, full payment required (#37). Online amounts exact; cash can be over-tendered, with change shown (#38). **Tips removed** (#39); **Nearbuy removed** (#40); Paytm later (F15). | A "Cash received" field shows the change, and only the bill amount is recorded as paid. Method list per branch comes from Settings. Historical tip and Nearbuy data stays readable in reports. |
| **Change payment method** | Within **6 hours** (setting), by permission (#41) | Window checked on the server. SAP incoming payment is cancelled and re-posted. Audited. |
| **Drafts** | Deletable; expire at branch end of day; no stock effect (#42); one mechanism only (#43). | Server drafts only. Browser "held carts" removed. Delete works (D13). A scheduled job expires drafts at the configured time. |
| **Finalised bills** | Items not editable; cancel and re-bill (#44) | Kept |
| **Cancel** | **No refunds; cancel only** (#45, #46). **Same day only** (F19). Blocked if any perk sold on the bill has been used at all (#47). Appointment → Cancelled (#23). | Refund endpoint, button and status removed for new bills; old "refunded" bills stay readable. The money returned on a same-day cancel is still recorded so cash reports stay exact. The SAP credit memo is kept. |
| **Invoice number** | Professional, no slashes, ≤ 16 characters (F20, D12) | Per-branch, per-financial-year series, e.g. `DL01-2627-00045`. Assigned only at finalisation. |
| **Rounding** | Nearest ₹1 with a "Round off" line (#50) | Standard practice in Indian retail. Kept. |
| **Receipt** | Real receipt size, works on any printer (#51, F21) | A print layout that adapts to 80 mm, 58 mm or A4 (setting "Auto"). It carries the GST-required fields: GSTIN, invoice number and date, HSN/SAC, taxable value, CGST/SGST (or "Bill of Supply" when GST is off), and round-off. The screen UI is unchanged. |
| **Staff split** | Several staff per line with a contribution %, which drives incentives (#49) | Kept. The 100% total is now also enforced on the server. |
| **Stock** | Nothing sold without stock (#52). Consumables block too (#18). | Checked under row locks at finalisation. Batches are taken earliest-expiry first (§5.6). |
| **"Manager"** | The **user who creates the bill** (#83) | New `created_by` user on the invoice. The Manager Discounts report uses it. |
| **GST on/off per branch** | #10 | Branch GST off: no tax lines, and the document is titled "Bill of Supply" (setting). |

**Value card worked example** (your F5 numbers).

The client bought the card for ₹20,000 + 5% GST = ₹21,000, with a face value of ₹30,000, so the ratio is 2⁄3. They now take Service A worth ₹30,000 and Service B worth ₹15,000, both normally 18% GST.

```
COVERED BY VALUE CARD  VC-000123  (balance ₹30,000 → ₹0)
  Service A                                   30,000.00
  Value-card benefit                         −10,000.00
  Taxable value                               20,000.00
  CGST 2.5% 500.00   SGST 2.5% 500.00          1,000.00
  Adjusted from card (prepaid)                21,000.00

PAYABLE NOW
  Service B                                   15,000.00
  CGST 9% 1,350.00   SGST 9% 1,350.00          2,700.00
  Amount payable                              17,700.00   ← paid by cash/UPI/…

Invoice total 38,700.00 = card 21,000.00 + paid 17,700.00
```

- **Partial use:** a ₹6,000 service gives benefit −₹2,000, taxable ₹4,000, GST ₹200, ₹4,200 adjusted, and the face balance becomes ₹24,000.
- **Cancelling** a bill that used the card (same day) restores the face value used.
- **Cancelling the bill that sold the card** is allowed only if the card is unused (#47).
- The SAP documents for this split follow the SAP team's JE design (D9).

### 5.5 Marketing (#53–58)

| Change | Why | How |
|---|---|---|
| Promotion types enforced on the server: Discount (% or ₹), FlatPrice (flat ₹ off), Trigger (specific services present → discount), Cashback (with expiry) | #28, #53, F16 | One promotion engine (§5.4) used by both the quote and the save, so the screen and the bill always match |
| Packages: **multiple services with quantities**, **GST % field**, validity; the same structure in Marketing and Billing | #55, #56 | Package template editor in the existing Marketing form style. The till's custom-package builder uses the same structure. |
| Membership: % discount on services | #57 | Kept, with the scope made explicit |
| Coupon allowance returned when a bill is cancelled | #54 | Kept (refunds no longer exist) |
| **Automated WhatsApp/email reminders** for expiry (card, membership, package, cashback) and optional campaigns | #58 | §4.4. Uses the existing DND flag. The mock campaign sender is replaced by the real provider. |

### 5.6 Inventory (#59–68, F17, F18)

| Change | Why | How |
|---|---|---|
| **Location types:** Branch and **Warehouse** | F17, D2 | A field on Centre. Warehouses hold stock and transfer, but never bill. |
| **Batch-level stock (FEFO)** | #68, F18 | New stock-by-batch table (product, location, batch, expiry, quantity). Sales, consumables, checkout and transfers **automatically take the earliest-expiring batch**. The product's total stock remains the sum of its batches. Existing stock without a batch becomes an "unbatched" bucket. |
| **Never negative** | #64 | Enforced in the service layer under row locks, plus a DB check on batch quantities |
| Over-receipt blocked | #62 | Goods receipt quantity ≤ ordered − already received |
| **Request → approval → dispatch → receive** | #63, #65, F10, F13 | 1. A branch raises a request. 2. Its approvers are notified (POS, email, WhatsApp). 3. The approver creates and **dispatches** the transfer: stock leaves the source, batches are chosen FEFO, status In Transit. 4. On **receipt**, stock arrives and the request becomes Fulfilled. |
| **Inter-state tax** on transfers | F17, D10 | Different GST states: transfer document with **IGST**. Same state, different GSTIN: CGST + SGST. Same GSTIN: no tax (delivery challan). The value basis is cost. The SAP document follows the SAP team. |
| Products: "All locations" = every branch and SAP; a single branch = that branch only; branch price can differ; PO products sync to that branch | #59 | Kept. MRP rule kept (#60). |
| PO steps by permission | #61 | Kept (forward-only status) |
| Internal checkout for usage without a bill | #66 | Kept, now batch-aware |
| Audit by permission | #67 | Kept, with row locks added |
| Low-stock alerts to the chosen users | #69 | Notification rule |
| Fix: the Stock Requests tab crashes for non-owner users | Defect | The branch filter is corrected |

### 5.7 Staff and payroll (#70–76)

| Change | Why | How |
|---|---|---|
| Remove the staff mobile app | #3 | Remove the endpoints and the PIN |
| Incentive rules target **designations** (dynamic), not fixed role groups | #81 | Rule form lists the designations. Existing rules are migrated. |
| Item commission: % **or** fixed amount per service/product | #70 | Kept and verified |
| Temporary transfer: revenue goes to the branch where the bill is made; incentive and commission stay with the staff member | #73 | Already true in the data. Reports labelled accordingly. |
| Staff perks: points added by admins, no reset | #72 | Kept. Product use links to the product and writes a stock movement. (The "perks report crash" claimed earlier was withdrawn: a probe showed it works.) |
| Payroll per D8, with **Generate** and **Unlock** buttons | #74, #75 | Payroll screen in the existing style. Adjustment lines record reasons. Lock still blocks that month's bill edits (#76). |

### 5.8 Finance and day-end (#77–83)

| Change | Why | How |
|---|---|---|
| **Shifts:** several per day per till or cashier, with opening float, cash in/out, mid-shift **X-report** and closing **Z-report** | #78 | Shift model extended. The register screen keeps its look. |
| **Daily closing computed by the server.** Expected cash = opening + cash sales − change given − cash returned on same-day cancels − petty-cash payouts + cash in − bank deposit. Card, UPI and other totals **auto-filled from payments**. The cashier enters only the **counted cash**. The server stores system cash, difference and closing balance. | #77, #79 | The client no longer supplies totals. An amendment needs a permission and keeps the old values in the audit log (kept). |
| **Petty cash, enterprise-grade:** configurable categories, branch petty-cash float (imprest) with top-ups, receipt photo attachment, running balance, linked to the shift and closing, optional SAP posting (D9) | #80 | New screens in the existing Finance style |
| **Exact reports:** one shared definition per figure (gross, discount, taxable, tax, net, collected, liability draw-downs, cancellations), used by every report and dashboard. A nightly **reconciliation check** (POS totals vs outbox vs SAP) flags any gap. | #82 | Report service plus reconciliation job and report |
| Manager discounts report keyed on the biller | #83 | Uses `created_by` |
| Fixes: "Duplicate rule to branch" ignores the branch; user names missing on petty cash and shift lists | Defects | As listed in the architecture document §13 |

### 5.9 Dashboard, logs, chat

- Kept as they are.
- Dashboard numbers come from the shared report definitions (§5.8), so they match the reports.
- The Logs page stops calling the third-party IP lookup from the browser; the server's cached geo lookup is used instead.

---

## 6. Data model changes

| Model | New / changed | Notes |
|---|---|---|
| `AppSetting` | **new** | key, scope (global/branch), value (typed JSON), encrypted flag, updated_by/at |
| `ApprovalPolicy`, `ApprovalPolicyApprover`, `ApprovalRequest`, `ApprovalToken` | **new** | §4.3; single-use, hashed, expiring tokens |
| `NotificationRule`, `NotificationTemplate`, `Notification`, `DeliveryLog` | **new** | §4.4 |
| `IntegrationConfig` | **new** | encrypted credentials per integration |
| `SapOutbox` (evolves `SapSyncQueue`) + existing `SapPosting` | changed | dependencies, priority, branch, status, attempts, next-attempt time, error |
| `Center` | changed | `location_type` (Branch/Warehouse), `branch_code` (≤ 4), `state_code`, opening hours (in Settings) |
| `Client` | changed | normalised phone, globally unique (after D15); PIN fields retired |
| `Invoice` | changed | `created_by` user; new number series; `refunded` status retired for new bills |
| `InvoiceItem` | changed | value-card split fields (covered, benefit, card id, card tax rate); inclusive-tax fields for products |
| `ClientValueCard` | changed | face balance, ratio and card tax rate fixed at sale, expiry |
| `CashbackTransaction` | changed | expires_on, consumed tracking |
| `Package` / `ClientPackage` | changed | multiple services with quantities, `gst_percent`, branch restriction |
| `StockBatch` | **new** | product, location, batch, expiry, quantity; FEFO |
| `StockTransfer` | changed | Dispatched / In transit / Received; tax type and amounts; linked request |
| `Shift`, `DailyClosing`, `PettyCashEntry`, `PettyCashCategory` | changed / **new** | server-computed fields, multiple shifts, categories, attachments |
| `PayrollRecord` + `PayrollAdjustment` | changed / **new** | D8 |
| `IncentiveRule` | changed | target designations instead of fixed groups |

**Migrations:**
- Every schema change ships as a Django migration. New fields default to today's behaviour.
- **Data moves to MySQL** by a scripted copy with row-count and money-total checks per table.
- **Duplicate clients** are cleaned up via D15 before the unique constraint is switched on.
- **Existing invoices are never renumbered.**

---

## 7. API changes (compatibility)

| Type | Endpoint / field | Reason |
|---|---|---|
| Added | `POST billing/invoices/quote/` | One pricing engine (§5.4) |
| Added | `approvals/…`, `notifications/…`, `settings/…`, `integrations/…`, `sap/outbox/…`, `inventory/batches/…`, `inventory/locations` (Centre type), finance shift X/Z, petty-cash categories, payroll generate/unlock UI | New modules |
| Changed | Invoice create/update: `promo_id`/`membership_id` drive the discount on the server; a manual discount or price change above the free limit needs `approval_id`; value-card use sent as card id + selected lines | Fixes the double discount (#28); enforces approvals (#25) |
| Changed | `DELETE billing/invoices/<id>/`: allowed **for drafts only** | #42 |
| Changed | Client create/update: phone must be globally unique (clear error message) | F2 |
| Removed | `billing/invoices/<id>/refund/`; tip fields; Nearbuy method; `clients/api/app/*`; `staff/api/app/*`; SAP login inside the sign-in response | #39, #40, #45, #3, D7 |
| Kept | Every other endpoint and response shape the frontend uses today | Compatibility |

---

## 8. Frontend plan (UI preserved)

**New screens:** Settings, Approvals, Notifications and Integrations, SAP Sync monitor, notification bell, approval-waiting state, cash received and change, value-card block, payroll buttons, warehouse/transfer steps, shifts X/Z, petty-cash categories. All are built with the **existing components, fonts, spacing and colours**, so they look like the screens around them.

**Smoothness and speed** (no visual redesign):
- Live updates over the websocket instead of polling.
- Debounced searches with cached master data (services, products, promotions per branch).
- Quote calls run in the background while the local estimate shows instantly.
- Skeleton loading for lists, and virtual scrolling for long lists.
- Short, subtle transitions on modals and drawers only.
- Optimistic updates where safe.
- Smaller bundles through lazy loading, which is already in place.

**Removed from the UI:** tips, Nearbuy, the Refund button, browser-held carts, the anonymous-client option, and the "apply value-card discount" button. Each is replaced by the confirmed flows above.

---

## 9. Non-functional design

**Performance and scale (~400 users):**
- Gunicorn/uvicorn workers sized to the CPUs.
- Persistent MySQL connections and indexes on every filter used by reports.
- Paginated lists everywhere, with no unbounded "return all rows" lists.
- Master data cached.
- SAP, email, WhatsApp and audit writes all run off the request path.
- A load test with Locust (a `locustfile.py` already exists) simulates 400 users before go-live.

**Reliability:**
- The transactional outbox.
- Idempotent checkout (kept).
- Post-once SAP keys (kept).
- Retries with back-off.
- Daily MySQL backups plus point-in-time recovery.
- Health checks.

**Security:**
- HTTPS only, `DEBUG` off, a fresh secret key.
- CORS limited to the POS address.
- Encrypted integration secrets.
- Rotate the weak SAP password, and move credentials out of the six root scripts (scripts archived, with your OK).
- Every rule enforced on the server (approvals, limits, windows, stock).
- Single-use approval tokens.
- Throttles on sign-in and approval links.
- A full audit trail.

**Maintainability:**
- A service layer per module, with no business rules in views or templates.
- Shared money, tax and report definitions.
- Typed settings.
- Tests for every confirmed rule.

---

## 10. Implementation plan (phases, dependencies, priorities)

Each phase ends with tests passing and **your sign-off** (a demo in the browser panel) before the next one starts. Sizes: **S** = days, **M** = about a week, **L** = more than a week.

| Phase | Content | Depends on | Size | Priority |
|---|---|---|---|---|
| **0. Safety net and platform** | (a) Snapshot/commit the current code (with your OK). (b) Local MySQL 8 + Redis; app running on MySQL with your data copied and checked. (c) Celery workers + beat. (d) Rewrite the 24 failing tests to match the confirmed rules. | none | M | Highest |
| **1. Settings + dynamic RBAC** | Admin → Settings (§4.1); remove owner checks → permissions (§4.2); users deactivate only | 0 | M | Highest |
| **2. SAP pipeline** | Outbox and workers, dependency order, session reuse, SAP Sync monitor, remove sign-in SAP login (§4.5) | 0 | M | Highest |
| **3. Quick correctness fixes** | Double discount; product GST inside MRP; drafts delete/expiry, held carts removed; tips, Nearbuy and refund removed; cancel same-day + perk-used block + appointment cancel; new invoice numbers; staff-split server check; stock-request crash; restore-appointment clash check; over-receipt and negative-stock guards; duplicate-rule and user-name fixes | 1 | M | High |
| **4. Clients global** | Duplicate report → your merge decisions (D15) → unique phone; global search; bulk upload rules; remove mobile apps | 1 | S–M | High |
| **5. Approvals + Notifications + Integrations** | §4.3, §4.4, notification bell, email (M365/SMTP), WhatsApp Cloud API, approve-by-link | 1, 2 | L | High |
| **6. Billing engine** | Quote endpoint, server promotion engine, membership choice, **value-card split** + receipt, advance tax 0, cashback expiry, split tender + cash change, payment-method change window, universal receipt | 3, 5 | L | High |
| **7. Inventory** | Warehouse location type, **batch stock (FEFO)**, request → approval → dispatch → receive, inter-state tax on transfers, batch-aware consumables | 5, 2 | L | Medium-high |
| **8. Appointments** | Hours per branch, clash rules everywhere, Checked-in/No-show | 1 | S | Medium |
| **9. Staff/payroll + Finance** | Payroll (D8) with Generate/Unlock, designation-based rules, multi-shift X/Z, server-computed closing, petty cash, exact reports + nightly reconciliation | 6, 7 | L | Medium-high |
| **10. Marketing** | Multi-service packages with GST, automated expiry reminders, promotion editor aligned with the engine | 5, 6 | M | Medium |
| **11. SAP end-to-end verification** | Each document type posted to the SAP test company (D3), checked against the SAP team's expectations; value-card, inter-state and expiry entries signed off by the SAP team | 2, 6, 7, 9 | M | High (before go-live) |
| **12. Performance, smoothness, security, go-live** | Frontend polish (§8), 400-user load test, security hardening (§9), production server setup, MySQL migration rehearsal, cut-over checklist | all | M | High (before go-live) |

**Critical path:** 0 → 1 → (2, 3, 4) → 5 → 6 → 7 → 9 → 11 → 12.

### Where the phases stand (updated 20 Oct 2026)

Taken from the git history on branch `pos-rebuild` and the QA run of 19-20 Oct. "Built" means committed with tests passing; the **owner's sign-off is not recorded** for any phase, so each stays "awaiting sign-off" until you say so.

| Phase | State | Evidence / note |
|---|---|---|
| 0. Safety net and platform | Built | commit `24499bb`; MySQL 8.4 platform, job runner; the suite now runs on MySQL |
| 1. Settings + dynamic RBAC | Built, then reworked | `24499bb`; the permission tree and the **Roles & Permissions screen were redesigned on 19-20 Oct**, with a test that the server and screen module lists match |
| 2. SAP pipeline | Built, reviewed again | `f29499c`; outbox, post-once ledger, SAP Sync monitor; 20 Oct hardening (wrong branch, duplicate check, missing item, pull errors) |
| 3. Quick correctness fixes | Built | `62680af`, `aaa766d` |
| 4. Clients global | Built | `5da6dc5` |
| 5. Approvals, notifications, integrations | Built | `2f8ef55`; approval rules for discount and price change are on by default since 19 Oct |
| 6. Billing engine | Built | `3e61154` |
| 7. Inventory | Built | `be58bb9`, `da79d96` |
| 8. Appointments | Built | `5da6dc5` |
| 9. Staff/payroll + Finance | Built, then corrected | `be58bb9`, `da79d96`; **incentive engine and Manage fixed on 19-20 Oct** (rules paying nothing after a save, per-day rules, tier read at the exact multiple, monthly/daily split, payroll breakdown) |
| 10. Marketing | Built | `2f8ef55` |
| **11. SAP end-to-end verification** | **Not done** | needs the SAP test company, one document type at a time, and the SAP team's answers to the five open questions (QA report, section 5, item 4) |
| **12. Performance, security, go-live** | **Partly** | security and permission audit done on 19-20 Oct; the 400k-bill speed test done (yearly all-centre reports still 4-10 s); server setup, load test, migration rehearsal and cut-over checklist **not done** (`backend/deploy/GO-LIVE.md`) |

**Work done outside the phases, 19-20 Oct (QA run):** every Critical, High, Medium and Low finding fixed; users need a role and a centre; three Finance report screens built; Admin > Bills totals from the server; centre switch shows at once; and a look pass (one surface, no clipped cards, no scroll bars, readable dark-theme cards). **None of this is committed yet.**

**What is left** is in section 5 of `Desktop/aftercirapos/QA-REPORT-2026-10-19.md`, which is now the working list; phases 11 and 12 above are its blockers A and the money checks B.

---

## 11. Testing strategy

- **Rule tests:** every confirmed rule in `UNDERSTANDING_LOG.md` gets an automated test. Examples:
  - the value-card split figures above;
  - product GST inside the MRP;
  - one coupon or one membership;
  - same-day cancel and the perk-used block;
  - FEFO picking and never-negative stock;
  - the 6-hour payment-method window;
  - approval enforcement on the server.
- **The existing suite** (888 tests) keeps passing. The 24 failing tests are rewritten to the confirmed rules, not deleted.
- **SAP:** automated tests against the simulated SAP for every document type and every failure path. Then **controlled real postings** to the SAP test company (D3), each verified in SAP: exactly once, correct amounts and taxes.
- **Frontend:** `ng test` plus the production build each phase. Each phase is demoed in the browser panel for your acceptance.
- **Data:** MySQL migration rehearsal with row counts and money totals compared table by table.
- **Load:** a Locust scenario of 400 users billing, searching and running reports, with response-time targets and no SAP lag on the till.
- **Reconciliation:** the nightly POS-vs-SAP check runs in test before go-live.

---

## 12. Risks and mitigations

| Risk | Mitigation |
|---|---|
| Test postings mixed with real branch postings in the single SAP company | D3: a separate test copy, or `POS-TEST`-tagged postings only with your go-ahead |
| Value-card and inter-state tax treatment | SAP team and accountant sign-off on the document design before phase 6/7 SAP work (D9, D10) |
| WhatsApp onboarding (business verification, template approval) takes time | Start the Meta setup early. Email and in-POS notifications work meanwhile. |
| Microsoft 365 mail authentication | Graph app registration (D6) by your IT person; SMTP as a fallback |
| Duplicate clients in existing data | D15 report and merge before the unique rule |
| Batch stock vs SAP batches out of step at first | Opening batch balances loaded from SAP; "unbatched" bucket for legacy stock; nightly comparison |
| Scope is large | Phased delivery, each phase usable and approved on its own; critical path first |

---

## 13. Traceability (your answers → plan)

| Answers | Where in this plan |
|---|---|
| 1, 2, 6, 7 | §4.2 |
| 3 | §5.1, §5.7 |
| 4 | §3, phase 0 |
| 5, F1 | §4.5, D3, phase 11 |
| 8, 13 | Parked (no change) |
| 9 | Kept (home/finance targets) |
| 10, 17, 27, F22 | §5.4 (GST) |
| 11, 12, 14, 15, F2–F4 | §5.1 |
| 16, 18, 19 | §5.2 |
| 20–24 | §5.3, D14 |
| 25, 26, F10–F13 | §4.3 |
| 28–31, 53, 54, F16 | §5.4, §5.5, D1 |
| 32, 55–57 | §5.4, §5.5 |
| 33, F5–F7 | §5.4 value card, D4 |
| 34–36, F8, F9 | §5.4 |
| 37–41, F15 | §5.4 |
| 42–47, F19 | §5.4, D13 |
| 48, F20 | §5.4, D12 |
| 49, 50, 51, 52, F21 | §5.4 |
| 58, 69, F14 | §4.4, D5, D6 |
| 59–68, F17, F18 | §5.6, D2, D10 |
| 70–76 | §5.7, D8 |
| 77–83 | §5.8, D9 |
| 84–87 | §3, §4.5, D7 |
| 88–91 | Deferred. Needed before phase 12 (sizing, offline, hosting, slow screens). |

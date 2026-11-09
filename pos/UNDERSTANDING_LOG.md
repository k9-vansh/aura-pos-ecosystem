# POS Understanding Log

This is the running record of business rules confirmed by the owner. It is the source for the architecture plan. Nothing here has been implemented yet.

Numbers refer to the question list of 2026-10-11, and "§" refers to `POS_ARCHITECTURE_AND_BUSINESS_LOGIC.md`.

## Round 1: confirmed (2026-10-11)

### Platform and access

| # | Topic | Decision |
|---|---|---|
| 1 | Branches | Unlimited; created from the POS. Each branch may have its own GSTIN. |
| 2, 6 | Access control | **Fully role-based, nothing hard-coded.** Roles are defined in Admin → Roles & Permissions (modules, submodules, actions) and assigned to users. Only the code-created **superuser** has full access. Every "owner" check in the code must become a permission. |
| 3 | Mobile apps | **Remove** the staff mobile app and client mobile app APIs. |
| 4 | Database | SQLite is only for local use. MySQL may be installed locally to verify every entry. |
| 5 | SAP | The credentials in the code point to the **testing SAP** company and may be used for full testing (see follow-up F1). |
| 7 | Users | Deactivate, never delete. All of their records are kept. |
| 8 | Credit limit, closing SMS | Keep the fields; decide later. |
| 9 | Targets | Set by a person each month. Show both the monthly target and the month-by-month history. |
| 10 | GST on/off per branch | Wanted. A branch with GST off bills without tax; with GST on, bills carry tax. Exact design to be proposed. |

### Clients, services, appointments

| # | Topic | Decision |
|---|---|---|
| 11 | Clients | **Global**: one client usable at any branch in any state. Searchable by name or number. Not tied to a branch. |
| 12 | Blacklist | Blocks billing and appointments. Done by whoever has the Admin → Clients permission. |
| 13 | Carry-over | Parked. |
| 14 | Client source | Clients are entered in the POS (one by one or bulk upload), not imported from SAP. |
| 15 | Walk-ins | First-time customers are normal and welcome (see F3). |
| 16 | Service prices | Changed by whoever has the Services permission (central price, branch override, active/inactive). |
| 17 | Service GST | Varies by service. |
| 18 | Consumables short | **Block** billing of that service and show which item is missing. |
| 19 | BOM | Maintained in SAP. |
| 20 | Calendar hours | **Configurable per branch.** |
| 21 | Unassigned services | Services can be booked without staff and wait in "Unallocated" until a staff member is free. Staff can never be double-booked. |
| 22 | Restoring an appointment | Must re-check for clashes. |
| 23 | Cancelled service bill | The linked appointment shows **Cancelled**. |
| 24 | Appointment statuses | Current ones are enough; recommendations welcome. |

### Billing

| # | Topic | Decision |
|---|---|---|
| 25, 26 | Price changes and discounts | The cashier may change a price or give a discount, but **only after approval**. The approved price is final and goes to SAP. **New: approval system.** Requests go to the designated approver in the POS, by email and by WhatsApp. Who approves what is configurable (billing discount/price, inventory stock transfer, and so on). |
| 27 | Product GST | Products are billed at MRP. **GST must be shown and extracted** (see F22). |
| 28 | Coupon discount | Fix the double discount. Specific-service and minimum-bill rules come from the campaign definition in Marketing and must be enforced. |
| 29 | Coupon + membership | One at a time. |
| 30 | What discounts apply to | Memberships and coupons apply to **services only, never products**. Advances can buy products. |
| 31 | Several memberships | A client may hold several. The client chooses which one applies on a bill. |
| 32 | Packages | Usable **only at the branch where bought**. Expired packages are not usable. |
| 33 | Value cards | Act like an advance (see the worked model in F5). The invoice must show what was covered by the card and what was paid separately, matching SAP. Validity is limited (for example 365 days). |
| 34 | Advance | Money deposited by a client, used later for services or products. No GST when received (see F8). |
| 35 | Cashback | **Expires** according to the promotion's validity rules. |
| 36 | Cross-branch use | **Only advance and cashback** work at any branch. |
| 37 | "Partial payment" | Means split payment (for example part cash, part UPI). Full payment is still required. |
| 38 | Overpayment | Online payments must be exact. Cash may be tendered higher and **change is shown**. |
| 39 | Tips | **Remove** tips from the POS. |
| 40 | Payment methods | Keep the list, **remove Nearbuy**. **Integrate a Paytm gateway** (see F15). |
| 41 | Changing a payment method | Allowed **within 6 hours** of the bill, by permission. |
| 42, 43 | Drafts | Drafts are deletable and **expire at end of day**. Stock is deducted only on finalisation. Drafts and held carts are the same thing, so keep one. |
| 44 | Finalised bills | Items cannot be edited; cancel and re-bill. |
| 45, 46 | Refunds | **No refunds. Cancel only.** Remove the refund action. |
| 47 | Used perks | A bill whose sold perk (card, membership, package) has been **used at all cannot be cancelled**. |
| 48 | Invoice number | Should look professional, to worldwide standard (see F20). |
| 49 | Staff split | Each line (service, product, membership…) can have several staff with a contribution ratio (for example 30/70). This drives incentives and is essential. |
| 50 | Rounding | Claude to decide using common practice. |
| 51 | Receipt | A real **receipt-size** invoice, not a full A4 page (see F21). |
| 52 | Stock | Nothing can be sold without stock. |

### Marketing

| # | Topic | Decision |
|---|---|---|
| 53 | Deal types | FlatPrice takes a flat ₹ amount off the bill; Trigger applies to specific services (see F16). |
| 54 | Coupon usage on cancel | Claude to decide. |
| 55 | GST on prepaid products | Set when the membership or card is created; packages should work the same way. |
| 56 | Packages | Make packages consistent between Marketing and Billing, including multiple services. |
| 57 | Membership benefit | A percentage discount for now. |
| 58 | WhatsApp | **Real automated messages.** For example, expiry reminders one month before. Provider: see F14. |

### Inventory

| # | Topic | Decision |
|---|---|---|
| 59 | Products | Created at "All locations" means every branch (and SAP for all). Created at one branch means that branch only. Price can differ per branch. Products from a PO sync to that branch. |
| 60 | Cost and MRP | The current behaviour is correct. |
| 61 | PO steps | Done by whoever has the Purchase Orders permission. |
| 62 | Over-receipt | **Not allowed**; cap at the ordered quantity. |
| 63, 65 | Stock requests and transfers | A branch raises a request. The **approver** is notified (POS, email, WhatsApp) and carries out the transfer and marks it complete. The request is fulfilled **when the stock is received**. |
| 64 | Negative stock | **Never** allowed. |
| 66 | Internal checkout | Used for items consumed without a bill (separate from the automatic deduction on bills). |
| 67 | Audit | By permission. |
| 68 | Batches and expiry | They matter; use the earliest expiry first (see F18). |
| 69 | Notifications | Need configurable **notification and approver settings** in roles: who receives billing-discount, stock and other alerts and approvals. |

### Staff, finance, reports, SAP

| # | Topic | Decision |
|---|---|---|
| 70 | Incentives | Defined by the rules in Finance → Manage (monthly, daily and other categories). Example rule: (revenue − salary) ≥ 5× salary pays 3% monthly. Item commission is a % or a fixed amount, as set on the item. |
| 71 | Commission scaling | Reduced when paid via wallet or card, and no commission on package redemption. Confirmed. |
| 72 | Staff perks | Points added by admins, not reset automatically. Used to claim services. |
| 73 | Temporary transfer | Revenue goes to the branch the staff member is currently in. Incentive and commission stay with the staff member, and their profile shows their own revenue. |
| 74 | Payroll | Owner unsure; Claude to propose. |
| 75 | Payroll buttons | Show Generate and Unlock. |
| 76 | Payroll lock | Blocks bill edits for that month. Confirmed. |
| 77–80 | Finance | Make it **enterprise-grade and fully automatic**: closing calculated by the server, shifts fully dynamic, method totals auto-filled, petty cash world-class. |
| 81 | Incentive role groups | Dynamic, not hard-coded. |
| 82 | Reports | **Every report must be exact.** |
| 83 | Manager | The **user who creates the bill**, not the service staff. |
| 84–87 | SAP | **Real-time posting** from every branch, for up to **~400 users**. It must never slow the POS, and the data must always be correct. The JE treatment for memberships and value cards is guided by the SAP team; keep it. |
| 88–91 | Operations | Deferred. |

## Round 2: confirmed (2026-10-11)

| # | Topic | Decision |
|---|---|---|
| F1 | SAP company | Every branch posts to the **same single SAP company database**. |
| F2 | Client phone | A phone number is **unique per client** (globally). A duplicate must raise a clear flag: "this number is already used by client X". |
| F3 | Required client fields | Every client needs a **name and a number**. No anonymous bills. |
| F4 | Clients in SAP | Clients without a GSTIN go under the single generic **B2C** BP, with their details in UDFs (matches `create_sap_business_partner`). Clients with a GSTIN get their own BP. Keep as is. |
| F5 | Value-card model | Confirmed (card balance in face value; card-covered part = face × pre-tax/face ratio + the card's GST; the rest billed normally). The POS must **show it on screen and on the invoice**. |
| F6 | Value-card scope | **Services only.** |
| F7 | Expired cards | Unused balance is **forfeited**. |
| F8 | Advance | No GST when received. When spent, the bill charges the normal GST and the advance is just the payment method. |
| F9 | Cashback | Usable for **services and products**. |
| F10 | Approvals | Needed only for **price change, discount, stock request and stock transfer**. Cancel, payment-method change, closing amendment and PO steps are permission-based admin work, with no approval flow. |
| F11 | Approval thresholds | **Admin settings, dynamic, no hard-coding.** For example, a discount up to X needs no approval and above X does. Add any other useful settings there too. |
| F12 | Waiting for approval | The cashier waits in the POS. The approver can approve **directly from WhatsApp or email**. |
| F13 | Approver scope | Flexible mapping: each branch can have its own approvers, and one person can cover several branches. |
| F14 | Integrations panel | A **panel to set and change WhatsApp and email** at any time. They use a WhatsApp Business account and an Outlook company mailbox (company@companyname.com). |
| F15 | Paytm | **Deferred.** |
| F16 | Trigger promotions | The discount applies to the whole bill. If products are included, use the legal/professional basis (pre-tax). *This conflicts with #30; see decision D1 in the plan.* |
| F17 | Transfers | Branch → branch, **warehouse → branch**, and **inter-state**. IGST vs CGST+SGST must be correct. |
| F18 | Batches | Batch and expiry picked **automatically**, earliest expiry first. |
| F19 | Cancellation | **Same day only.** |
| F20 | Invoice number | The branch-code/FY/sequence idea is approved, but **without slashes**. |
| F21 | Receipt printer | Unknown. Make the receipt **universal** (works on common printer widths). |
| F22 | Product price | MRP is GST-inclusive. Confirmed. |

Questions 88–91 (operations) remain deferred.

## Round 3: plan decisions (2026-10-11)

| # | Topic | Decision |
|---|---|---|
| D1 | Promotion scope | All promotions and memberships discount **services only**. |
| D3 | SAP test company | The SAP company in the code **is a test copy** (its database name says TEST). It may be used for real test postings. |
| D4 | Coupons with a value card | **No coupon can be applied on a bill where a value card is used.** |
| D5 | WhatsApp | Build the WhatsApp Cloud API integration and **leave the credentials empty**. The owner enters them later, so testing incurs no WhatsApp charges. |
| D4b | Value card with a membership | When a value card is used, the bill gets **no other discount at all**: no coupon and no membership. |
| D2, D6–D15 | Remaining decisions | **Approved as recommended** in the plan. |
| Plan | Approval | **Plan approved. Start Phase 0.** |
| Safety | Snapshot | Commit the current state on a **new git branch** (not main, not pushed). |
| Q1 (2026-10-12) | 34,334 phone-less clients | **Keep them.** They are B2B clients. Billing needs a field to enter a client's **GSTIN** (for B2B bills and **e-invoicing**). |
| Q2 (2026-10-12) | SAP price list for MRP | Owner doesn't know. To be found by a read-only check of SAP price lists, then confirmed with the SAP team. |
| Q11 (2026-10-12) | Starter roles | Claude creates test roles with different module/submodule access to prove each role sees only what it was given. |
| Local install | MySQL | **MySQL 8.4 LTS** download approved. **Memurai/Redis was not approved**, so the local background jobs use a database-backed queue. |

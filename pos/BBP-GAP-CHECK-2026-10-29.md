# Aura POS BBP V3 vs our POS — gap check (2026-10-29)

How this was done: read the whole BBP, then searched the backend models/views/settings and the Angular screens for each requirement.
This is a **code-reading check, not a live click-through**. "Done" = the model/endpoint/screen exists. It does not prove every edge works.

## Core modules (BBP sections 3-10)

| Area | Status | Notes |
|---|---|---|
| Dashboard tabs (Summary, Revenues, Clients, Services&Products, Staff, Finance) | Done | All 6 tabs in dashboard.html; hourly series exists |
| Dashboard actual vs target, monthly targets | Done | Center.monthly_target + history |
| Appointments (calendar, staff, services, edit/cancel, reminder SMS button, start invoice) | Done | Reminder SMS button is on the screen; check it really sends |
| Staff profile fields (name, gender, joining, aadhaar, phone, app password, salary, designation, photo) | Done | Uses commission % instead of a single "salary multiple"; incentive config + tiers exist |
| Staff transfer, deactivate, service logs, performance/payroll tabs | Done | StaffTransfer, ServiceLog, payroll tab |
| Billing: client search, services, products, staff per line, equalise/split | Done | staff_members + staff_allocations on each line |
| Billing: promotions, membership, packages, value cards, advance, custom package | Done | custom_package_services handled |
| Multi-payment, Cash/Card/UPI/Cheque/NetBanking/wallets | Done | Payment methods are a setting per branch |
| Invoice must match payments | Done (not re-tested) | |
| Job cards / held bills | Done | held-carts-modal |
| Client blacklist (flag + permission) | Done | is_blacklisted, blacklist_reason, needs "blacklist" permission |
| Marketing: promotions, value cards, memberships, packages, campaigns, WhatsApp log | Done | |
| Expiry reminders | Partial | Exists, but default is **30 days**, BBP says **15** — one setting change (`reminders.expiry_days`) |
| Finance: register, monthly, detailed, refunds, procurement, per-type sales reports | Done | Excel export present |
| Daily closing + petty cash | Done | DailyClosing, PettyCashEntry, voucher no. |
| Incentives | Done | |
| Inventory: stock, receive, history, product/vendor master, PO, transfers, stock audit, batches | Done | |
| Admin: centres, users, roles, services, products, taxes, targets, settings | Done | Roles are permission ticks |
| SAP: invoice, payment, credit note, petty cash, PO, transfers, masters | Done | Per our SAP notes, the live posting is the part still to be proven |

## Section 13 "Additional requirements" — where the real gaps are

### Missing (nothing in the code)
1. **2-hour session timeout.** Tokens last **7 days** (API_TOKEN_MAX_AGE_DAYS). BBP wants hard 2 h.
2. **Block duplicate-device login / force logout.** No device tracking at all.
3. **Geofencing / location-restricted manager login**, and the **email to Sumit Israni** when an Admin logs into a geo-fence. Only an optional IP-geolocation log setting exists.
4. **Draft Print** (price-only preview without salon/client details).
5. **QR payment code printed on the bill.** The only QR is the e-invoice IRN QR.
6. **Negative-inventory override** per line, with report + email to RSM. Today billing **blocks** a line with "Not enough stock".
7. **Training module** (events, masters, centre-manager nominates staff, upcoming schedule alerts). Nothing exists.
8. **Loyalty points** (earn rules per service/product category, 180-day expiry, redeem at bill). Nothing exists (only cashback wallet).
9. **Salon vs Studio separation** with a single landing page to pick which one.
10. **Cheque lifecycle:** reference / clearance no., deposit date, invoice stays "Open", alert to accounts on pending clearance. Cheque is only a payment type.
11. **Gift cards** (sell and redeem by code). Only value cards exist; Finance report has a hard-coded zero "gift_cards" line.
12. **Backdated-entry block (more than 10 days).** Not found.
13. **Dedicated Import & Export module** (the code has per-screen imports, not one isolated module).
14. **Total visits overall + at this centre shown on the billing screen.** Not found.
15. **Auto reports: hourly summary email** + manual request. Not found as a feature (only closing SMS recipients).
16. **WhatsApp -> SMS fallback for OTP.** No OTP flow found, and no SMS sender.
17. **SMS campaigns** by last-visit filter / CSV upload with test SMS. Campaign UI mentions csv once — verify; SMS sending itself is not there.
18. **Real-time GST number validation against SAP** with pop-up. Only GSTIN storage + e-invoice.
19. **Bulk upload of one-time unique promo codes** and "Coupon Already Used" message / invoice lookup by coupon code. Coupons are one-per-client usage-limited; no unique-code list.

### Partial
- **Manager discount cap 30%:** an approval threshold exists (`approvals.discount_free_percent`), with approve link + token. Need to confirm its default is 30 and that it covers custom packages.
- **Multi-staff per line (up to 4):** supported; the "max 4" limit not confirmed.
- **Multi-value-card split on one bill:** payments carry a `value_card_id`, but I did not find a clean multi-card flow; the BBP also says one value card per invoice — the two statements conflict in the BBP.
- **Min-stock thresholds POS + SAP sync / 60-day batch expiry:** reorder level + daily low-stock event + batches with expiry_date exist; no explicit 60-day expiry alert or SAP threshold sync found.
- **Stock-in with expiry date:** batch has expiry_date; scanner entry not checked.
- **Warehouse dispatch free-text note:** StockTransfer has `notes` and `received_note`; fine if "notes" is shown on dispatch.
- **ITR (inventory transfer request):** StockRequest -> approve -> transfer exists; matches the intent.
- **Staff transfer with both managers approving:** StaffTransfer has a status, but the two-sided approve/reject was not found.
- **Blacklist for staff, Aadhaar/phone check, reason mandatory, alert to RSM/Admin/Owner:** client blacklist has reason + permission; staff blacklist and the alert routing not found.
- **Blacklist popup at billing:** logic exists on the server (billing/views.py:1171); popup text not checked.
- **Credit note:** posted to SAP on refund; a separate on-screen credit-note document was not checked.
- **GST-client invoice / e-invoice:** built (IRN, QR, GSTIN, B2B).
- **Gender "Other":** staff seed data uses it; confirm the dropdowns show it.
- **Images for products and staff:** staff has `image`; product has **no** image field.
- **Search by name or phone at checkout:** client-search component exists; name search not confirmed.
- **Role-restricted dashboard (manager = current month only; RSM/HO = all) and quick filters (week/month/last month/year/all):** access is by permission ticks, no fixed range limit per role.
- **Staff / client web apps (staff.POS.com, client PIN via WhatsApp):** client has `app_pin`, staff has `app_password`; the web apps themselves are not in this repo.
- **Petty cash single-week view, petty cash -> SAP sync:** SAP sync field exists; the one-week restriction was not found.

### Not verified (no evidence either way)
Login screen showing a 4-digit PIN then password and "Reset Password" email; server-vs-device clock check; Chrome-only note; Help Centre / Manage Subscriptions menu.

## Rough count (of ~75 line items in the BBP)
- Done: ~40
- Partial: ~16
- Missing: ~19

## Biggest gaps by effort (my suggestion)
- Small: reminder 15 days, "Other" gender, product image, 2-hour session, draft print, total-visits display, gift cards as a value-card flavour.
- Medium: negative-inventory override + RSM email, cheque lifecycle + alert, backdate block, QR on bill, hourly auto-report email, coupon code list + "Coupon Already Used".
- Large: loyalty points, training module, salon/studio split, geofencing + duplicate-device logout, SMS campaigns + OTP fallback.

## Second pass — extra items found (sections 2-12 of the BBP that the first pass skipped)

### Missing
- Login: 4-digit PIN step then password; "Reset Password?" link that mails a code; "Reset PIN". Only "change own password" and Admin-resets-another-user exist.
- Device date/time vs server check that restricts billing.
- Send Client PIN by WhatsApp, and Admin viewing the Client WebApp PIN (Client.app_pin field exists, no send/view action). The Staff WebApp and Client WebApp themselves are not in this project.
- Receipt by email / WhatsApp to the client after Close Invoice (print exists; sending not found). WhatsApp authentication PIN at finalise: not found.
- Appointment cancellation policy setting (only open/close time, slot minutes, default duration, closed days exist).
- Dashboard "Export / Screenshot" step.
- Staff: "Find by phone" (shows current/last location) and "phone already exists -> offer to reactivate".

### Partial
- Per-client Credit Limit: only a per-centre credit_limit exists.
- Equalise button for split staff credit: manual allocation exists, button not found.
- Manual stock adjustment for damaged/expired: only through Stock Audit.
- Bulk price update across all salons at once: service bulk upload exists; one-shot "raise price everywhere" not found.
- Full client history ledger for managers: last-visit and balances are shown; a full visit/transaction/notes ledger screen not confirmed.
- WhatsApp templates in Admin: notification rules have editable body/template; not specifically the appointment/receipt/expiry template set the BBP lists.

### Confirmed done (second pass)
All four promotion types (Discount, Flat Price, Trigger, Cashback); Marketing WhatsApp and Usage tabs; "Add to Advance" on a payment mismatch; staff Usage Report and inactive-staff display; vendor to item mapping; centre branch code and SAP warehouse code; invoice rounding.

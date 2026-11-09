# Permissions audit, 2026-10-21

Scope: every module and submodule in `permission_tree.json` (11 modules, 59 ticks, 24 action ticks) against the
backend that enforces it and the screens that show it.

## How it was checked

1. Walked all 348 routes. Every route that is not login, a self-service page or an API index is behind the permission class.
2. For every one of the 59 ticks, made a role holding ONLY that tick and asked every list endpoint whether it opens.
3. For every write endpoint (469 method/action pairs), called the permission class directly: a role with only the
   right verb must pass, a role with every other verb must not, a verb the tree does not offer must never pass,
   and every action tick must be required (full Add/Edit/Delete without it is refused).
4. Scanned every permission check in the Angular screens against the tree (typos, ticks nobody checks).

Kept as tests: `pos_backend/tests_permission_contract.py` (step 3, runs on every test run, so a new endpoint
with a wrong or missing tick fails the build), `pos_backend/tests_permission_leaks.py`, `salon_admin/tests_dashboard_tabs.py`.

## Correct as found

- Every tick the code names exists in the tree (no typos); every action tick named in code exists in the tree.
- Add never opens Edit, Edit never opens Delete, on all 469 write routes.
- All 24 action ticks are required for their action. 12 are checked by the route walk (cancel a bill, change payment,
  merge clients, payroll generate/lock/unlock/paid, amend closing, SAP retry/resolve, ...). The other 12 are checked
  inside the view or serializer and each has a test (centre bulk import, integration test message and staff app PIN
  were the three with no test; added).
- Reports (Finance, Staff Reports, Manager Discounts) are strict: holding one report tick opens only that report.
- `staff.profile` (salary and commission) is enforced inside the staff serializer, `inventory.fulfill_stock_requests`
  inside the stock-request action; neither has its own URL.

## Fixed in this pass

| What was wrong | Fix |
|---|---|
| A report tick offers Read only, but a stored role file carrying Add/Edit/Delete on it was still honoured | `has_module_permission` refuses any verb the tree does not offer for that tick |
| The stock ledger (stock history) could be created/edited/deleted through the API by such a role (the app never writes it that way) | The ledger endpoint is read-only |
| Marketing > Usage tick was not what guarded the usage report: the offers list tick or even a Billing tick opened it | The report now needs Marketing > Usage |
| A Dashboard-only role (any tab) could read the full client list (names, phones), staff list and products list | Dashboard removed from those lists' read access (it never loads them); Marketing and Finance removed from the client list, Finance from the staff directory |
| Dashboard had one tick for six tabs | Six ticks (done earlier today) |

## Still open: needs your decision

- **Billing > Invoice History** (decided 2026-10-21: leave as is). A New Invoice-only role can read past bills in the
  Drafts & History tab. The owner is happy with this; do not raise it again.
- **Shared lists are readable by any tick in the module.** For example any Inventory tick reads the stock ledger
  (the Checkout tab needs it), any Staff tick reads the staff list and service logs, any Marketing tick reads all
  four perk lists. That is the designed rule ("reading is governed at module level, writes per tick"); only
  reports are strict. If you want it stricter for a module, say which.
- The Logs tick opens the user list (the log screen shows who did what, with names).

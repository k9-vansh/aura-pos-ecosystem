# Billing connections check, 2026-10-21 (found and fixed)

Scope: how Billing connects to Marketing, Inventory, Services, Staff, Appointments and Clients.
Method: read every link in code, tried each suspected gap against the real billing API (SAP off), then fixed all
14 and pinned each with tests in `backend/billing/tests_connections.py` (41 tests).

## Fixed

| # | Gap | Fix |
|---|-----|-----|
| 1 | A service switched off at a branch was sold at the central price | `billing/pricing.py` `resolve()` marks it unavailable; `billing/engine.py` `price_lines` refuses it. The till and the calendar also hide it. A package redemption is exempt (already paid for). |
| 2 | Branch-only service of another branch could be billed | Same check: a `Center`-level service must list this branch. |
| 3 | Switched-off membership could be sold | Same check for membership, package and value card (active, and organisation-wide or this branch's). |
| 4 | Another branch's package could be sold | Same. |
| 5 | Switched-off product could be sold | Same check (also refuses another branch's product). The till hides switched-off products. |
| 6 | Deactivated staff could be credited | `billing/serializers.py` refuses an inactive person on a line. |
| 7 | Campaigns named the service by name | The service ids are worked out when a campaign is saved (`marketing/service_links.py`) and matched by id; the name is shown as it is today. Existing campaigns were migrated (`marketing/migrations/0019`). Campaigns with no ids still match by name. |
| 8 | Cancel returned stock unbatched | Stock ledger rows now carry the bill (`inventory.StockTransaction.invoice`); `inventory/stock.py` `record_back` gives each batch back what the bill took, for products and service consumables. Bills made before this fall back to unbatched. |
| 9 | Future-dated transfer moved the person at once; return on the end date itself | A transfer moves the person on its start date and a temporary one ends the day AFTER its end date (`staff/utils.py`). New `StaffTransfer.applied` flag; existing transfers marked applied. The sync also runs on the first request of each day. |
| 10 | Appointment not checked | `billing/serializers.py` `_check_appointment`: same branch, same client (by id, or by phone number for a phone-only booking), and still Scheduled or Checked-in. A Cancelled or No-show one must be restored on the calendar first. |
| 11 | `appointment_id` completed without linking | `appointment_id` is now the same as `appointment` (linked, checked); finalising only completes the appointment the bill points at. Cancelling a bill no longer overwrites a No-show or cancels an appointment another live bill stands on. |
| 12 | Value cards logged as Product | New log type "Value Card" (`staff/models.py`), commission report gains `value_card_revenue`, usage report gains `value_cards_count`; past rows re-labelled by migration `staff/0027`. |
| 13 | Products with no MRP sold at cost | New setting **Sell products that have no MRP** (Admin > Settings > Billing), ON = today's behaviour. Turn it off once the MRP is loaded. |
| 14 | Anonymous bills | New setting **Every bill needs a client**, OFF = today's behaviour (the till's Walk-in button keeps working). ON refuses to complete a bill with no client. |

## Things you will notice

- Re-billing after a cancelled bill: the cancel sets the appointment to Cancelled (decision #23), so restore it on the calendar before billing it again.
- 13 and 14 are your business decisions; both ship at today's behaviour so nothing at the till changes until you switch them.
- Gaps found in the check that were already correct: stock locking, BOM, commission split, perk issue/removal, same-day cancel, payroll lock.

## Tests

Backend: full suite on SQLite and the touched apps (staff, inventory, billing connections) on MySQL.

# Who does what (test team)

Made by `python manage.py seed_test_team --yes` (test database only). Logins and the shared password are in
`TEST-LOGINS.md` (kept out of git). Access is only ticks in Roles & Permissions: no code looks at a role's name.

Pilot centres: **A** = Downtown Flagship, **B** = Metropolis Branch, **W** = Central Hub & Warehouse.
Every active centre also has stock (warehouses 1000 of everything, branches 100, retail 30) and 3 staff.

## The stock request, step by step

| Step | Who | Role | Tick that allows it |
|---|---|---|---|
| 1. Asks for stock for the branch | requester.a / requester.b (or the branch manager) | Branch Stock Requester | Make Stock Requests |
| 2. Says yes or no | approver.a / approver.b (or the regional manager) | Branch Stock Approver | Approve Stock Requests (Edit) |
| 3. Picks where from, makes the transfer, dispatches | storekeeper.wab | Store Keeper | Fulfill Stock Requests (Edit) + Stock Transfers |
| 4. Receives it at the branch | manager.a / manager.b | Branch Manager | Stock Transfers (Edit) at that branch |

Rules the server enforces: nobody approves their own request (the owner may); only the person who made a request or an
approver can cancel or change it, and only while it is pending; the person who sent a transfer cannot receive it;
an approver or receiver only acts for the centres they are assigned to.

## The people

| Login | Role | Centres | Can | Cannot |
|---|---|---|---|---|
| requester.a / requester.b | Branch Stock Requester | own branch | make stock requests, look at products and stock history | approve, send, receive, bill, buy, see money |
| approver.a / approver.b | Branch Stock Approver | own branch | approve or reject requests of their branch | make requests, send, bill |
| cashier.a / cashier.b | Cashier / Front Desk | own branch | bill, book appointments (check-in, no-show...), add and edit clients | stock changes, finance, admin, marketing |
| manager.a / manager.b | Branch Manager | own branch | bill and cancel bills, appointments, staff, approve its stock requests, receive transfers, petty cash, branch reports | edit products, buy, change roles or settings |
| storekeeper.wab | Store Keeper | W + A + B | send approved requests, dispatch transfers, checkout, audit | approve, receive a transfer they sent themselves, bill, buy |
| purchasing.abw | Inventory & Purchasing Manager | A + B + W | products, vendors, purchase orders and receiving, audit, checkout, transfers | billing, finance, approve requests |
| marketing.ab | Marketing Executive | A + B | offers, cards, memberships, packages, messages | stock, billing, finance |
| finance.all | Finance & Accounts | all | every finance report, petty cash, refunds, SAP sync view, bills (view) | bill, stock, users |
| regional.all | Regional Manager | all | everything a branch manager does, for every centre, plus approve and transfer | roles, settings, SAP retry |
| adminhelper.all | Admin Assistant | all | users, services, clients | roles, settings, SAP, finance, stock |

The owner ("hello", superuser) can do everything.

## What was checked

45 checks with these logins on a copy of the database, all passing: asking, who sees a request, approving, own-request
rule, cancel/change rules, transfer create/dispatch/receive, stock moving at both ends, request becoming Fulfilled,
the ledger rows, and a screen-by-screen matrix of what each login can open. In the browser, the requester sees only
Cancel on a pending request, the approver sees only Approve and Reject.

## Known limits

- Reading is decided per module, not per tab: anyone who may read Inventory can fetch its read-only lists for their own
  centres (vendors, stock history, requests) even if their tab is hidden. Writing is strictly per tick.
- The stock added here is test data and is not in SAP. "Bring SAP stock into the POS" is switched off
  (Settings > SAP mapping > 0) because SAP has no stock and would zero it. Set it back before using real SAP stock.
- A bill or transfer made now is queued to SAP and will fail until SAP's posting periods (Oct 2026 on) are unlocked.

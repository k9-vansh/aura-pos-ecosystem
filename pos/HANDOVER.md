# Handover

State as of 25 Aug 2026. Nothing is committed to git — every change is in the
working tree, so run it and look.

## Verify it yourself

```bash
# Backend — 842 tests
cd backend
python manage.py test --settings=pos_backend.settings_test

# Frontend — 99 tests, and a real production build
cd ../frontend
node node_modules/@angular/cli/bin/ng.js test --watch=false
node node_modules/@angular/cli/bin/ng.js build --configuration production
```

**Do not trust `tsc -p tsconfig.json` or `ngc -p tsconfig.json`.** That file is a
solution file (`"files": []` with project references), so both check *nothing*
and always exit 0. I proved it by planting a bogus template call, which both
missed — and a real duplicate-identifier bug in `finance.ts` survived because of
it. Use `-p tsconfig.app.json` for tsc, or `ngc -p tsconfig.check.json`.

If the build fails on esbuild, `node_modules` was installed on Linux. Install
both platform binaries **in one command**, or npm prunes the other:

```bash
npm install --no-save --no-audit --no-fund --force @esbuild/win32-x64@0.28.2 @esbuild/linux-x64@0.28.2
```

---

## Read this before you deploy

`backend/deploy/GO-LIVE.md` is the runbook. The short version:

Your live server runs `manage.py runserver` — Daphne's **development** server —
with `backend/.env` containing exactly one line: `DJANGO_DEBUG=True`. Every guard
in `settings.py` is written `if not DEBUG:`, so that line also disables:

- the refusal to boot with the committed `SECRET_KEY` (which is in git history —
  session cookies and the staff/client app tokens are signed with it, so anyone
  with the repo can forge a login for any user)
- secure cookies, the SSL redirect, HSTS
- the CORS allowlist (`CORS_ALLOW_ALL_ORIGINS` is `True`)

There is no TLS: every allowed origin is plain `http://` on a bare IP.

`python manage.py check` now reports all of it, every time.

Ready to copy: `deploy/gunicorn.conf.py`, `deploy/pos-backend.service`,
`deploy/nginx-pos.conf`.

---

## Two things only you can decide

1. **Confirm 18% GST** on memberships and value cards with your accountant. I set
   it as the default on SAC 9997 reasoning; it now goes on real invoices.
2. **Run `repair_invoice_math`.** Historical service lines are stored at ₹0,
   which is why staff commission has been under-paid. Back up first, dry-run,
   read the report, then `--apply`.

---

## What I changed that you should know about

- **`node_modules`**: added the Windows esbuild binary so the build runs here.
  Your environment, not your code.
- **Deleted `backend/gunicorn.conf`** — it was a supervisor stub with
  `directory=/path/to/backend`, never installed. Replaced by `deploy/`.
- **Emails are now stored lowercased**, with a migration that lowercases existing
  rows. Accounts differing only in capitalisation are *reported and left alone* —
  those are two real accounts and merging them could hand someone another
  person's access. Check the migration output.
- **Value cards are deactivated, not deleted**, when the selling bill is
  cancelled. `Payment.value_card_id` has no foreign key, so deleting the card
  made every later refund fail silently.
- **The till now sends an `Idempotency-Key`** per checkout attempt. A retry
  returns the same bill instead of creating a second one.

---

## Where the remaining work is

Roughly 28 findings are open, all from the capacity/MySQL audit, none of them
blocking. In the order I would take them:

1. **`OptionalPagination` returns whole tables** unless `?page` is passed
   (`pos_backend/pagination.py`). Measured 397ms → 2,514ms as customers went
   50 → 250. Fixing it means changing frontend callers too, which is why I left
   it.
2. **28 date filters wrap the column in `DATE()`**, discarding the index. I
   rewrote all 28; the tests caught it breaking the dashboard (those dates are
   *strings*, not date objects) and I reverted it. Needs doing per-site with
   measurement — a shifted report boundary is worse than a slow query.
3. **The printed invoice omits your GSTIN and HSN/SAC codes.**
4. **Charset**: verify the database is `utf8mb4` before the first bill, or emoji
   and Devanagari will not store. Cannot be checked from the code.

The full findings with evidence are in the workflow journals under
`.claude/projects/<project>/<session>/subagents/workflows/`.

---

## What has never been tested

Said plainly, because 842 passing tests can read as more than they are:

- **No test has ever run against MySQL.** All 842 are SQLite. `select_for_update`
  is a no-op on SQLite, so every concurrency test passes there and proves nothing
  about MySQL.
- **Performance is measured only in query counts**, never under real concurrent
  load.
- **Most screens have never been opened.** The tests are strong evidence about
  billing maths, permissions, imports and reports — and no evidence at all about
  whether the appointments screen looks right.

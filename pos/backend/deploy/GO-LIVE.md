# Go-live runbook

Everything here is one server's worth of work. Do it in order; each step is
independent enough to stop after.

The three things that matter most are at the top, and they are not code changes.

---

## Where things stand right now

Verified by reading the files on the deployed box, not guessed:

| What | Current state | Should be |
|---|---|---|
| Web server | `manage.py runserver` (Daphne **development** server) | gunicorn + nginx |
| `DEBUG` | `True` | `False` |
| `SECRET_KEY` | the committed `django-insecure-…`, public in git | freshly generated |
| CORS | `CORS_ALLOW_ALL_ORIGINS = True` | your own origins only |
| TLS | none — every origin is plain `http://` on an IP | https via nginx |
| Cache | LocMemCache (per process) | Redis |
| Process supervision | none | systemd |

`backend/.env` on the live box currently contains exactly one line:
`DJANGO_DEBUG=True`. Every guard in `settings.py` is written `if not DEBUG:`, so
that single line switches off all of the above at once.

`python manage.py check` now reports all of it. Run it any time to see where you
are.

---

## 1. Fix `.env` (10 minutes, biggest single win)

Generate a key:

```bash
python -c "from django.core.management.utils import get_random_secret_key; print(get_random_secret_key())"
```

Then write `backend/.env`:

```bash
DJANGO_SECRET_KEY=<paste the generated key>
DJANGO_DEBUG=False
DJANGO_ALLOWED_HOSTS=yourdomain.com,www.yourdomain.com

DB_NAME=latest
DB_USER=pos_app
DB_PASSWORD=<a new password, not the one in git>
DB_HOST=localhost
DB_PORT=3306

CORS_ALLOWED_ORIGINS=https://yourdomain.com
CSRF_TRUSTED_ORIGINS=https://yourdomain.com

REDIS_URL=redis://127.0.0.1:6379/1
```

**Expect the app to refuse to start** until `DJANGO_SECRET_KEY` and
`DB_PASSWORD` are both set. That refusal is the guard doing its job — do not put
`DJANGO_DEBUG=True` back to get past it.

Two consequences to plan for:

- **Rotating the secret key signs everyone out.** Do it when the salon is shut.
- **The old DB password is in git history.** Change it in MySQL too:
  ```sql
  ALTER USER 'pos_app'@'localhost' IDENTIFIED BY '<the new password>';
  FLUSH PRIVILEGES;
  ```

Verify:

```bash
python manage.py check          # deployment warnings should be gone
```

---

## 2. Run a real server (30 minutes)

`runserver` is one process on one GIL, with no worker pool, no request timeout
and nothing to restart it. At your expected burst it will queue requests until
every till times out together.

Install Redis and the server:

```bash
sudo apt install redis-server
pip install gunicorn uvicorn[standard]
```

Copy `deploy/gunicorn.conf.py` and `deploy/pos-backend.service` into place, edit
the two paths marked `CHANGE ME`, then:

```bash
sudo cp deploy/pos-backend.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now pos-backend
sudo systemctl status pos-backend
```

Stop the old process first — it is holding port 4091.

### Why ASGI and not WSGI

`asgi.py` routes **all** HTTP through Django, not just `/ws/`. Every view is
synchronous ORM code, so under ASGI Django creates a fresh thread per request,
and a Django database connection is thread-local — meaning a new MySQL
connection per request, with `CONN_MAX_AGE=60` buying nothing.

The gunicorn config uses uvicorn workers so the websocket chat keeps working,
with a **bounded** number of workers so connection count has a ceiling you chose
rather than tracking traffic.

Check MySQL can take it — `workers × threads` must stay well under
`max_connections`:

```sql
SHOW VARIABLES LIKE 'max_connections';
SHOW STATUS LIKE 'Max_used_connections';
```

---

## 3. TLS and nginx (30 minutes)

```bash
sudo apt install nginx certbot python3-certbot-nginx
sudo cp deploy/nginx-pos.conf /etc/nginx/sites-available/pos
sudo ln -s /etc/nginx/sites-available/pos /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
sudo certbot --nginx -d yourdomain.com
```

Without this, staff passwords and customer phone numbers cross the network in
clear text.

nginx also serves `/media/` and `/static/` directly, so Django stops occupying a
worker for the length of every image download.

Run `collectstatic` once before the first deploy:

```bash
python manage.py collectstatic --noinput
```

---

## 4. Before the first bill

**Check the database charset.** If it is not `utf8mb4`, emoji and Devanagari
will not store — and this cannot be told from the code:

```sql
SELECT @@character_set_database, @@collation_database;
SELECT TABLE_NAME, TABLE_COLLATION FROM information_schema.TABLES
 WHERE TABLE_SCHEMA = 'latest';
```

If anything is not `utf8mb4`, convert **before** taking real data:

```sql
ALTER DATABASE latest CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
-- then per table:
ALTER TABLE <name> CONVERT TO CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
```

**Run the invoice repair.** Historical service lines are stored at ₹0, which is
why staff commission has been under-paid. Take a database backup first:

```bash
mysqldump -u root -p latest > backup-before-repair.sql
python manage.py repair_invoice_math --dry-run     # read the report
python manage.py repair_invoice_math --apply
```

**Run the test suite against MySQL**, not SQLite. Everything so far has only
been proven on SQLite:

```bash
python manage.py test
```

---

## 5. Weekly, from then on

The activity log gains a row per write and nothing removes them — roughly a
million rows a week at your expected traffic, and the Logs screen slows in
proportion.

```bash
python manage.py purge_audit_logs --older-than 365 --archive-to /backups/audit
python manage.py purge_audit_logs --older-than 365 --archive-to /backups/audit --apply
```

Dry run by default. It refuses a window under 90 days without `--force`, and
archives to gzipped JSONL before deleting.

---

## What is still not done

Being straight about it:

- **Roughly 31 findings remain**, mostly missing indexes and MySQL-vs-SQLite
  differences. None of them stop you opening; several will bite as tables grow.
- **28 date filters wrap the column in `DATE()`**, which discards the index. I
  rewrote them, the tests caught it breaking the dashboard (those dates are
  strings, not date objects), and I reverted it. It needs doing per-site with
  measurement on the real database — a wrong report boundary is worse than a
  slow query.
- **Updated 20 Oct 2026:** the backend suite (1,177 tests) now passes on MySQL 8.4,
  and the frontend's 154 tests pass. What has not been done is a run against a
  copy of the real data and a day of bills against a SAP sandbox.
- **Every screen was opened and tried by role on 19-20 Oct** (60 test users, one
  per module and sub-module), but the Incentives payroll Generate screen, shared
  and refunded bills and mid-month changes still need a hand-calculated check.
- The full list of what is left, in order, is section 5 of
  `Desktop/aftercirapos/QA-REPORT-2026-10-19.md`.

---

## Audit log: record each person's real address

Behind nginx, Django only sees nginx (127.0.0.1), so every audit row would show
the same address. Put this in `backend/.env` on the server:

```
AUDIT_LOG_TRUST_FORWARDED_FOR=1
```

The audit log then uses the address nginx wrote in `X-Real-IP` (the proxy sets it
from the real connection; a caller cannot choose it). Only set it when nginx is
really in front, with the `proxy_set_header X-Real-IP $remote_addr;` lines from
`nginx-pos.conf`. Showing a city for outside addresses stays off until you turn on
Admin > Settings > "Show the location of IP addresses".

---

## Many centres at once (80+ centres, crores of bills)

Found by the 324-lakh-bill test of 31 Oct 2026 (`Desktop/aftercirapos/bulk-harness/BULK-TEST-REPORT-2026-10-31.md`).
The numbers were right at that size; only speed broke. What protects the tills now, and what you set:

| Setting in `backend/.env` | Default | What it does |
|---|---|---|
| `HEAVY_REPORT_SLOTS` | 4 | How many big reports (all-centre dashboards, tax, services, usage report, incentives...) one web worker builds at a time. `0` switches the gate off. |
| `HEAVY_REPORT_QUEUE` | 4 | How many more may wait for a turn. Beyond that a report is told to retry at once, because a waiting thread is a thread no till can use. |
| `HEAVY_REPORT_WAIT_SECONDS` | 25 | How long a queued report waits before "many big reports are being built, try again". |
| `HEAVY_REPORT_QUERY_SECONDS` | 120 | MySQL stops any single query of a report after this long; the screen shows "choose a shorter period or fewer centres" (HTTP 503) instead of a raw 500. `0` = no limit. |
| `DB_REPLICA_HOST` (+ `DB_REPLICA_PORT`, `_NAME`, `_USER`, `_PASSWORD`) | not set | A MySQL server that replicates the main one. When set, the big reports read from it, so an owner opening a dashboard cannot slow billing at all. Login, tokens and every write stay on the main server. |

Also at this size:

- Run several web workers (`GUNICORN_WORKERS`, default `2 x cores + 1`, at most 8); every worker has its own
  report gate. Keep the database on its own machine, with a buffer pool of about half its RAM.
- The till's customer search needs the `clients` migration 0021 (a reversed-phone column and four indexes). On a
  crore-row customer table it backfills in slices and builds the indexes in about an hour: apply it in a quiet
  window (`python manage.py migrate clients`).
- Several summary-table ideas (a row per centre and day) are NOT built: bills of a closed day can still be cancelled
  or changed, so a stored total could silently disagree with the bills. Decide that rule first.

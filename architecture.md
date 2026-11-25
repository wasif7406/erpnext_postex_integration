# PostEx Integration — how the app works

**Current-state architecture (BUILD 2026-11-26.2).** This document describes the
code as it runs today. The earlier forward-looking draft ("target model",
"migration plan", "decisions still open") described the build that is now
delivered; it lives in git history at `680bb11^` if you need the rationale.

Read this top-down: what the moving parts are, who is allowed to write what,
then the three flows (poll → documents → money) and the guards that make them
re-runnable.

---

## 1. Purpose

One PostEx merchant account (or several) for one ERPNext company (or several),
with **no accounting name hard-coded anywhere**: every ledger account, warehouse
and cost centre comes from a `PostEx Account Mapping` row, so the same app serves
two companies with different charts of accounts.

What it automates, end to end:

| stage | the app does |
|---|---|
| booking (optional) | creates the order at PostEx, writes the tracking back |
| status | polls the courier and keeps `Courier Status` + `Courier Tracking ID` current |
| stock | Stores → In-Transit on pickup, In-Transit → customer on delivery, back to Stores on return |
| revenue | Delivery Note + Sales Invoice the moment the parcel is delivered |
| cost | courier fee + GST, 4% withheld tax, return-leg cost |
| cash | Payment Entry per CPR once PostEx pays, per invoice |

**Non-goals:** it never posts anything it cannot source from PostEx or the
merchant's own settlement export; it never books a courier cost from a guess; it
never touches imported history.

---

## 2. System overview

```
                 ┌────────────────────────────┐
                 │ PostEx Merchant API        │
                 │ get-all-order              │
                 │ get-unbooked-orders        │
                 │ track-order/{tn}           │
                 │ payment-status/{tn}        │
                 │ add-order · cancel-order   │
                 └─────────────┬──────────────┘
                               │  HTTPS + per-account token
                 ┌─────────────▼──────────────┐
                 │ client.PostExClient        │  1 req/s · 800 req/run
                 │  paced · budgeted · typed  │  GET retried, POST never
                 └──────┬──────────┬──────────┘
                        │          │
        ┌───────────────▼──┐   ┌───▼──────────────────┐
        │ sync.py          │   │ settlement.py        │
        │ poll + map       │   │ API settlement       │
        └───┬──────────────┘   └───┬──────────────────┘
            │ run_helpers          │
   ┌────────▼──────────────────────▼───────────┐
   │ stock.py   pickup · DN · invoice          │
   │            return · terminal              │
   │ booking.py create/cancel/link a booking   │
   │ webhook.py status only                    │
   │ settlement_log.py CSV reconcile           │
   └────────┬──────────────────────────────────┘
            │ every write through claim_field()
   ┌────────▼──────────────────────────────────┐
   │ Sales Order.custom_postex_*                │
   │ Delivery Note · Sales Invoice · Stock Entry│
   │ Journal Entry · Payment Entry              │
   └───────────────────────────────────────────┘
```

---

## 3. Data model

| record | purpose |
|---|---|
| **PostEx Account** | token, base URL, `sync_window_days` (60), `withholding_percent` (4), `enable_sync`, `dry_run`, `enable_auto_booking`, `webhook_secret`, `last_run_result` |
| **PostEx Account Mapping** (child) | role → account / warehouse / cost centre. Roles: `Courier Finance`, `Debtors`, `Delivery Charges`, `Tax Withheld`, `Unsettled COD`, `Unaccounted`, `Stock Loss`, `In-Transit Warehouse`, `Stores Warehouse`, `Cost Center` |
| **Sales Order `custom_postex_*`** | the whole state: `account`, `status`, `tracking`, `delivered_on`, `status_changed_on`, `last_attempt_on`, `settled_on`, `payout_checked_on`, `needs_attention`, failure counter, fee fields (`delivery_fee/tax`, `reversal_fee/tax`), and one link per document: `pickup_entry`, `delivery_note`, `sales_invoice`, `return_entry`, `return_charges_entry`, `payout_entry`, `charges_entry` |
| **Sales Invoice `custom_postex_*`** | `account` (read-only), `tracking`, `status`, `settled_on`, `payout_entry`, `charges_entry` |
| **PostEx Sync Run** (+ `PostEx Sync Failure`) | one row per run: counters, backlog, per-order failures |
| **PostEx Webhook Log** | every inbound webhook, accepted or rejected |
| **PostEx Booking Log** | every booking attempt and its response |
| **PostEx Settlement Upload** (+ Exception, Booking children) | one CSV reconciliation: counters, rows needing a human, documents created |

Two fields deserve special mention because they are **join keys, not decoration**:

* `Sales Order.custom_postex_sales_invoice` — settlement selects on
  `ifnull(custom_postex_sales_invoice,'') != ''`. Empty means the order is
  invisible to settlement even if an invoice exists. It is also the
  "already invoiced" idempotency flag every stock helper tests before creating
  anything. Nothing outside the app sets it.
* `Sales Order.custom_postex_account` — the ownership claim. Helpers only touch
  orders assigned to this account; the sync may claim an unassigned order when
  its reference appears in this account's feed (`_claim`, a CAS UPDATE).

---

## 4. Entry points — who may write what

| trigger | handler | may write |
|---|---|---|
| cron `*/15` | `tasks.sync_all_accounts` → `sync.sync_account_locked` | status/tracking/fees **and documents** (pickup, DN, invoice, return, terminal) |
| cron `*/30` | `tasks.settle_all_accounts` → `settlement.reconcile_account_locked` | charges JE, Payment Entry, invoice payment status |
| cron `03:15` daily | `tasks.late_return_sweep` → `sync.sync_late_returns` | status only, Delivered/Returned inside 45 days |
| cron `07:00` / `08:30` | `webhook_watchdog`, `daily_digest` | nothing (alerts) |
| webhook | `webhook.receive` | status/tracking **only** — deliberately never documents |
| desk | `booking.book_order`, `api.resync_order`, `api.requeue_order` | booking / re-read one order |
| desk | `api.check_settlement_upload`, `api.apply_settlement_upload` | books from a Settlement Logs CSV |
| `Sales Order.on_submit` | `booking.on_sales_order_submit` | enqueues a booking when the account allows it |

Every cron/manual handler runs under `tasks.run_locked(kind, account, fn)` — a
per-account, per-kind `filelock` with `timeout=0`, so a second concurrent run
returns `{"skipped": "... already running"}` instead of racing.

---

## 5. The poll — `sync_account()`

```python
sync_account(account, limit=page_size, run_helpers=True, mode="Active"):
    guard: enabled · enable_sync · token · required custom fields present
    run = insert PostEx Sync Run(status="Running")

    # one call for the whole window; a ref is NOT unique at PostEx
    ref_map = client.list_orders(today - sync_window_days, today)   # 60 days
              #  -> { orderRefNumber: [payload, ...] }
    client.list_unbooked_orders(...)                                # gap-fill only

    orders = SELECT 200 Sales Orders
             ORDER BY ifnull(last_attempt_on, '1900-01-01') ASC     # never-tried first

    for order in orders:
        if budget_exhausted: break                 # stop cleanly; cursor keeps the rest

        payloads = ref_map.get(order.po_no)
        if len(payloads) > 1:  fail "ambiguous reference"   # never "last one wins"
        payload = payloads[0] if payloads else None
        if payload is None and order.tracking:
            payload = track_order(order.tracking)   # per-order fallback, any age
        if payload is None:
            no_data += 1;  maybe_auto_book(order)   # booking it is still fair
            continue

        if order has no account and not _claim(order, this_account):
            continue                                # another account won
        write status (mapped), tracking, fees, delivered_on   # only when changed
        reopen a Closed SO if the parcel came back to life

    if mode == "Active" and run_helpers:
        run_helpers(account)                        # §6
    record summary on the run + Account.last_run_result
```

`_touch()` stamps `last_attempt_on` for **every** attempt, success or not, which
is what makes the 200-row window rotate instead of pinning a broken order.
`CURSOR_COMMIT_EVERY = 25` commits progress periodically.

### 5.1 What is in scope (`_eligibility`)

```sql
docstatus = 1
AND company = <this company>
AND ifnull(custom_postex_account,'') IN ('', <this account>)   -- claimable
AND ifnull(custom_courier,'') IN ('', 'PostEx')                -- never QD / F&Q
AND ifnull(custom_postex_needs_attention,0) = 0                -- quarantine
AND ifnull(custom_postex_booking_cancelled,0) = 0              -- merchant's cancel is final
AND ifnull(custom_shopify_state,'') = ''                       -- imported history is out
AND ( status blank / Unknown / ACTIVE_STATUSES
      OR (Cancelled|Expired AND Sales Order not Closed/Completed/Cancelled) )
```

Late-return mode instead requires `status IN (Delivered, Returned)` and
`status_changed_on >= today - 45`.

### 5.2 Status mapping

`constants.STATUS_MAP` is an **ordered** list of substring needles; the first hit
wins, which is why `"un assigned"` precedes `"unassign"`, `"en route to postex"`
precedes `"postex warehouse"`, and `"unbook"` precedes `"booked"`. When
`transactionStatus` is empty, `HISTORY_CODES` (`0001`…`0013`) is consulted —
unbooked orders carry code `0001` / "At … Warehouse" and would otherwise look
`Booked`.

---

## 6. Documents — `run_helpers()`

Five helpers, each expressed as *"orders for which this document is still
missing"* plus a `claim_field` CAS after creating it. Order matters:

```python
pickup_transfer()      # Stores  → In-Transit      (status in PICKUP_STATUSES)
delivery_and_return()  # In-Transit → customer
    _create_delivery_notes()   # DELIVERY_WAITING
    _create_invoices()         # INVOICE_WAITING  (tax + discount rows copied)
    _process_returns()         # RETURN_WAITING   (In-Transit → Stores + cost)
    close_terminals()          # TERMINAL_WAITING (write-off / credit note / close)
helper_backlog()       # what is ready vs blocked, for the run summary
```

### 6.1 The state machine

| PostEx status | stock | documents | books |
|---|---|---|---|
| `Unbooked`, `Booked` | — | none | — |
| `Picked By PostEx` … `Out For Delivery`, `Transferred`, `Attempted`, `Delivery Under Review`, `Out For Return` | Stores → **In-Transit** | Material Transfer | — |
| `Delivered` | In-Transit → customer | Delivery Note, then Sales Invoice | revenue + COGS |
| `Returned`, never invoiced | In-Transit → Stores | none (no invoice for a return) | delivery-attempt + reversal fee |
| `Returned` **after** invoicing | **return DN** into Stores | credit note (stock-neutral) | revenue and COGS reversed |
| `Lost` / `Damage` / `Stolen` | written off | Material Issue | stock loss |
| `Cancelled` / `Expired` / `Un-Assigned By Me` | In-Transit → Stores | none | cost if PostEx billed it |

`CLOSABLE_STATUSES` drives `close_terminals`; **`Delivered` is deliberately not
in it** — ERPNext marks a fully delivered+billed order `Completed` itself, and
closing early would block the invoice that has not been created yet.

### 6.2 Backlog line

Every run summary ends with

```
| backlog ready/blocked: pickup 0/0, delivery 0/3, invoice 0/0, return 0/0, terminal 0/0
```

`ready` = queued work, `blocked` = quarantined. A green run that creates nothing
is therefore distinguishable from a stalled one, and any blocked row adds
`ALERT: N quarantined order(s) are holding back document creation`.

---

## 7. Money

### 7.1 Two settlement paths, one core

```python
# A. polling (cron */30) — MAX_PAYMENT_STATUS_CALLS = 40 per run
reconcile_account(account):
    orders = Delivered + invoiced + not settled, oldest-checked first
    for order in orders:
        stamp payout_checked_on                     # failures rotate, never pin
        if tracking_conflict(tracking): report; continue
        dist = client.payment_status(tracking)
        if not dist.settle: continue                # healthy "not yet"
        settle_order(order, invoice, settled_on=dist.settlementDate, cpr=dist.cpr1)

# B. settlement log (desk: Check → Apply)
reconcile(account, csv, dry_run, from_date):
    for row in parse_log(csv):
        if from_date and row.date < from_date: skipped += 1; continue
        order = find by tracking, then po_no
        if row.status == "Return":  book_return_cost(fee + gst); continue
        state = payment_state(row)      # paid | partial | unpaid | nothing_due
        if state != "paid": report; continue
        settle_order(order, invoice, fee=row.fee, tax=row.gst,
                     withholding=row.itwh + row.stwh)     # exact CPR figures

# shared
settle_order(order, invoice, fee, fee_tax, withholding):
    if order.charges_entry submitted: return          # idempotent
    cod = order.grand_total            # withheld tax is on the COD, not the invoice
    net = fee + fee_tax + withholding
    to_debtors, excess, role = charge_split(net, invoice.outstanding)
    JE  Dr Delivery Charges (fee)  Dr Tax Withheld (4% of COD)
        Cr Debtors (≤ outstanding, party + invoice)
        Cr Courier Finance / Unsettled COD (the rest)
    if invoice.outstanding <= 0: mark Paid; return     # parked / prepaid invoice
    PE  Dr Courier Finance / Cr Debtors  (remaining outstanding)
```

`payment_state()` decides **paid** on `upfront + reserve + balance ≥ COD − 0.5`,
not on "some money arrived": the invoice is settled for its whole outstanding, so
a first leg must be `partial` and is reported, never booked.

### 7.2 What posts where

| event | debit | credit |
|---|---|---|
| delivery | COGS (via DN) | Stock In Hand |
| invoice | Debtors | Sales |
| settlement | Delivery Charges (fee + GST), PostEx Tax Withheld (2% ITWH + 2% STWH) | Debtors |
| payout | Courier Finance | Debtors |
| prepaid invoice | Delivery Charges, Tax Withheld | **Unsettled COD** (Debtors has nothing left to credit) |
| returned parcel | Delivery Charges | **Courier Finance** — the payout is already net of that fee |
| lost / damaged | Stock Loss | In-Transit stock |

`Courier Finance` must always equal the PostEx wallet statement; `Unsettled COD`
holds COD collected but not yet matched to an invoice. When a historical batch
was booked by a lump journal entry, the CSV apply needs a cutover
(`apply_blocker`) or it would re-book costs the lump already covered.

---

## 8. The API client

```python
_send(method, path):
    if requests_made >= run_request_budget: raise PostExBudgetExceeded   # ~800
    sleep(request_delay)                          # default 1.0 s, floor 0.25 s
    try:    response = requests(method, base_url + path, headers={token})
    except transient:
            if method == "GET" and attempt < 2: retry
            error.ambiguous = (method == "POST")  # may have reached PostEx
            raise PostExAPIError
    if status >= 500 and method == "GET" and attempt < 2: retry
    handle(response)                              # 4xx → PostExAPIError(status_code)
```

*POST is never retried.* A booking that timed out may or may not exist, so the
order is put into `booking_doubt` and resolved by `track-order` later — never by
firing the request again.

Typed helpers: `list_orders`, `list_unbooked_orders`, `track_order`,
`payment_status`, `add_order`, `cancel_order`. Budget exhaustion is **never**
treated as an order failure: the run stops, `budget_exhausted` is recorded, and
the cursor resumes next run.

---

## 9. Guards that make everything re-runnable

| mechanism | prevents |
|---|---|
| `utils.claim_field` — conditional `UPDATE … WHERE ifnull(f,'')=''` | two runs creating the same document (the 25-10 duplicate DN/invoice) |
| verify-after-create — re-read the SO link, cancel our own doc if another run won | cron racing a manual "Sync now" |
| cursor `last_attempt_on` (NULL first, stamped on every attempt) | a broken order pinning a scan slot |
| cursor `payout_checked_on` + `MAX_PAYMENT_STATUS_CALLS` | re-checking the same invoice every 30 min |
| quarantine after `MAX_SYNC_FAILURES` (5) | a permanently-broken order consuming slots |
| `settlement.tracking_conflict` | one parcel paid twice from two Sales Orders |
| `payment_state` (legs ≥ COD) | booking cash that has not fully arrived |
| `apply_blocker` (CSV cutover) | re-booking history a lump JE already covered |
| `run_locked` filelock (per account, per kind) | two concurrent runs of the same job |
| `_claim()` on the account field | two accounts both processing one order |

Limits per run: `MAX_DOCS_PER_HELPER = 100`, `MAX_SCAN_CEILING = 2000`,
`sync_page_size = 200`, `run_request_budget = 800`.

---

## 10. Reading a run

```
Success  everything eligible was processed
Partial  budget exhausted, or per-order failures (they stay queued)
Failed   the run itself threw
```

| counter | meaning |
|---|---|
| `no data N` | no feed ref and `track-order` found nothing — usually no tracking yet |
| `unassigned N` | same, plus the order belongs to no account |
| `ambiguous` | the reference matches several PostEx orders → linked by a human |
| `duplicate_refs` | PostEx itself has several orders on one reference |
| `backlog ready/blocked` | queued work vs quarantined work |
| `failures N` | one row per order in the Failures child table |

---

## 11. Operating it

**Routine:** nothing. Watch `PostEx Account.last_run_result` (or the daily
digest). Green runs with `backlog 0/0` need no attention.

**When an order is stuck:** the run's Failures table names the step; fix the data,
then `api.requeue_order(sales_order)` clears the quarantine and puts it at the
head of the cursor.

**When a payout does not appear:** upload the merchant's `Settlement_Logs_*.csv`
on `PostEx Settlement Upload`, set **Reconcile From** to the day after the last
CPR already reconciled, press **Check**, read the Exceptions table, then
**Apply**. The API's `settle` flag is the only automatic signal and it can stay
`false` for an order a CPR has paid.

**When a return cost looks missing:** the API returns `transactionFee: 0` for a
returned parcel; the cost lives in `reversalFee`/`reversalTax` (which the app
does book) and in the settlement log (`FEE` + `GST`, which the CSV path books).
`return_cost_missing` in the run summary lists any parcel the poll could not
price.

---

## 12. Deliberate limits

* **Imported history is frozen.** Orders with `custom_shopify_state` are out of
  scope for every helper, so the app can never create stock or GL documents for
  a period that was reconciled by hand. On this site that is 1,719 of 2,121
  Sales Orders.
* **45-day late-return window.** After that a parcel's outcome is final.
* **`outstanding ≤ 0` means "settled elsewhere".** Charges still book, but no
  cash moves and the invoice is only flagged Paid.
* **No partial-payment booking.** A payout that does not cover the COD is
  reported, because settling the invoice books the whole outstanding.
* **The webhook never creates documents**, so there is exactly one writer for
  invoices and stock.

---

## 13. File map

| file | lines | job |
|---|---|---|
| `stock.py` | 1,226 | pickup / delivery / invoice / return / terminal documents |
| `booking.py` | 871 | create, cancel, link a PostEx booking; `po_no` bridge |
| `sync.py` | 855 | feed pull, status mapping, cursors, run record |
| `api.py` | 843 | whitelisted endpoints (buttons, manual actions, CSV upload) |
| `settlement.py` | 660 | charges, withheld tax, payout, `payment_state` consumers |
| `fields.py` | 419 | custom-field definitions (created only when missing) |
| `settlement_log.py` | 375 | Settlement Logs CSV parse + reconcile |
| `utils.py` | 362 | `claim_field`, quarantine, failure rows, mapping helpers |
| `config.py` | 356 | account resolution + role → account/warehouse mapping |
| `tasks.py` | 337 | cron entry points, per-account file locks |
| `migrate.py` | 310 | legacy `PostEx Settings` import, default provisioning |
| `constants.py` | 265 | status vocabulary, code maps, per-run limits |
| `client.py` | 264 | HTTP client: pacing, budget, retries, typed calls |
| `webhook.py` | 211 | guest webhook endpoint (status only) |
| `backfill.py` | 159 | assign accounts to existing orders |
| `scope.py` | 123 | scan-scope predicate used by sync |
| `install.py` / `hooks.py` | 99 / 83 | lifecycle, scheduler, `doctype_js` |

---

## 14. Tests and deploy

```bash
# outside a bench (the repo ships a minimal frappe stub for this)
cd postex_integration && PYTHONPATH=../.teststub python3 -m pytest postex_integration/tests -q   # 119 pass
```

Deploy = push `main`, deploy the app on Frappe Cloud, then verify:

```
/api/method/postex_integration.api.health   →  build + feature flags
```

`health()` lists a flag per behavioural guarantee (`settlement_log_upload`,
`payment_state_on_all_legs`, `credit_note_for_dn_invoices`, `tracking_uniqueness_guard`, …)
so "is the fix live?" is one HTTP call, not a guess.

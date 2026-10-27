# PostEx Integration (Frappe app)

Multi-company, multi-account PostEx courier integration for ERPNext.

Replaces the single-company Server Script integration with a proper app:

| | Server scripts (old) | This app |
|---|---|---|
| Accounts | hard-coded `- AN` names | `PostEx Account` + `PostEx Account Mapping` |
| Company | only `so.company` was honoured | every query and document is company-scoped |
| PostEx accounts | one token for the whole site | one `PostEx Account` per merchant account, several per company |
| Structuring | 7 scripts chained via `run_script()` | Python modules + hooks |
| Locking | none (`frappe.cache` unavailable in `safe_exec`) | per-account `filelock` |
| Webhook | one unguessable URL | one URL + secret per account |
| History | `last_run_result` text only | `PostEx Sync Run`, `PostEx Booking Log`, `PostEx Webhook Log` |

`architecture.md` documents the design; this README documents **how to install, configure
and migrate**. Decisions that were open in `architecture.md` §17 are recorded in
[`architecture.md`](architecture.md#19-implementation-decisions).

---

## 1. Install

```bash
cd /path/to/frappe-bench
bench get-app postex_integration /path/to/postex_integration   # or a git URL
bench --site <site> install-app postex_integration
```

`before_install` adopts a pre-existing UI-created `PostEx Webhook Log` doctype (module
`Custom`) so its rows are kept; `after_install` creates the custom fields and prints the
next steps. Custom fields are **created only when missing** — an existing site keeps its
labels, positions and list view settings untouched.

No fixtures are shipped, so `bench migrate` never rewrites desk customisations.

---

## 2. Configure

### 2.1 One `PostEx Account` per merchant account

`PostEx Integration > PostEx Account > New`

| Field | Meaning |
|---|---|
| `account_name` | free text, unique per company |
| `company` | which ERPNext company this merchant account belongs to |
| `is_default` | the account used when a document has none set (one per company; **required** when a company has more than one enabled account) |
| `enabled` | master switch: stops **all** flows for this account |
| `enable_sync` | status/tracking/settlement sync |
| `enable_auto_booking` | book on Sales Order submit (off by default — see §5) |
| `dry_run` | shadow mode: sync status/fees, create no documents |
| `api_token` | Password field, encrypted |
| `webhook_secret` | Password field, per account |
| `withholding_percent` | default 4 (2% ITWH + 2% STWH) |
| `sync_window_days` | default 60 (older orders fall back to `track-order`) |
| `pickup_address`, `pickup_city` | booking payload |
| `default_order_detail`, `default_weight` | booking fallbacks |

A company can have **several** accounts, but then exactly one **must** be flagged `is_default`:
flows that are not given an explicit account (auto booking, unassigned orders falling into
sync) use it, and if several are enabled with none flagged they refuse to run instead of
guessing. `Sales Order.custom_postex_account` / `Sales Invoice.custom_postex_account` record
which account booked and settles that document; on the Sales Order the field is selectable
(filtered to the order's company) and is locked once a tracking number exists, because
changing it then would misattribute the settlement.

### 2.2 Account Mapping (child table)

Nothing financial is hard-coded: the user picks accounts/warehouses, the code resolves them
per account.

| Role | Required at run time | Requirement |
|---|---|---|
| Courier Finance | yes | leaf Asset with `account_type` Bank/Cash — the courier wallet |
| Debtors | yes | leaf Asset, `account_type` Receivable |
| Delivery Charges | yes | leaf Expense |
| Tax Withheld | yes | leaf Asset (or Expense) |
| Unsettled COD | yes | leaf Asset |
| Unaccounted | no | leaf Liability (or Asset) |
| In-Transit Warehouse | yes | leaf warehouse, `warehouse_type = Transit` |
| Stores Warehouse | yes | leaf warehouse, not Transit |
| Cost Center | no | leaf cost center (falls back to the company's default) |

Validation on save (blocks):

1. account/warehouse/cost center must belong to the account's company;
2. must be a leaf (not a group);
3. root-type / account-type must match the role;
4. the same account cannot be mapped by two **different companies**;
5. `In-Transit` must be a Transit warehouse and differ from `Stores`.

Missing required roles are **warnings** (so a half-configured account can be saved and then
provisioned); the flow that needs a missing role refuses to run with a clear message.

### 2.3 Provisioning shortcut

On a saved account: **Create default accounts** (`api.create_default_accounts`) creates with
the company abbreviation:

`Postex Finance - <ABBR>`, `Delivery Charges - <ABBR>`,
`PostEx Tax Withheld (Advance Tax) - <ABBR>`, `PostEx Unsettled COD - <ABBR>`,
`PostEx Unaccounted Amount - <ABBR>`, warehouse `PostEx In Transit - <ABBR>`,
and maps `Debtors`/`Stores` from the company defaults. Existing mappings are never overwritten.

### 2.4 Webhook

**Show webhook URL** returns

```
https://<site>/api/method/postex_integration.webhook.receive/<account>/<secret>
```

Register it per account in the PostEx dashboard. The secret is compared in constant time;
the endpoint is guest-callable because PostEx cannot log in as an ERPNext user. If the
dashboard mangles path segments, the same endpoint also accepts query parameters:
`…/webhook.receive?account=<account>&secret=<secret>`.

---

## 3. What runs, and when (`hooks.py`)

| Job | Frequency | Function |
|---|---|---|
| Status sync | `*/15 * * * *` | `tasks.sync_all_accounts` → enqueues `sync.sync_account` per account |
| Settlement | `*/30 * * * *` | `tasks.settle_all_accounts` → enqueues `settlement.reconcile_account` per account |
| Log pruning | daily | `tasks.prune_logs` (90 days) |
| Auto booking | on Sales Order submit | `booking.on_sales_order_submit` |

Each enqueued job takes `filelock("postex-sync-<account>")` / `postex-settle-<account>`, so
cron and a manual "Sync now" can never process the same account concurrently. Document
creation is additionally guarded by **verify-after-create**: after inserting a document the
Sales Order's link field is re-read, and if another run stored a different document, ours is
cancelled/deleted and the event logged.

---

## 4. Flows

```
booking      : add-order                         -> tracking, status, account
sync         : get-all-order + get-unbooked      -> tracking, status, fees, account claim
pickup       : Stores -> In-Transit              -> Material Transfer
delivered    : In-Transit -> customer            -> Delivery Note -> Sales Invoice
returned     : In-Transit -> Stores              -> Material Transfer + courier cost JE
settlement   : payment-status/{tracking}         -> charges JE + withholding JE + Payment Entry
```

Accounting shapes (all accounts resolved from the mapping):

| Flow | Debit | Credit |
|---|---|---|
| Delivery invoice | Debtors | Sales |
| Settlement — fee + GST | Delivery Charges | Debtors (party + invoice) |
| Settlement — withholding | Tax Withheld | Debtors (party + invoice) |
| Settlement — payout | Courier Finance | Debtors |
| Return leg (no invoice exists) | Delivery Charges | Courier Finance |

Policy: **delivered ⇒ invoice, returned ⇒ no invoice.** A returned parcel still costs the
delivery attempt plus the reverse leg, so that cost is credited to the wallet.

### Multi-account ownership rules

1. Booking stamps `custom_postex_account` on the Sales Order.
2. Sync only considers orders of the account's company that are either assigned to the
   account or unassigned. An unassigned order is claimed by an account only when its
   reference actually appears in that account's PostEx feed (each feed contains only its
   own orders), and the claim is a conditional `UPDATE … WHERE custom_postex_account IS NULL`.
3. Stock/revenue/settlement helpers require `custom_postex_account = <this account>`, so two
   accounts of one company can never act on the same order.
4. Webhook matches `<account, tracking>` first, then an *unassigned* order of the same
   company.

---

## 5. Auto booking

Off by default. When on, a submitted Sales Order whose courier is blank or `PostEx`, that has
no tracking and is not a historical import, is booked in a background job — on the company's
**default** account (if a company has several enabled accounts and none flagged default, the
job refuses to run rather than pick one). The other OMS may already book orders, so enable
this only when ERPNext is the booking system.

Manual booking: the **Create PostEx Booking** button in the Courier Detail section (visible
only when courier is blank/PostEx and no tracking exists). If the order already carries a
`PostEx Account` it is used as-is; if the company has exactly one account, that one is used;
if it has several, the button asks which account to book on. Every attempt is recorded in
`PostEx Booking Log`.

---

## 6. Migration from the Server Scripts

Run in this order on a copy/staging site first.

1. **Freeze** — disable the scheduler Server Script and the `Scheduled Job Type`:
   `bench --site <site> set-config pause_scheduler 1` (or delete the
   `Scheduled Job Type` row named `postex_order_status_sync_cron`).
2. Install the app (§1).
3. Create the `PostEx Account` for the existing company. Fastest path:

   ```bash
   bench --site <site> execute postex_integration.migrate.import_legacy_settings
   ```

   This copies `PostEx Settings` (token, base URL, flags, weight) into a `PostEx Account`
   per company and maps the existing `- <ABBR>` accounts/warehouses. It does not delete or
   modify the legacy Single doctype.
4. Fill anything the report lists as missing, then click **Create default accounts** and
   verify the mapping table.
5. Set the webhook secret and register the new URL. Keep the old webhook script enabled
   during the shadow period if you prefer.
6. **Shadow period**: enable the app's scheduler (optionally with `dry_run` on the account,
   which syncs tracking/status/fees but creates no documents), keep the scripts disabled, and
   compare wallet / invoice / receivable totals daily. The 15-minute sync plus 30-minute
   settlement should produce no new documents for old orders (`custom_shopify_state` is
   excluded, and the app's filters otherwise mirror the scripts).
7. Disable the old **Client Scripts** (their behaviour is now in the app's `doctype_js`):
   `Sales Order PostEx Booking`, `Sales Order Courier Detail`, `Sales Invoice Courier Detail`.
   The list-colour scripts (`Sales Order PostEx Colors`, `Sales Invoice PostEx Colors`) are
   pure display and can stay.
8. Delete the 7 Server Scripts and the `Scheduled Job Type` row(s), then re-enable the
   scheduler. Keep `PostEx Settings` (read-only) until you are satisfied, then delete it.

Rollback: disable the app's accounts (`enabled = 0`) and re-enable the Server Scripts.

> **Backfill note.** Old orders that predate the app have no `custom_postex_account`. Running
> the sync once claims them from the PostEx feed, so account attribution is fixed
> automatically. `custom_shopify_state` orders are still excluded from stock/invoice helpers.

---

## 7. Endpoints

| Method | Purpose |
|---|---|
| `postex_integration.api.sync_now(account=None, company=None, background=0)` | manual sync |
| `postex_integration.api.reconcile_payouts(account=None, company=None)` | manual settlement |
| `postex_integration.api.run_all(company=None)` | sync + settle |
| `postex_integration.api.book_order(sales_order, account=None)` | manual booking (button) |
| `postex_integration.api.get_booking_accounts(sales_order)` | enabled accounts of the order's company, for the booking dialog |
| `postex_integration.api.assign_account(sales_order, account)` | assign an order to an account |
| `postex_integration.api.create_default_accounts(account)` | provision mapping |
| `postex_integration.api.migrate_legacy_settings(company=None)` | legacy import |
| `postex_integration.api.webhook_url(account)` | webhook endpoint with secret |
| `postex_integration.webhook.receive(account, secret)` | **guest**, per-account secret |

Every write endpoint resolves the account from the document's company; none accept an
arbitrary account id for a document write.

---

## 8. Known gaps / next steps

- Reports (`Courier Unpaid Invoices`, `Courier Payment Summary`, `Approx Settled Invoices`,
  `Orders/Invoices Without Courier`) are still the single-company ones and need a **Company**
  filter + grouping by `custom_postex_account`.
- `PostEx Unsettled COD` / `PostEx Unaccounted Amount` are mapped but no automatic
  reconciliation job writes them yet (the historical backlog was reconciled manually).
- Quick Delivery and Fast And Quick are intentionally out of scope (PostEx only).
- `PostEx Settings` remains on the site as a read-only legacy record until deleted.

---

## 9. Tests

```bash
bench --site <site> run-tests --app postex_integration
```

`tests/test_status_mapping.py` covers the status/history mapping table, which is where the
old implementation collected the most bugs (specific needles before generic ones; history
only as a fallback).

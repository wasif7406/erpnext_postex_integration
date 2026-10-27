# PostEx Integration — Multi-Company Architecture

Status: design + implementation. The app lives in this repository
(`postex_integration/`); see `README.md` for install, configuration and migration.
Scope: replacing the single-company Server Script integration with a
GitHub-installed Frappe app that supports **multiple PostEx accounts, possibly
several per Company**, on the same ERPNext site.

---

## 1. Purpose

Run several ERPNext Companies on one site, each with **its own PostEx account**
(its own token, pickup address and money), without any cross-posting:

- Company A sells product A → ships on PostEx account A
- Company B sells product B → ships on PostEx account B

Each company keeps its own chart of accounts, warehouses, customers' receivable,
courier wallet, tax withheld, and settlement reconciliation.

---

## 2. Non-goals

- Moving stock between companies (intercompany stock transfer is out of scope).
- Multi-currency PostEx settlement (assume PKR only).
- Courier aggregation/consolidated statements across companies.
- Replacing the Shopify connector.

---

## 3. Today's state (baseline)

| Area | Current implementation |
|---|---|
| Settings | `PostEx Settings` is a **Single** doctype: one token, one base URL, one pickup address, one `enable_auto_booking` |
| Logic | 7 Server Scripts (`PostEx Sync Core`, `PostEx Order Status Sync` scheduler, `PostEx Sync Now 2`, `PostEx Pickup Transfer`, `PostEx Delivery & Return`, `PostEx Payout Reconciliation`, `PostEx Webhook`, plus `PostEx Book Order`) |
| Accounts | hard-coded `- AN` names inside the scripts |
| Warehouses | hard-coded `PostEx In Transit - AN`, `Stores - AN` |
| Cost center | hard-coded `Main - AN` |
| Company | taken from the document (`so.company`) — the only company-aware part |
| Scheduler | one `Scheduled Job Type` for the scheduler script (history: an orphan duplicate had to be removed) |
| Concurrency | "verify-after-create" guards; no real locks (Redis unavailable inside `safe_exec`) |

Hard-coded values that block multi-company:

| Constant | Used in |
|---|---|
| `Postex Finance - AN` | Delivery & Return, Payout |
| `Debtors - AN` | Payout |
| `Delivery Charges - AN` | Delivery & Return, Payout |
| `PostEx Tax Withheld (Advance Tax) - AN` | Payout |
| `PostEx Unsettled COD - AN` / `PostEx Unaccounted Amount - AN` | reconciliation JEs |
| `PostEx In Transit - AN`, `Stores - AN` | Pickup, Delivery & Return |
| `Main - AN` | every JE |

---

## 4. Target model

```
Company A ──1:N──> PostEx Account A1 ──> token A1, pickup A1, accounts A1, warehouses A1
                └─ PostEx Account A2 ──> token A2, pickup A2, accounts A2, warehouses A2
Company B ──1:N──> PostEx Account B1 ──> token B1, pickup B1, accounts B1, warehouses B1
```

Rules:

1. **A Company may have several PostEx Accounts**, but then exactly one must be
   `is_default` (used when a document has no account set). `(company,
   account_name)` is unique. If several are enabled and none is flagged, the
   resolver aborts with a clear error instead of picking one arbitrarily.
2. Every document created by the integration belongs to the company of the
   Sales Order being processed — never a hard-coded one.
3. A Sync run for account A1 only reads/writes Sales Orders where
   `Sales Order.company == A` **and** the order is assigned to A1 or still
   unassigned. An unassigned order is claimed by the account whose PostEx feed
   actually contains its reference (`UPDATE … WHERE custom_postex_account IS NULL`).
4. Stock/revenue/settlement helpers require `custom_postex_account == that
   account`, so two accounts of one company can never act on the same order.
5. Account/warehouse resolution always goes through the mapping, never through
   a constant.
6. Disabling an account (or `enable_sync = 0`) stops every flow for that account
   without touching the other.

---

## 5. DocType design

### 5.1 `PostEx Account` (new, regular doctype — not Single)

One record per company.

| Field | Type | Notes |
|---|---|---|
| `company` | Link → Company | **mandatory**; `(company, account_name)` unique, several accounts per company allowed |
| `account_name` | Data | mandatory, human label |
| `is_default` | Check | the account used when a document has none set (one per company) |
| `enabled` | Check | master switch |
| `enable_sync` | Check | status/tracking sync |
| `enable_auto_booking` | Check, default 0 | booking on new Sales Orders |
| `api_token` | Password | per company |
| `base_url` | Data | default `https://api.postex.pk` |
| `merchant_name` | Data | informational |
| `withholding_percent` | Float (4) | advance tax withheld by the courier |
| `sync_window_days` | Int (60) | list window; older orders use `track-order` |
| `pickup_address` | Small Text | booking payload |
| `pickup_city` | Data | booking payload |
| `default_order_detail` | Data | booking fallback |
| `default_weight` | Float (0.5) | booking fallback |
| `webhook_secret` | Password | per-account webhook endpoint |
| `last_sync_on` | Datetime | per company |
| `last_run_result` | Small Text | per company log line |
| `mapping` | Table → `PostEx Account Mapping` | financial/stock mapping |

### 5.2 `PostEx Account Mapping` (child table)

| Field | Type | Notes |
|---|---|---|
| `role` | Select: `Courier Finance`, `Debtors`, `Delivery Charges`, `Tax Withheld`, `Unsettled COD`, `Unaccounted`, `In-Transit Warehouse`, `Stores Warehouse`, `Cost Center` | one row per role |
| `account` | Link → Account (filtered: same company, leaf) | filled for financial roles |
| `warehouse` | Link → Warehouse (filtered: same company, leaf) | filled for stock roles |
| `cost_center` | Link → Cost Center (filtered: same company, leaf) | filled for the Cost Center role |

`account` / `warehouse` / `cost_center` are the **user's selection** — the user
creates the account in ERPNext and picks it here. Exactly one of the three is
filled per row. Full dropdown behaviour, validation and the per-role account
requirements are in **§6A**.

Alternative (simpler): flat fields on `PostEx Account`
(`finance_account`, `debtors_account`, `delivery_charges_account`,
`tax_withheld_account`, `unsettled_cod_account`, `unaccounted_account`,
`in_transit_warehouse`, `stores_warehouse`, `cost_center`).
A child table is preferred because it makes validation and future roles easier;
flat fields are easier to read. **Decision required — see §17.**

### 5.3 `PostEx Booking Log` (optional, recommended)

One row per booking attempt (manual or automatic): company, sales order,
reference number, payload hash, HTTP status, response, tracking returned,
user, timestamp. Gives an audit trail and makes duplicate-booking analysis
possible.

### 5.4 Reuse of existing custom fields

The current Sales Order / Sales Invoice fields stay as they are (they are
company-agnostic by fieldname):

- Sales Order: `custom_courier`, `custom_postex_status`,
  `custom_postex_tracking_number`, `custom_postex_last_synced_on`,
  `custom_postex_delivery_fee/tax`, `custom_postex_reversal_fee/tax`,
  `custom_qd_*`, `custom_fq_*`, `custom_postex_pickup_entry`,
  `custom_postex_delivery_note`, `custom_postex_sales_invoice`,
  `custom_postex_return_entry`, `custom_postex_return_charges_entry`,
  `custom_postex_payout_entry`, `custom_postex_charges_entry`,
  `custom_postex_settled_on`, `custom_create_postex_booking` (button)
- Sales Invoice: same courier/status/tracking/payment fields.

New field recommended: `custom_postex_account` (Link → PostEx Account) on both
Sales Order and Sales Invoice, so every document records **which account**
booked/settled it. This makes multi-account reconciliation unambiguous even if
two accounts issue overlapping tracking ranges.

---

## 6. Resolution rules

```
settings   = PostEx Account where company = doc.company
accounts   = settings.mapping[role].account
warehouse  = settings.mapping[role].warehouse
cost       = settings.mapping["Cost Center"].cost_center or company.cost_center
```

Fallbacks (in order):

1. mapping row for the company,
2. company defaults (`company.default_*`) where ERPNext provides one,
3. abort with a clear error — **never** fall back to another company's account.

Validation at save time of `PostEx Account`: every mapped account must belong
to the same company, every warehouse must belong to the same company, and no
two companies may share a mapped account.

---

## 6A. Account assignment in Settings (what the user picks)

The **whole point** of `PostEx Account` is that nothing financial is hard-coded:
the user creates (or reuses) accounts and warehouses in ERPNext, then **selects
them from dropdowns** in the settings. Every script resolves the role → the
account the user picked, for that company.

### 6A.1 The role table (all fields are Link dropdowns)

| Role | Required | Field type in settings | ERPNext account requirement | Used by |
|---|---|---|---|---|
| **Courier Finance** | ✅ | Link → Account | root **Asset**, account_type **Bank** (or Cash) — the courier wallet | every payout PE (Dr net received) |
| **Debtors** | ✅ | Link → Account | account_type **Receivable** (party required) | fee JE (Cr), withholding JE (Cr), payout PE (Cr) |
| **Delivery Charges** | ✅ | Link → Account | root **Expense** (e.g. under Direct Expenses) | delivery fee + GST, return costs |
| **Tax Withheld** | ✅ | Link → Account | root **Asset** (advance tax) or Expense if the merchant prefers | 4% ITWH+STWH booked at settlement |
| **Unsettled COD** | ✅ | Link → Account | root **Asset**, no account_type | delivered-but-unpaid invoices (money parked with courier) |
| **Unaccounted** | ⚠️ optional | Link → Account | root **Liability** (contra) | offsets statement credits not yet tied to invoices; omit only if never reconciling |
| **In-Transit Warehouse** | ✅ | Link → Warehouse | same company, leaf, `warehouse_type = Transit` | pickup transfer destination, delivery note source |
| **Stores Warehouse** | ✅ | Link → Warehouse | same company, leaf, not transit | pickup source, return destination |
| **Cost Center** | ⚠️ optional | Link → Cost Center | same company | applied to every JE line when set |

For each row also store `account` **or** `warehouse` (only one is filled, per
role type) — the child table keeps both columns and validates that exactly one
is set.

### 6A.2 Dropdown behaviour

- All Link fields are filtered by the **Company** on the settings doc:
  - accounts: `company = <company>` and `is_group = 0` (leaf only),
  - warehouses: `company = <company>` and `is_group = 0`,
  - cost centers: `company = <company>` and `is_group = 0`.
- Changing Company re-filters every dropdown (client-side).
- Each Link shows the account number + name so two similarly-named accounts are
  distinguishable.
- The account's `root_type` / `account_type` is validated on save (see 6A.3),
  so a Receivable can never be picked as Courier Finance by accident.

### 6A.3 Validation on save

Hard errors (block save):

1. `company` missing or a second `PostEx Account` already exists for it.
2. Any mapped account / warehouse / cost center belongs to a different company.
3. Any mapped account is a **group** account (must be a leaf).
4. Root-type mismatch for the role (Asset vs Expense vs Liability as per 6A.1).
5. `Courier Finance` not of type Bank/Cash; `Debtors` not Receivable.
6. In-Transit warehouse `warehouse_type != "Transit"`, or equals the Stores
   warehouse.
7. The same account mapped to two different roles **inside the same company**
   (warn, not block — sometimes intentional).
8. The same account mapped by two different companies (block).

Warnings (allow but flag):

- `Unaccounted` left empty → reconciliation JEs will fail and be logged.
- `Tax Withheld` left empty → settlement will skip withholding (and the wallet
  will drift from the statement).
- `enable_auto_booking` on while `pickup_address`/`pickup_city` are empty.

### 6A.4 Default provisioning for a new company

On creating a `PostEx Account`, offer a **"Create default accounts"** action
that provisions, with the company's abbreviation:

`Postex Finance - <ABBR>`, `Delivery Charges - <ABBR>`,
`PostEx Tax Withheld (Advance Tax) - <ABBR>`, `PostEx Unsettled COD - <ABBR>`,
`PostEx Unaccounted Amount - <ABBR>`, warehouse `PostEx In Transit - <ABBR>`,
and maps **Debtors** and **Stores** from the company defaults.

That makes onboarding company B a two-click job instead of hunting for the right
accounts — while still letting the user override any dropdown afterwards.

### 6A.5 Which role is hit by which flow

| Flow | Debit | Credit |
|---|---|---|
| Pickup transfer | *(stock only)* Stores → In-Transit | — |
| Delivery Note | *(stock only)* In-Transit → customer | — |
| Delivery invoice | Debtors | Sales |
| Delivery fee JE | Delivery Charges | Debtors |
| Return transfer | *(stock only)* In-Transit → Stores | — |
| Return cost JE | Delivery Charges | Courier Finance |
| Settlement — fee + GST | Delivery Charges | Debtors |
| Settlement — 4% withheld | Tax Withheld | Debtors |
| Settlement — payout | Courier Finance | Debtors |
| Unpaid-but-delivered reclass | Unsettled COD | Debtors (or Courier Finance) |
| Statement credits not tied to invoices | Courier Finance / Unsettled COD | Unaccounted |

---

## 7. Business flows (per company)

### 7.1 Booking

- **Manual**: button `Create PostEx Booking` on the Sales Order (already
  exists) → resolves settings by `so.company` → POST `add-order` → writes
  tracking/status/courier/account back. When the company has more than one
  enabled account the desk asks which one to use (the order's existing
  `custom_postex_account` wins if set).
- **Automatic**: on `Sales Order.on_submit` (app hook) when
  `settings.enable_auto_booking`; fallback sweep in the sync for orders created
  while the switch was off.
- Guard: never book twice (tracking present, or a Booking Log row already
  successful for that SO).

### 7.2 Status sync

Per account, in one scheduled run:

1. Pull `get-all-order` with that account's token.
2. Upsert only Sales Orders with `company == account.company`.
3. Update tracking / status / fees / reversal fees / last synced.
4. Optional: book missing orders when `enable_auto_booking`.
5. Write a per-company summary into `PostEx Account.last_run_result`.

### 7.3 Pickup transfer

For each company: submitted SO with status that implies pickup, no
`custom_postex_pickup_entry`, `custom_shopify_state` empty →
Material Transfer `Stores` → `PostEx In Transit` (that company's warehouses).

### 7.4 Delivery → Delivery Note → Sales Invoice

`Delivered` + pickup entry + no delivery note:
1. create + submit Delivery Note from `In-Transit` warehouse,
2. then create + submit Sales Invoice (accrual),
3. book fee (+GST) as `Dr Delivery Charges / Cr Debtors` against the invoice,
4. record `custom_postex_account`.

The 4% withholding is booked at **settlement**, not at delivery (see 7.6).

### 7.5 Return

`Returned` + pickup entry + no delivery note + no return entry:
1. Material Transfer `In-Transit` → `Stores`,
2. book the return cost (`delivery_fee + delivery_tax + reversalFee +
   reversalTax`) as `Dr Delivery Charges / Cr Courier Finance` (the wallet takes
   the hit — no invoice exists),
3. no invoice/credit note (policy: delivered ⇒ invoice; returned ⇒ no invoice).

### 7.6 Settlement / payout reconciliation

Per account, for delivered+invoiced orders without `custom_postex_settled_on`:

1. `payment-status/{tracking}` → `settle`, `settlementDate`, `cpr1`.
2. When settled:
   - JE: `Dr Delivery Charges` (fee + GST) **and** `Dr Tax Withheld` (4% of
     invoice total) / `Cr Debtors` (party + invoice reference),
   - PE: receive net into the company's `Courier Finance` account, allocated to
     the invoice,
   - write `custom_postex_settled_on`, `custom_postex_payout_entry`,
     `custom_postex_charges_entry`, payment status `Paid`.
3. When not settled: leave the invoice `Not Paid`.
4. The account's `Courier Finance` balance must always equal the PostEx wallet
   statement for that account.

### 7.7 Webhook

- One endpoint per account: `/api/method/<app>.webhook/<account_id>/<secret>`,
  or a single endpoint that resolves the account by tracking number then
  company.
- Updates status only. **Never** creates invoices or accounting entries
  (keeps a single writer for documents).
- Writes to `PostEx Webhook Log` with the account and company.

---

## 8. Idempotency and concurrency

| Risk | Mitigation |
|---|---|
| Two sync runs create two DNs / invoices / transfers / PEs | (a) **one distributed lock per account** (`frappe.utils.synchronization.filelock`) so one account is processed by one worker at a time; (b) "verify-after-create": store the created doc on the SO link field, re-read it, cancel own copy if another run won; (c) unique constraint on Booking Log (sales order + success) |
| Booking twice | check tracking + Booking Log before POST; store the returned tracking immediately |
| Duplicate Payment Entry | one PE per (CPR, settlement date, customer, company); unique reference number check before insert |
| Same tracking used by two SOs | validate uniqueness of `custom_postex_tracking_number` per company at write time |
| Scheduler duplicates | scheduled jobs declared in `hooks.py`; no `Scheduled Job Type` rows to leave behind |
| Partial failure mid-flow | one transaction per sales order (`frappe.db.savepoint` / rollback on error); failures logged and retried next run |

Server Scripts could not do (a) because `frappe.cache` is unavailable in
`safe_exec`; the app can.

---

## 9. Scheduled jobs (hooks.py)

| Job | Frequency | Notes |
|---|---|---|
| `sync_all_accounts` | every 15 minutes | loops enabled accounts; enqueues one background job per account |
| `payout_reconciliation` | every 30–60 minutes | per account |
| `booking_sweep` | hourly | optional catch-up when auto-booking is on |

Each job:

- acquires the account lock,
- checks `enabled` / `enable_sync`,
- writes `last_sync_on` + `last_run_result` on the account.

---

## 10. API endpoints (whitelisted, login required unless noted)

| Method | Purpose |
|---|---|
| `postex_integration.api.sync_now(company)` | manual sync for one company |
| `postex_integration.api.book_order(sales_order)` | manual booking (button) |
| `postex_integration.api.reconcile_payouts(company)` | manual settlement run |
| `postex_integration.webhook.receive(account, secret)` | **guest**, per-account secret |

Every endpoint resolves the account from the document's company; none accept an
account id from the client for write operations.

---

## 11. Permissions and security

- `PostEx Account` readable by System Manager / Accounts Manager; writable by
  System Manager only. Tokens are `Password` fields (encrypted).
- Company-level row permissions: a user with access to company A must not see
  company B's account row (use User Permissions on Company).
- Webhook: per-account secret, constant-time compare, IP allow-list optional.
- All created documents set `ignore_permissions` only inside the app, with the
  acting user recorded in `owner` as `Administrator` and the reason in remarks.

---

## 12. Reports

Existing query reports keep working but need a `company` filter:

| Report | Change |
|---|---|
| `Courier Unpaid Invoices` | add Company filter + column |
| `Courier Payment Summary` | group by Company, Courier, Payment status |
| `Approx Settled Invoices` | add Company |
| `Orders Without Courier` / `Invoices Without Courier` | add Company |
| new: `PostEx Wallet by Company` | wallet balance vs statement per account |
| new: `PostEx Settlement Reconciliation` | CPR × invoice × fee × withholding |

All reports must filter by the user's permitted companies.

---

## 13. Error handling and observability

- Every external call wrapped; failures written to `Error Log` with company,
  sales order, tracking and payload hash.
- `PostEx Account.last_run_result` keeps a one-line summary per company
  (`orders seen / filled / statuses / booked / failed`).
- Optional `PostEx Sync Run` doctype (start, end, company, counters, error
  count) for a run history instead of a single text line.

---

## 14. Migration plan (from Server Scripts)

1. **Freeze** the current scripts as reference; stop the scheduler script.
2. Create the app skeleton, DocTypes and fixtures (custom fields) — no logic.
3. Populate `PostEx Account` for the existing company (Aqreno Nova) with the
   current accounts/warehouses.
4. Port in this order, each verified on the existing company before proceeding:
   1. settings + manual booking
   2. status sync (read-only effects)
   3. pickup transfer
   4. delivery note + invoice + fee
   5. return leg
   6. payout + withholding
   7. webhook
5. Run a **shadow period**: app writes, scripts disabled; compare
   wallet/invoice/receivable totals daily.
6. Delete the Server Scripts and mark them in `AGENTS.md` as replaced.
7. Only then create company B + PostEx Account B (config only, no code).

Rollback: re-enable the scheduler script; the app's schedulers are disabled per
account via `enabled = 0`.

---

## 15. Testing strategy

- **Unit**: status mapping table (API strings → ERPNext statuses), fee and
  withholding calculation, payload builder.
- **Integration** (on a test site): book → pickup → deliver → invoice →
  settle → return, asserting AR, wallet, stock and tax-withheld balances.
- **Multi-company test**: same scenario on company A and B in one site,
  asserting no document of A touches an account of B.
- **Idempotency test**: run the same sync twice concurrently; assert exactly one
  DN/invoice/PE.
- **Reconciliation test**: replay a real settlement CSV and assert the wallet
  equals the statement.

---

## 16. Deployment

- GitHub repo `postex_integration` (Frappe app, `bench get-app`).
- Frappe Cloud private bench: add the app to the bench, `install-app` per site,
  deploy via dashboard; migrations run on deploy.
- `after_install` creates custom fields, the default `PostEx Account` row for
  the existing company, and the two Client Scripts (or ships them as
  `doctype_js` + fixtures).
- Version tags for each release; rollback = deploy the previous tag.

---

## 17. Decisions (were open in the first draft)

1. Mapping storage → **child table** `PostEx Account Mapping` (validated per role).
2. Partial invoicing policy → **one invoice per delivery note** (unchanged).
3. 4% withholding → booked at **settlement**, percent configurable per account.
4. Run history → `last_run_result` **plus** a `PostEx Sync Run` doctype.
5. Webhook → **one URL + secret per account**.
6. Quick Delivery / Fast And Quick → **out of scope**; PostEx only.
7. Company B → same site (multi-account supported), a separate site also works.

---

## 18. Appendix — current values to map for company A (Aqreno Nova)

These are the values to **select in the settings dropdowns** for company A
(nothing is hard-coded in the app; they are rows of `PostEx Account Mapping`):

| Role | Current value | Dropdown source |
|---|---|---|
| Courier Finance | `Postex Finance - AN` | Account, company = Aqreno Nova, leaf, Bank |
| Debtors | `Debtors - AN` | Account, Receivable |
| Delivery Charges | `Delivery Charges - AN` | Account, Expense |
| Tax Withheld | `PostEx Tax Withheld (Advance Tax) - AN` | Account, Asset |
| Unsettled COD | `PostEx Unsettled COD - AN` | Account, Asset |
| Unaccounted | `PostEx Unaccounted Amount - AN` | Account, Liability |
| In-Transit Warehouse | `PostEx In Transit - AN` | Warehouse, Transit |
| Stores Warehouse | `Stores - AN` | Warehouse, leaf |
| Cost Center | `Main - AN` | Cost Center, leaf |

For company B the user creates (or provisions with **Create default accounts**)
the equivalents with company B's abbreviation and picks them in company B's
`PostEx Account` — no code change, only configuration.

---

## 19. Implementation map

Everything in §1–§18 is implemented except the items listed under *Deferred*.

| Concern | Module |
|---|---|
| DocTypes | `postex_integration/postex_integration/doctype/{postex_account, postex_account_mapping, postex_booking_log, postex_sync_run, postex_webhook_log}` |
| Constants (status table, endpoints, roles) | `constants.py` |
| Account + mapping resolution, validation | `config.py` |
| HTTP client (one token per account) | `client.py` |
| Booking / auto-booking hook / sweep | `booking.py` |
| Status-tracking-fee sync, run records | `sync.py` |
| Pickup, delivery note, invoice, return | `stock.py` |
| Settlement (charges, withholding, payout) | `settlement.py` |
| Guest webhook (per-account secret) | `webhook.py` |
| Cron entry points, per-account filelock | `tasks.py` |
| Whitenlisted API | `api.py` |
| Custom fields (create-if-missing) | `fields.py` |
| Install hooks, legacy doctype adoption | `install.py` |
| Legacy settings import, provisioning | `migrate.py` |
| Desk behaviour | `public/js/{sales_order, sales_invoice, postex_account}.js` |

Porting order from §14 is satisfied: settings/booking → sync → pickup → delivery
→ return → settlement → webhook, each scoped by account.

### Deviations from the first draft

- **Missing mapping roles are warnings, not save-blocking errors.** Blocking
  them makes it impossible to save a new account before running
  "Create default accounts" (the button needs a saved document). Structural
  errors (wrong company, group account, wrong type, account shared by two
  companies) still block.
- **`PostEx Settings` is not created by the app.** It is a legacy UI-created
  Single doctype; the app reads it once via `migrate.import_legacy_settings`
  and then ignores it. `PostEx Webhook Log` is *adopted* in `before_install`
  (module `Custom` → `PostEx Integration`, `custom = 0`) so existing rows survive.
- **No fixtures.** Custom fields are created from `fields.py` only when
  missing, so an existing site's layout is never overwritten by `bench migrate`.
- **One `PostEx Account` may be shared by nothing**: account sharing between
  companies is a hard validation error, because a ledger account cannot belong
  to two companies in ERPNext anyway.

### Deferred

- Reports still need a Company filter and grouping by `custom_postex_account`
  (§12).
- `PostEx Unsettled COD` / `PostEx Unaccounted Amount` mappings exist but no
  scheduled job writes them; the historical backlog was reconciled manually.
- `PostEx Sync Run` has no chart/list view beyond the default; `last_run_result`
  remains the per-account one-line summary.


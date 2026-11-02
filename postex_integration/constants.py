"""Constants that are genuinely PostEx-wide (not company-specific).

Anything that can differ per company or per merchant account belongs in
`PostEx Account` / `PostEx Account Mapping`, never here.
"""

# Bumped whenever we need to prove which code a site is actually running; call
# `postex_integration.api.health` after a deploy and compare.
BUILD = "2026-11-02.1"

POSTEX_COURIER = "PostEx"
DEFAULT_BASE_URL = "https://api.postex.pk"

# Endpoints (Merchant API Guide v4.1.9)
EP_ALL_ORDERS = "/services/integration/api/order/v1/get-all-order"
EP_UNBOOKED_ORDERS = "/services/integration/api/order/v2/get-unbooked-orders"
EP_TRACK_ORDER = "/services/integration/api/order/v1/track-order/"
EP_PAYMENT_STATUS = "/services/integration/api/order/v1/payment-status/"
EP_ADD_ORDER = "/services/integration/api/order/v1/add-order"

# ---------------------------------------------------------------------------
# Status mapping
# ---------------------------------------------------------------------------
# Live PostEx strings differ from the wording in the guide, and order matters:
# specific needles must come before the generic ones they contain
# ("en route to postex" before "postex warehouse" and before "en route";
#  "return in transit" before "return"; "unbook" before "booked").
STATUS_MAP = [
	("un assigned", "Un-Assigned By Me"),
	("unassign", "Un-Assigned By Me"),
	("unbook", "Unbooked"),
	("en route to postex", "En-Route to PostEx Warehouse"),
	("out for deliver", "Out For Delivery"),
	("en route", "Out For Delivery"),
	("out for return", "Out For Return"),
	("return in transit", "Out For Return"),
	("under verification", "Delivery Under Review"),
	("under review", "Delivery Under Review"),
	("postex warehouse", "PostEx Warehouse"),
	("warehouse", "PostEx Warehouse"),
	("in stock", "PostEx Warehouse"),
	("picked", "Picked By PostEx"),
	("delivered", "Delivered"),
	("attempt", "Attempted"),
	("refused", "Returned"),
	("consignee not", "Attempted"),
	("not responding", "Attempted"),
	("transfer", "Transferred"),
	("expire", "Expired"),
	("lost", "Lost"),
	("damage", "Damage"),
	("stolen", "Stolen"),
	("cancel", "Cancelled"),
	("return", "Returned"),
	("booked", "Booked"),
]

# Merchant API Guide 3.8.3 / 3.9.3 / 3.16.3 transactionStatusHistory codes.
# Only used when transactionStatus is empty: unbooked orders carry history
# code 0001 / "At ... Warehouse" and would otherwise look Booked.
HISTORY_CODES = {
	"0001": "Booked",
	"0002": "Returned",
	"0003": "PostEx Warehouse",
	"0004": "Transferred",
	"0005": "Delivered",
	"0006": "Returned",
	"0007": "Returned",
	"0008": "Delivery Under Review",
	"0013": "Attempted",
}

STATUS_OPTIONS = [
	"Unbooked",
	"Booked",
	"PostEx Warehouse",
	"En-Route to PostEx Warehouse",
	"Picked By PostEx",
	"Out For Delivery",
	"Transferred",
	"Attempted",
	"Delivery Under Review",
	"Out For Return",
	"Returned",
	"Delivered",
	"Cancelled",
	"Un-Assigned By Me",
	"Expired",
	"Lost",
	"Damage",
	"Stolen",
	"Unknown",
]

# A parcel in these states has physically left the store and has not come back.
PICKUP_STATUSES = (
	"Picked By PostEx",
	"En-Route to PostEx Warehouse",
	"PostEx Warehouse",
	"Transferred",
	"Out For Delivery",
	"Attempted",
	"Delivery Under Review",
	"Out For Return",
	"Delivered",
)

TERMINAL_STATUSES = ("Delivered", "Returned", "Cancelled", "Expired")

# ---------------------------------------------------------------------------
# Sync scope
# ---------------------------------------------------------------------------
# The scope of a sync run is decided by the order's state, never by a row
# count. See `scope.py` for the predicate and `architecture.md` §7.2 for the
# sizing table.
#
# Still moving: polled on every run.
ACTIVE_STATUSES = (
	"Unbooked",
	"Booked",
	"Picked By PostEx",
	"En-Route to PostEx Warehouse",
	"PostEx Warehouse",
	"Out For Delivery",
	"Attempted",
	"Delivery Under Review",
	"Out For Return",
	"Transferred",
)

# Terminal, but only once the late-return window has closed: a parcel marked
# Delivered can still be refused / returned by the consignee weeks later.
LATE_RETURN_STATUSES = ("Delivered", "Returned")
LATE_RETURN_WINDOW_DAYS = 45

# Final for good. `Cancelled` / `Expired` are handled separately: they are only
# final once the Sales Order is terminal too, because a cancelled booking can
# be rebooked while the order is still open.
FINAL_STATUSES = ("Lost", "Damage", "Stolen", "Un-Assigned By Me")

# `Sales Order.status` values that make a Cancelled/Expired courier status final.
CLOSED_SALES_ORDER_STATUSES = ("Closed", "Completed", "Cancelled")

SCAN_ACTIVE = "Active"
SCAN_LATE_RETURNS = "Late Returns"

# ---------------------------------------------------------------------------
# Sync pacing / cursor
# ---------------------------------------------------------------------------
# page size: how many orders one run looks at. Sizing (1 request/second):
#   100 orders/day -> ~500 in flight  -> 6 per run at 96 runs/day
#   500 orders/day -> ~2.500 in flight -> 26 per run
#  1000 orders/day -> ~5.000 in flight -> 53 per run
#  2000 orders/day -> ~10.000 in flight -> 105 per run
# 200 gives 4-8x headroom up to ~2.000 orders/day. If volume outgrows that,
# raise the page size or shorten the cron interval - never the safety ceiling.
DEFAULT_SYNC_PAGE_SIZE = 200
DEFAULT_REQUEST_DELAY = 1.0
MIN_REQUEST_DELAY = 0.25
DEFAULT_RUN_REQUEST_BUDGET = 800

# An order that fails this many times in a row is quarantined
# (`custom_postex_needs_attention`) so it stops consuming scan slots.
MAX_SYNC_FAILURES = 5

# Hard safety ceiling only: a bad query must not scan the whole table. It is
# not a correctness mechanism (the cursor is).
MAX_SCAN_CEILING = 2000

# ---------------------------------------------------------------------------
# Mapping roles
# ---------------------------------------------------------------------------
ROLE_FINANCE = "Courier Finance"
ROLE_DEBTORS = "Debtors"
ROLE_DELIVERY_CHARGES = "Delivery Charges"
ROLE_TAX_WITHHELD = "Tax Withheld"
ROLE_UNSETTLED_COD = "Unsettled COD"
ROLE_UNACCOUNTED = "Unaccounted"
ROLE_IN_TRANSIT = "In-Transit Warehouse"
ROLE_STORES = "Stores Warehouse"
ROLE_COST_CENTER = "Cost Center"

ROLES = (
	ROLE_FINANCE,
	ROLE_DEBTORS,
	ROLE_DELIVERY_CHARGES,
	ROLE_TAX_WITHHELD,
	ROLE_UNSETTLED_COD,
	ROLE_UNACCOUNTED,
	ROLE_IN_TRANSIT,
	ROLE_STORES,
	ROLE_COST_CENTER,
)

ACCOUNT_ROLES = (
	ROLE_FINANCE,
	ROLE_DEBTORS,
	ROLE_DELIVERY_CHARGES,
	ROLE_TAX_WITHHELD,
	ROLE_UNSETTLED_COD,
	ROLE_UNACCOUNTED,
)
WAREHOUSE_ROLES = (ROLE_IN_TRANSIT, ROLE_STORES)

REQUIRED_ROLES = (
	ROLE_FINANCE,
	ROLE_DEBTORS,
	ROLE_DELIVERY_CHARGES,
	ROLE_TAX_WITHHELD,
	ROLE_UNSETTLED_COD,
	ROLE_IN_TRANSIT,
	ROLE_STORES,
)

# role -> (allowed root_types, allowed account_types, allow blank account_type)
ACCOUNT_RULES = {
	ROLE_FINANCE: (("Asset",), ("Bank", "Cash"), False),
	ROLE_DEBTORS: (("Asset",), ("Receivable",), False),
	ROLE_DELIVERY_CHARGES: (("Expense",), (), True),
	ROLE_TAX_WITHHELD: (("Asset", "Expense"), (), True),
	ROLE_UNSETTLED_COD: (("Asset",), (), True),
	ROLE_UNACCOUNTED: (("Liability", "Asset"), (), True),
}

# ---------------------------------------------------------------------------
# Invoice payment status vocabulary (Sales Invoice.custom_courier_payment_status)
# ---------------------------------------------------------------------------
PAYMENT_NOT_PAID = "Not Paid"
PAYMENT_PAID = "Paid"
PAYMENT_APPROX = "Paid (Approx)"
PAYMENT_NOT_DUE = "No Payment Due"

# 4% advance tax withheld by PostEx (2% ITWH + 2% STWH) is configured per
# account (PostEx Account.withholding_percent); this is only the default.
DEFAULT_WITHHOLDING_PERCENT = 4.0

# Safety caps per run, mirroring the server-script behaviour. The scan size and
# the HTTP call budget are per account (see `PostEx Account.sync_page_size` /
# `run_request_budget`); these remain genuinely site-wide.
MAX_PAYMENT_STATUS_CALLS = 40
MAX_DOCS_PER_HELPER = 100

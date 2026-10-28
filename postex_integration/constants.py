"""Constants that are genuinely PostEx-wide (not company-specific).

Anything that can differ per company or per merchant account belongs in
`PostEx Account` / `PostEx Account Mapping`, never here.
"""

# Bumped whenever we need to prove which code a site is actually running; call
# `postex_integration.api.health` after a deploy and compare.
BUILD = "2026-10-28.3"

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

# Safety caps per run, mirroring the server-script behaviour.
MAX_TRACK_ORDER_CALLS = 40
MAX_PAYMENT_STATUS_CALLS = 40
MAX_ORDERS_PER_SYNC = 500
MAX_DOCS_PER_HELPER = 100

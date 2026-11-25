"""Gate *automatic* PostEx booking on the order's return-risk score.

The `ai_return_prediction` app scores a Sales Order into `custom_return_risk_pct`
and `custom_return_risk_band` (Low / Medium / High / Very High). A high-risk
parcel is often refused at the door, and a refusal costs the delivery attempt
plus the return leg — so the merchant wants to call that customer before the
parcel is booked.

The gate is **opt-in per account** and only ever affects automatic booking:

* `enable_risk_gate` off -> every eligible order is booked as before;
* on -> an order is booked only when its risk is **strictly below**
  `auto_book_max_return_risk`; everything else is held for manual review;
* an order with **no score** is never auto-booked while the gate is on
  (unknown risk is not low risk) — but it can always be booked by hand.

Manual booking is never blocked: the whole point is that a human decides.
"""

from frappe.utils import cint, flt

RISK_PCT_FIELD = "custom_return_risk_pct"
RISK_BAND_FIELD = "custom_return_risk_band"

# Set on the Sales Order when the gate holds it, cleared when it is booked.
REVIEW_FIELD = "custom_postex_review_reason"


def risk_gate_blocker(account, sales_order) -> str | None:
	"""Why this order must not be auto-booked, or None when it may be.

	Pure: works on a `PostEx Account` document, a dict or anything with
	`.get()`, and writes nothing — so the decision is testable without a site.
	"""
	if not account or not cint(account.get("enable_risk_gate")):
		return None

	limit = flt(account.get("auto_book_max_return_risk"))
	if limit <= 0:
		# Gate switched on without a limit: behave as before rather than
		# silently blocking every booking.
		return None

	pct = flt(sales_order.get(RISK_PCT_FIELD))
	band = (sales_order.get(RISK_BAND_FIELD) or "").strip()

	# The scoring app leaves pct 0 and an empty band on an order it could not
	# score (no delivery address). That is "unknown", not "safe".
	if not band and pct <= 0:
		return "no return-risk score on this order"

	if pct >= limit:
		return f"return risk {pct:g}% is not below the {limit:g}% auto-booking limit"

	return None

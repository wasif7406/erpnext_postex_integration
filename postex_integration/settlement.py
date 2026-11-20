"""COD settlement: courier charges, 4% withheld tax and the payout entry.

At settlement PostEx has already collected the COD, deducted its delivery fee
plus GST on that fee, and withheld advance tax. The net amount lands in the
merchant wallet (`Courier Finance` of that account).

    JE: Dr Delivery Charges (fee + GST)
        Dr Tax Withheld      (withholding % of the COD)
        Cr Debtors           (party + invoice reference)
    PE: Dr Courier Finance / Cr Debtors   (allocated to the invoice)

Two settlement paths exist and both end up here:

* `reconcile_account` walks the account's delivered orders and asks the
  `payment-status` API whether each one is paid (the polling path);
* `settlement_log.reconcile` reads the merchant's Settlement Log export, where
  every CPR batch carries the exact fee / GST / ITWH / STWH that was deducted
  (the authoritative path — the API's `settle` flag can stay false for an order
  that a CPR has already paid).

Everything that decides *what* to book lives in this module so the two paths
cannot drift apart:

* the withheld tax is computed on the **COD collected** (`order.grand_total`),
  never on the invoice total, because that is what PostEx withholds;
* an invoice whose receivable is already cleared (`outstanding <= 0`, e.g. a
  historical import parked it elsewhere) still gets its charges booked — the
  cost then reduces `Unsettled COD` instead of `Debtors`, because that is where
  the money is parked — and is marked paid instead of silently skipped;
* one tracking number backed by two Sales Orders is refused rather than paid
  twice.
"""

import frappe
from frappe.utils import flt, now_datetime, nowdate

from .client import PostExClient
from .config import AccountConfig, get_account
from .constants import (
	MAX_PAYMENT_STATUS_CALLS,
	PAYMENT_PAID,
)
from .exceptions import PostExAPIError, PostExConfigError
from .utils import (
	bump_failures,
	claim_field,
	clear_failures,
	field_exists,
	log_error,
	today_or,
)

SETTLEMENT_CURSOR = "custom_postex_payout_checked_on"


# ---------------------------------------------------------------------------
# Claim / guard helpers
# ---------------------------------------------------------------------------
def _is_submitted(doctype: str, name: str) -> bool:
	"""True only for a submitted document; a cancelled/draft/deleted one is not a
	live claim and may be replaced (see `utils.claim_field`)."""
	return frappe.db.get_value(doctype, name, "docstatus") == 1


def _cancel_quietly(doc) -> bool:
	"""Cancel a document this run created by mistake. True when it is cancelled."""
	try:
		doc.flags.ignore_permissions = True
		doc.cancel()
		frappe.db.commit()
		return frappe.db.get_value(doc.doctype, doc.name, "docstatus") == 2
	except Exception:
		log_error(
			"PostEx: could not cancel a duplicate document",
			f"{doc.doctype} {doc.name}\n{frappe.get_traceback()}",
		)
		return False


def tracking_conflict(tracking: str, sales_order: str) -> str | None:
	"""Another submitted Sales Order already claims this tracking number.

	Two orders sharing one tracking is how a single CPR gets paid twice: the
	courier reports the same parcel settled and each order books its own
	receivable. The duplicate is reported (never guessed) so a human can fix the
	mapping instead of the books receiving phantom money.
	"""
	tracking = (tracking or "").strip().replace(" ", "")
	if not tracking or not field_exists("Sales Order", "custom_postex_tracking_number"):
		return None
	return frappe.db.get_value(
		"Sales Order",
		{
			"custom_postex_tracking_number": tracking,
			"docstatus": 1,
			"name": ("!=", sales_order),
		},
		"name",
	)


# ---------------------------------------------------------------------------
# Cron entry point
# ---------------------------------------------------------------------------
def reconcile_all(company=None) -> dict:
	accounts = frappe.get_all(
		"PostEx Account",
		filters={"enabled": 1, "enable_sync": 1, **({"company": company} if company else {})},
		fields=["name"],
	)
	out = {"accounts": len(accounts), "results": {}}
	for row in accounts:
		try:
			out["results"][row.name] = reconcile_account(row.name)
		except Exception:
			log_error("PostEx settlement failed", f"{row.name}\n{frappe.get_traceback()}")
			out["results"][row.name] = {"error": "exception"}
	return out


def reconcile_account_locked(account: str) -> dict:
	"""Cron entry point: settlement under the account's settlement lock."""
	from .tasks import run_locked

	return run_locked("settle", account, reconcile_account)


def reconcile_account(account: str, limit: int = MAX_PAYMENT_STATUS_CALLS) -> dict:
	account_doc = get_account(account)
	if not account_doc.enabled or not account_doc.enable_sync:
		return {"skipped": "disabled"}
	if not account_doc.get_password("api_token", raise_exception=False):
		return {"skipped": "no token"}

	try:
		config = AccountConfig(account_doc)
	except PostExConfigError as exc:
		return {"skipped": f"mapping incomplete: {exc}"}
	if config.dry_run:
		return {"skipped": "dry run: no documents created"}
	client = PostExClient(account_doc)
	result = {
		"account": account_doc.name,
		"checked": 0,
		"settled": 0,
		"charges_entries": [],
		"payments": [],
		"already_settled": [],
		"duplicate_trackings": [],
		"failed": [],
	}

	orders = frappe.db.sql(
		f"""
		select name, customer, company, shopify_order_number, grand_total,
		       custom_postex_tracking_number, custom_postex_sales_invoice,
		       custom_postex_delivery_fee, custom_postex_delivery_tax,
		       custom_postex_charges_entry
		from `tabSales Order`
		where docstatus = 1
		  and company = %(company)s
		  and custom_postex_account = %(account)s
		  and custom_postex_status = 'Delivered'
		  and ifnull(custom_postex_sales_invoice, '') != ''
		  and ifnull(custom_postex_settled_on, '') = ''
		  and ifnull(custom_postex_needs_attention, 0) = 0
		order by {_cursor_order()}, modified asc
		limit %(limit)s
		""",
		{"company": config.company, "account": config.name, "limit": int(limit)},
		as_dict=True,
	)

	for row in orders:
		order = frappe._dict(row)
		# Settlement has its own cursor: every checked order is stamped, so a
		# permanently failing or forever-unsettled invoice rotates to the back
		# instead of occupying one of the 40 slots on every single run.
		_stamp_checked(order.name)
		tracking = (order.custom_postex_tracking_number or "").strip().replace(" ", "")
		if not tracking:
			continue
		result["checked"] += 1

		clash = tracking_conflict(tracking, order.name)
		if clash:
			result["duplicate_trackings"].append(
				{"sales_order": order.name, "tracking": tracking, "also_used_by": clash}
			)
			continue

		try:
			dist = client.payment_status(tracking)
		except PostExAPIError as exc:
			result["failed"].append(
				{"sales_order": order.name, "step": "settlement", "error": str(exc)[:300]}
			)
			bump_failures(order.name, "settlement", str(exc))
			continue
		# The call worked: "not settled yet" is a healthy answer, not a failure.
		clear_failures(order.name)
		if not dist or not _is_true(dist.get("settle")):
			continue

		settled_on = today_or(dist.get("settlementDate"))
		cpr = str(dist.get("cpr1") or dist.get("cprNumber_1") or "").strip()

		invoice = frappe.get_doc("Sales Invoice", order.custom_postex_sales_invoice)
		if invoice.docstatus != 1:
			_mark_settled_on_order(order.name, settled_on)
			clear_failures(order.name)
			continue

		settle_order(
			config,
			order,
			invoice,
			settled_on=settled_on,
			cpr=cpr,
			tracking=tracking,
			result=result,
		)

	frappe.db.commit()
	return result


# ---------------------------------------------------------------------------
# One order: charges + payout (shared by both settlement paths)
# ---------------------------------------------------------------------------
def settle_order(
	config: AccountConfig,
	order,
	invoice,
	settled_on: str,
	cpr: str = "",
	tracking: str = "",
	result: dict | None = None,
	fee=None,
	fee_tax=None,
	withholding=None,
):
	"""Book the courier charges and the payout for one delivered order.

	`fee`, `fee_tax` and `withholding` override the values derived from the
	Sales Order / invoice; the settlement-log path passes the exact figures the
	CPR deducted. `result` is the caller's accumulator (None for a one-off call).
	"""
	result = result if result is not None else {}

	invoice = _book_charges(
		config,
		order,
		invoice,
		settled_on,
		tracking,
		result,
		fee=fee,
		fee_tax=fee_tax,
		withholding=withholding,
	)

	outstanding = flt(invoice.outstanding_amount)
	if outstanding <= 0:
		# Nothing left to receive: the receivable was already cleared elsewhere
		# (historical import, manual JV). The money is settled, so say so — the
		# old code left the invoice on "Not Paid" forever.
		_mark_invoice_paid(order, invoice.name, settled_on)
		_mark_settled_on_order(order.name, settled_on)
		clear_failures(order.name)
		result.setdefault("already_settled", []).append(order.name)
		return invoice

	group = {
		"cpr": cpr,
		"date": settled_on,
		"customer": order.customer,
		"company": config.company,
		"account": config.name,
		"refs": [
			{
				"reference_doctype": "Sales Invoice",
				"reference_name": invoice.name,
				"allocated_amount": outstanding,
			}
		],
		"orders": [order.name],
		"total": outstanding,
	}
	_create_payment(config, group, result)
	return frappe.get_doc("Sales Invoice", invoice.name)


# ---------------------------------------------------------------------------
# Charges
# ---------------------------------------------------------------------------
def withholding_amount(cod, percent) -> float:
	"""The 2% + 2% advance tax PostEx withholds, on the **COD collected**.

	Not on the invoice total: a consignment split over two invoices, or an
	invoice carrying a discount, would then withhold 4% of the wrong number and
	drift from the settlement log.
	"""
	if not cod:
		return 0.0
	return flt(flt(cod) * flt(percent) / 100.0, 2)


def charge_split(net, outstanding):
	"""Where the courier charges land: (to_debtors, excess, excess_role).

	* `outstanding > 0` -> reduce the receivable, the remainder (a small invoice
	  with a large fee) goes to the courier wallet, which is what the money
	  actually does.
	* `outstanding <= 0` -> the receivable was already cleared (historical
	  import, manual JV). Debiting `Debtors` would leave a credit balance, and
	  skipping the entry — the previous behaviour — silently dropped a real cost
	  and left the invoice stuck on "Not Paid". The money is parked in the
	  courier receivable, so the cost reduces that (`unsettled_cod`).
	"""
	net = flt(net, 2)
	outstanding = flt(outstanding, 2)
	if outstanding > 0:
		to_debtors = min(net, outstanding)
		return to_debtors, flt(net - to_debtors, 2), "finance"
	return 0.0, net, "unsettled_cod"


def _book_charges(
	config: AccountConfig,
	order,
	invoice,
	settled_on,
	tracking,
	result,
	fee=None,
	fee_tax=None,
	withholding=None,
):
	"""Book the courier fee (+GST) and the withheld tax for one invoice.

	The withheld tax is a percentage of the **COD collected**, which is the
	Sales Order total, not the invoice total: when one consignment is split over
	two invoices (or an invoice carries a discount) the invoice total is not what
	PostEx withheld, and 4% of it would drift from the settlement log.
	"""
	existing = order.custom_postex_charges_entry
	if existing and _is_submitted("Journal Entry", existing):
		return invoice

	if fee is None:
		fee = flt(order.custom_postex_delivery_fee)
	if fee_tax is None:
		fee_tax = flt(order.custom_postex_delivery_tax)
	charges = flt(fee + fee_tax, 2)

	cod = flt(order.get("grand_total")) or flt(invoice.grand_total)
	if withholding is None:
		withholding = withholding_amount(cod, config.withholding_percent)
	withholding = flt(withholding, 2)

	net = flt(charges + withholding, 2)
	if net <= 0:
		return invoice

	outstanding = flt(invoice.outstanding_amount)
	to_debtors, excess, excess_role = charge_split(net, outstanding)
	excess_account = config.finance if excess_role == "finance" else config.unsettled_cod

	je = frappe.get_doc(
		{
			"doctype": "Journal Entry",
			"voucher_type": "Journal Entry",
			"company": config.company,
			"posting_date": settled_on,
			"user_remark": f"PostEx delivery charges + {flt(config.withholding_percent):g}% tax withheld "
			f"{order.shopify_order_number or order.name} / {tracking} [{config.name}]",
		}
	)
	if charges > 0:
		je.append(
			"accounts",
			{
				"account": config.delivery_charges,
				"debit_in_account_currency": charges,
				"cost_center": config.cost_center,
			},
		)
	if withholding > 0:
		je.append(
			"accounts",
			{
				"account": config.tax_withheld,
				"debit_in_account_currency": withholding,
				"cost_center": config.cost_center,
			},
		)
	if to_debtors > 0:
		je.append(
			"accounts",
			{
				"account": config.debtors,
				"credit_in_account_currency": to_debtors,
				"party_type": "Customer",
				"party": order.customer,
				"reference_type": "Sales Invoice",
				"reference_name": invoice.name,
				"cost_center": config.cost_center,
			},
		)
	if excess > 0:
		je.append(
			"accounts",
			{
				"account": excess_account,
				"credit_in_account_currency": excess,
				"cost_center": config.cost_center,
			},
		)

	try:
		je.insert(ignore_permissions=True)
		je.submit()
	except Exception as exc:
		log_error(
			"PostEx charges entry failed",
			f"{order.name}: {exc}\n{frappe.get_traceback()}",
			"Sales Invoice",
			invoice.name,
		)
		result.setdefault("failed", []).append(
			{"sales_order": order.name, "step": "settlement", "error": f"charges: {str(exc)[:280]}"}
		)
		bump_failures(order.name, "settlement", f"charges: {exc}")
		return invoice

	# Claim the order *conditionally*: a concurrent run may have booked its own
	# charges entry while this one was submitting. `allow=existing` takes over a
	# link left on a cancelled/draft entry by an interrupted run.
	if not claim_field(
		"Sales Order", order.name, "custom_postex_charges_entry", je.name, allow=existing
	):
		_cancel_quietly(je)
		result.setdefault("failed", []).append(
			f"{order.name}: duplicate charges entry prevented ({je.name})"
		)
		return frappe.get_doc("Sales Invoice", invoice.name)

	frappe.db.set_value(
		"Sales Invoice",
		invoice.name,
		{
			"custom_postex_charges_entry": je.name,
			"custom_postex_account": config.name,
		},
		update_modified=False,
	)
	result.setdefault("charges_entries", []).append(je.name)

	return frappe.get_doc("Sales Invoice", invoice.name)


# ---------------------------------------------------------------------------
# Payment entry
# ---------------------------------------------------------------------------
def _create_payment(config: AccountConfig, group, result):
	if not group["refs"]:
		return
	try:
		pe = frappe.get_doc(
			{
				"doctype": "Payment Entry",
				"payment_type": "Receive",
				"party_type": "Customer",
				"party": group["customer"],
				"company": group["company"],
				"posting_date": group["date"] or nowdate(),
				"paid_from": config.debtors,
				"paid_to": config.finance,
				"paid_amount": group["total"],
				"received_amount": group["total"],
				"reference_no": group["cpr"] or f"PostEx {config.name}",
				"reference_date": group["date"] or nowdate(),
				"remarks": f"PostEx COD settlement {group['cpr'] or ''} "
				f"({len(group['orders'])} invoice(s)) via {config.name}".strip(),
				"references": group["refs"],
			}
		)
		pe.insert(ignore_permissions=True)
		pe.submit()
	except Exception as exc:
		log_error(
			"PostEx payout entry failed",
			f"{group['cpr']} / {group['customer']}: {exc}\n{frappe.get_traceback()}",
		)
		# A payment failure is group-wide (CPR/customer), not one order's fault,
		# so it is recorded but does not quarantine the invoices.
		result.setdefault("failed", []).append(
			{
				"sales_order": group["orders"][0] if group["orders"] else None,
				"step": "settlement",
				"error": f"payment {group['cpr']}/{group['customer']}: {str(exc)[:280]}",
			}
		)
		return

	# Claim every order of the group. The claim is a conditional UPDATE, so a
	# concurrent run that already paid one of them makes us the loser instead of
	# silently overwriting its link (which is what duplicated payouts before).
	claimed = []
	for sales_order in group["orders"]:
		if not _claim_payout(sales_order, pe.name):
			_drop_duplicate_payment(pe, claimed, group, result)
			return
		claimed.append(sales_order)

	for sales_order in claimed:
		frappe.db.set_value(
			"Sales Order",
			sales_order,
			{"custom_postex_settled_on": group["date"]},
			update_modified=False,
		)
		clear_failures(sales_order)
	for ref in group["refs"]:
		frappe.db.set_value(
			"Sales Invoice",
			ref["reference_name"],
			{
				"custom_postex_payout_entry": pe.name,
				"custom_postex_settled_on": group["date"],
				"custom_courier_payment_status": PAYMENT_PAID,
				"custom_courier_paid_on": group["date"],
				"custom_postex_account": config.name,
			},
			update_modified=False,
		)

	result.setdefault("payments", []).append(pe.name)
	result["settled"] = result.get("settled", 0) + len(group["orders"])
	frappe.db.commit()


def _mark_invoice_paid(order, invoice_name, settled_on, payment_entry=None):
	"""Flag a settled invoice as paid without creating a payment entry.

	Used when the receivable is already zero: there is no cash left to move, but
	the courier *has* paid, and the invoice must stop advertising "Not Paid".
	"""
	values = {
		"custom_postex_settled_on": settled_on,
		"custom_courier_payment_status": PAYMENT_PAID,
		"custom_courier_paid_on": settled_on,
	}
	if payment_entry:
		values["custom_postex_payout_entry"] = payment_entry
	frappe.db.set_value(
		"Sales Invoice",
		invoice_name,
		values,
		update_modified=False,
	)


def _claim_payout(sales_order: str, payment_entry: str) -> bool:
	"""Claim one order for this payment entry.

	`allow` matters here: if an interrupted run left a link to a payment entry
	that was never submitted, the order would otherwise be unclaimable forever.
	A link to a *submitted* entry is a real claim and is never taken over.
	"""
	if claim_field("Sales Order", sales_order, "custom_postex_payout_entry", payment_entry):
		return True
	current = frappe.db.get_value("Sales Order", sales_order, "custom_postex_payout_entry")
	if current and not _is_submitted("Payment Entry", current):
		return claim_field(
			"Sales Order",
			sales_order,
			"custom_postex_payout_entry",
			payment_entry,
			allow=current,
		)
	return False


def _drop_duplicate_payment(pe, claimed, group, result):
	"""Another run already paid part of this group: undo ours completely.

	The payment entry is cancelled (which reverses its GL and its allocations)
	and the orders we had claimed are released, so the next run settles them
	again. A crash between the two steps is safe: the released link is the only
	thing that matters, and a claim on a cancelled entry is taken over later.
	"""
	cancelled = _cancel_quietly(pe)
	if cancelled and claimed:
		frappe.db.sql(
			"""
			update `tabSales Order`
			set custom_postex_payout_entry = null
			where name in %s
			  and custom_postex_payout_entry = %s
			""",
			(tuple(claimed), pe.name),
		)
	frappe.db.commit()
	result.setdefault("failed", []).append(
		{
			"sales_order": group["orders"][0] if group["orders"] else None,
			"step": "settlement",
			"error": f"duplicate payment prevented {pe.name}",
		}
	)


def _mark_settled_on_order(sales_order, settled_on):
	frappe.db.set_value(
		"Sales Order", sales_order, "custom_postex_settled_on", settled_on, update_modified=False
	)
	frappe.db.commit()


def _stamp_checked(sales_order):
	if not field_exists("Sales Order", SETTLEMENT_CURSOR):
		return
	frappe.db.set_value(
		"Sales Order",
		sales_order,
		SETTLEMENT_CURSOR,
		now_datetime(),
		update_modified=False,
	)


def _cursor_order() -> str:
	"""Settlement cursor, or `modified` on a site that has not migrated yet."""
	if field_exists("Sales Order", SETTLEMENT_CURSOR):
		return f"ifnull({SETTLEMENT_CURSOR}, '1900-01-01 00:00:00')"
	return f"ifnull(modified, '1900-01-01 00:00:00')"


def _is_true(value) -> bool:
	return value is True or str(value).lower() in ("true", "1", "yes")


def unpaid_delivered(config: AccountConfig, limit: int = 0) -> list:
	"""Delivered + invoiced but not settled — the cash the courier still holds."""
	filters = {
		"docstatus": 1,
		"company": config.company,
		"custom_postex_account": config.name,
		"custom_postex_status": "Delivered",
		"custom_postex_settled_on": ("is", "not set"),
	}
	return frappe.get_all(
		"Sales Order",
		filters=filters,
		fields=["name", "customer", "custom_postex_tracking_number", "custom_postex_sales_invoice", "grand_total"],
		limit=limit or None,
		order_by="transaction_date asc",
	)

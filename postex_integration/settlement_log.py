"""Reconcile the merchant's PostEx **Settlement Log** export against ERPNext.

The `payment-status` API is the only settlement signal the polling path has, and
it is not always right: an order can sit on `settle: false` for weeks while a
CPR has already paid it (that is how 43 delivered invoices stayed "Not Paid"
while 94,665.25 sat in the courier's payout). The settlement log is the
merchant's own record of every CPR batch — gross COD, the reserve that was paid,
the fee, GST and the 2%+2% withheld tax — so it is the authoritative source.

This module books from that file and nothing else:

* **Delivered + a reserve payment** -> settle the invoice (charges + payout)
  using the exact figures the CPR deducted, and mark it paid.
* **Return** -> book the delivery fee + GST of the failed attempt, which the
  API does not expose for returned parcels (`transactionFee` comes back 0), so
  the polling path could never book it.

Nothing is created for a row whose Sales Order or invoice cannot be resolved:
those are reported back so a human can map them, never guessed.

Usage
-----
``bench execute postex_integration.settlement_log.reconcile --kwargs '{"path": "/tmp/log.csv", "dry_run": 1}'``

or `postex_integration.api.reconcile_settlement_log(path=..., dry_run=1)`.
"""

import csv
import os

import frappe
from frappe.utils import flt

from .config import AccountConfig, get_account
from .constants import PAYMENT_NOT_PAID, PAYMENT_PAID
from .exceptions import PostExConfigError
from .settlement import settle_order, tracking_conflict
from .utils import existing_invoice, log_error

DELIVERED = "Delivered"
RETURNED = "Return"


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------
def parse_log(path: str) -> list:
	"""Read a Settlement Logs CSV into normalised rows.

	The export is comma separated with a UTF-8 BOM and quoted numbers, so a
	plain `csv.DictReader` after stripping the BOM is enough; every optional
	column is read defensively (an older export may not carry one).
	"""
	if not path or not os.path.exists(path):
		raise FileNotFoundError(f"Settlement log not found: {path}")
	rows = []
	with open(path, encoding="utf-8-sig", newline="") as fh:
		for raw in csv.DictReader(fh):
			tracking = (raw.get("TRACKING_NUMBER") or "").strip()
			if not tracking:
				continue
			rows.append(
				{
					"ref": (raw.get("ORDER_REF_NUMBER") or "").strip(),
					"tracking": tracking.replace(" ", ""),
					"customer": (raw.get("CUSTOMER_NAME") or "").strip(),
					"status": (raw.get("TRANSACTION_STATUS") or "").strip(),
					"cod": flt(raw.get("COD_AMOUNT")),
					"reserve": flt(raw.get("RESERVE_PAYMENT")),
					"fee": flt(raw.get("FEE")),
					"gst": flt(raw.get("GST")),
					"itwh": flt(raw.get("ITWH (2%)")),
					"stwh": flt(raw.get("STWH (2%)")),
					"cpr": (raw.get("RESERVE_CPR_NUMBER") or "").strip(),
					"cpr_date": (raw.get("RESERVE_CPR_DATE") or "")[:10],
					"delivered_on": (raw.get("ORDER_DELIVERY_DATE") or "")[:10],
				}
			)
	return rows


# ---------------------------------------------------------------------------
# Order resolution
# ---------------------------------------------------------------------------
def _find_order(company: str, row, account: str | None = None) -> dict | None:
	"""The Sales Order this log row belongs to: tracking first, then its ref.

	Tracking wins because it identifies the physical parcel; the order reference
	(`po_no`) is what the merchant booked and can be a `-COPY` re-booking. An
	`account` narrows further when the caller knows it, but a historical order
	that was imported before the app existed has no account set and must still
	be found — otherwise the reconciliation would skip exactly the orders that
	need it most.
	"""
	params = {"company": company, "tracking": row["tracking"], "ref": row["ref"]}
	account_sql = ""
	if account:
		account_sql = " and (ifnull(custom_postex_account, '') in ('', %(account)s))"
		params["account"] = account
	rows = frappe.db.sql(
		f"""
		select name, customer, company, grand_total, shopify_order_number,
		       custom_postex_sales_invoice, custom_postex_settled_on,
		       custom_postex_charges_entry, custom_postex_return_charges_entry,
		       custom_postex_tracking_number
		from `tabSales Order`
		where docstatus = 1
		  and company = %(company)s
		  {account_sql}
		  and (
		    custom_postex_tracking_number = %(tracking)s
		    {("or po_no = %(ref)s" if row["ref"] else "")}
		  )
		order by (custom_postex_tracking_number = %(tracking)s) desc, transaction_date desc
		limit 2
		""",
		params,
		as_dict=True,
	)
	if not rows:
		return None
	return frappe._dict(rows[0])


def _invoice_for(order) -> str | None:
	name = order.get("custom_postex_sales_invoice") or existing_invoice(order["name"])
	return name


# ---------------------------------------------------------------------------
# Reconciliation
# ---------------------------------------------------------------------------
def reconcile_account_locked(account: str, path: str, dry_run: bool = True, from_date: str = None) -> dict:
	from .tasks import run_locked

	return run_locked(
		"settle-log", account, lambda acc: reconcile(acc, path, dry_run=dry_run, from_date=from_date)
	)


def reconcile(account: str = None, path: str = None, dry_run: bool = True, from_date: str = None) -> dict:
	"""Book every settled / returned row of one settlement log for one account.

	`from_date` (ISO date) ignores rows whose CPR / delivery date is earlier.
	Set it whenever the batch you are importing was already reconciled by a lump
	journal entry: a historical export would otherwise book the courier cost of
	every return a second time (the 25-10 lump covered all of it).
	"""
	account_doc = get_account(account)
	try:
		config = AccountConfig(account_doc)
	except PostExConfigError as exc:
		return {"skipped": f"mapping incomplete: {exc}"}

	rows = parse_log(path)
	result = {
		"account": config.name,
		"file": os.path.basename(path or ""),
		"from_date": from_date or "",
		"rows": len(rows),
		"skipped_before_cutover": 0,
		"settled": 0,
		"already_paid": 0,
		"return_charges": 0,
		"return_costs": [],
		"unmatched": [],
		"no_invoice": [],
		"duplicate_trackings": [],
		"failed": [],
		"dry_run": bool(dry_run),
	}

	for row in rows:
		if row["status"] not in (DELIVERED, RETURNED):
			continue
		if from_date and (row["cpr_date"] or row["delivered_on"]) < from_date:
			result["skipped_before_cutover"] += 1
			continue

		order = _find_order(config.company, row, account=config.name)
		if not order:
			result["unmatched"].append({"ref": row["ref"], "tracking": row["tracking"], "status": row["status"]})
			continue

		if row["status"] == RETURNED:
			_book_return_cost(config, order, row, result, dry_run=dry_run)
			continue

		# Delivered: only a row that actually paid counts as settled.
		if row["reserve"] <= 0:
			continue
		invoice_name = _invoice_for(order)
		if not invoice_name:
			result["no_invoice"].append(order["name"])
			continue
		invoice = frappe.get_doc("Sales Invoice", invoice_name)
		if invoice.docstatus != 1:
			continue
		pay_status = invoice.get("custom_courier_payment_status") or ""
		if pay_status == PAYMENT_PAID:
			result["already_paid"] += 1
			continue
		# Never re-open an invoice that was deliberately settled some other way:
		# a zero-value order or a credit note is "No Payment Due", an
		# approximation import is "Paid (Approx)". Only an open one is settled.
		if pay_status not in ("", PAYMENT_NOT_PAID):
			result["already_paid"] += 1
			continue

		clash = tracking_conflict(row["tracking"], order["name"])
		if clash:
			result["duplicate_trackings"].append(
				{"sales_order": order["name"], "tracking": row["tracking"], "also_used_by": clash}
			)
			continue

		if dry_run:
			result["settled"] += 1
			continue

		try:
			settle_order(
				config,
				order,
				invoice,
				settled_on=row["cpr_date"] or row["delivered_on"],
				cpr=row["cpr"],
				tracking=row["tracking"],
				result=result,
				# exact figures from the CPR — this is also what makes the
				# withheld tax match the log when the invoice total is not the COD
				fee=row["fee"],
				fee_tax=row["gst"],
				withholding=flt(row["itwh"] + row["stwh"], 2),
			)
			result["settled"] += 1
			frappe.db.commit()
		except Exception as exc:
			log_error(
				"PostEx settlement log: settle failed",
				f"{order['name']} / {row['tracking']}: {exc}\n{frappe.get_traceback()}",
				"Sales Order",
				order["name"],
			)
			result["failed"].append({"sales_order": order["name"], "error": str(exc)[:280]})

	return result


def _book_return_cost(config: AccountConfig, order, row, result, dry_run=True):
	"""Book the courier cost of a parcel that came back.

	`Dr Delivery Charges (fee + GST) / Cr Courier Finance` — the same entry the
	polling path makes, except the amounts come from the log, because the API
	returns `transactionFee: 0` for a returned parcel and the cost was therefore
	never booked.
	"""
	total = flt(row["fee"] + row["gst"], 2)
	if total <= 0:
		return
	if order.get("custom_postex_return_charges_entry"):
		return
	if dry_run:
		result["return_charges"] += 1
		return

	from .stock import _book_courier_cost  # imported late: stock imports settlement

	so = frappe.get_doc("Sales Order", order["name"])
	_book_courier_cost(
		config,
		so,
		total,
		"custom_postex_return_charges_entry",
		"return_costs",
		"PostEx return charges (settlement log)",
		"settlement-log",
		result,
	)
	result["return_charges"] += 1

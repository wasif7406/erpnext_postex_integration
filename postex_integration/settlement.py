"""COD settlement: courier charges, 4% withheld tax and the payout entry.

At settlement PostEx has already collected the COD, deducted its delivery fee
plus GST on that fee, and withheld advance tax. The net amount lands in the
merchant wallet (`Courier Finance` of that account).

    JE: Dr Delivery Charges (fee + GST)
        Dr Tax Withheld      (withholding % of the invoice total)
        Cr Debtors           (party + invoice reference)
    PE: Dr Courier Finance / Cr Debtors   (allocated to the invoice)
"""

import frappe
from frappe.utils import flt, nowdate

from .client import PostExClient
from .config import AccountConfig, get_account
from .constants import (
	MAX_PAYMENT_STATUS_CALLS,
	PAYMENT_PAID,
)
from .exceptions import PostExAPIError, PostExConfigError
from .utils import log_error, today_or


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
		"failed": [],
	}

	orders = frappe.get_all(
		"Sales Order",
		filters={
			"docstatus": 1,
			"company": config.company,
			"custom_postex_account": config.name,
			"custom_postex_status": "Delivered",
			"custom_postex_sales_invoice": ("is", "set"),
			"custom_postex_settled_on": ("is", "not set"),
		},
		fields=[
			"name",
			"customer",
			"company",
			"shopify_order_number",
			"custom_postex_tracking_number",
			"custom_postex_sales_invoice",
			"custom_postex_delivery_fee",
			"custom_postex_delivery_tax",
			"custom_postex_charges_entry",
		],
		limit=limit,
		order_by="modified asc",
	)

	groups = {}

	for order in orders:
		tracking = (order.custom_postex_tracking_number or "").strip().replace(" ", "")
		if not tracking:
			continue
		result["checked"] += 1
		try:
			dist = client.payment_status(tracking)
		except PostExAPIError as exc:
			result["failed"].append(f"{order.name}: {str(exc)[:90]}")
			continue
		if not dist or not _is_true(dist.get("settle")):
			continue

		settled_on = today_or(dist.get("settlementDate"))
		cpr = str(dist.get("cpr1") or dist.get("cprNumber_1") or "").strip()

		invoice = frappe.get_doc("Sales Invoice", order.custom_postex_sales_invoice)
		if invoice.docstatus != 1:
			_mark_settled_on_order(order.name, settled_on)
			continue

		invoice = _book_charges(config, order, invoice, settled_on, tracking, result)

		outstanding = flt(invoice.outstanding_amount)
		if outstanding <= 0:
			_mark_settled_on_order(order.name, settled_on)
			continue

		key = (cpr, settled_on, order.customer, config.name)
		group = groups.setdefault(
			key,
			{
				"cpr": cpr,
				"date": settled_on,
				"customer": order.customer,
				"company": config.company,
				"account": config.name,
				"refs": [],
				"orders": [],
				"total": 0.0,
			},
		)
		group["refs"].append(
			{
				"reference_doctype": "Sales Invoice",
				"reference_name": invoice.name,
				"allocated_amount": outstanding,
			}
		)
		group["orders"].append(order.name)
		group["total"] = flt(group["total"]) + outstanding

	for group in groups.values():
		_create_payment(config, group, result)

	frappe.db.commit()
	return result


# ---------------------------------------------------------------------------
# Charges
# ---------------------------------------------------------------------------
def _book_charges(config: AccountConfig, order, invoice, settled_on, tracking, result):
	"""Book the courier fee (+GST) and the withheld tax against the invoice."""
	if order.custom_postex_charges_entry:
		return invoice

	fee = flt(order.custom_postex_delivery_fee)
	fee_tax = flt(order.custom_postex_delivery_tax)
	charges = flt(fee + fee_tax, 2)

	total = flt(invoice.grand_total)
	withholding = flt(total * flt(config.withholding_percent) / 100.0, 2) if total else 0.0
	net = flt(charges + withholding, 2)
	if net <= 0:
		return invoice

	# Zero outstanding means the receivable is already cleared (a Payment Entry or
	# a journal entry booked it earlier, e.g. the historical rework). Booking the
	# fee against such an invoice fails ERPNext's "cannot be greater than
	# outstanding" check and would double-book a cost that is already recognised,
	# so treat the order as settled and leave its history alone.
	outstanding = flt(invoice.outstanding_amount)
	if outstanding <= 0:
		result["already_settled"].append(order.name)
		return invoice

	# Never credit the receivable by more than what is still open; the remainder
	# (small invoice, large courier fee) goes to the courier wallet, which is how
	# the historical import booked those cases.
	to_debtors = min(net, outstanding)
	excess = flt(net - to_debtors, 2)

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
				"account": config.finance,
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
		result["failed"].append(f"{order.name} charges: {str(exc)[:100]}")
		return invoice

	frappe.db.set_value(
		"Sales Order", order.name, "custom_postex_charges_entry", je.name, update_modified=False
	)
	frappe.db.set_value(
		"Sales Invoice",
		invoice.name,
		{
			"custom_postex_charges_entry": je.name,
			"custom_postex_account": config.name,
		},
		update_modified=False,
	)
	result["charges_entries"].append(je.name)

	# Verify-after-create: another run may have stored a different JE.
	winner = frappe.db.get_value("Sales Order", order.name, "custom_postex_charges_entry")
	if winner and winner != je.name:
		try:
			je.flags.ignore_permissions = True
			je.cancel()
			frappe.db.commit()
		except Exception:
			pass
		result["failed"].append(f"{order.name}: duplicate charges entry prevented")
		return frappe.get_doc("Sales Invoice", invoice.name)

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
		result["failed"].append(f"payment {group['cpr']}/{group['customer']}: {str(exc)[:100]}")
		return

	for sales_order in group["orders"]:
		frappe.db.set_value(
			"Sales Order",
			sales_order,
			{
				"custom_postex_payout_entry": pe.name,
				"custom_postex_settled_on": group["date"],
			},
			update_modified=False,
		)
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

	result["payments"].append(pe.name)
	result["settled"] += len(group["orders"])
	frappe.db.commit()

	# Verify-after-create on the first order of the group.
	winner = frappe.db.get_value("Sales Order", group["orders"][0], "custom_postex_payout_entry")
	if winner and winner != pe.name:
		try:
			pe.flags.ignore_permissions = True
			pe.cancel()
			frappe.db.commit()
		except Exception:
			pass
		# Our links are stale now: point them at the winner instead.
		for sales_order in group["orders"]:
			frappe.db.set_value(
				"Sales Order", sales_order, "custom_postex_payout_entry", winner,
				update_modified=False,
			)
		for ref in group["refs"]:
			frappe.db.set_value(
				"Sales Invoice", ref["reference_name"], "custom_postex_payout_entry", winner,
				update_modified=False,
			)
		frappe.db.commit()
		result["payments"].remove(pe.name)
		result["failed"].append(f"duplicate payment prevented {pe.name}")


def _mark_settled_on_order(sales_order, settled_on):
	frappe.db.set_value(
		"Sales Order", sales_order, "custom_postex_settled_on", settled_on, update_modified=False
	)
	frappe.db.commit()


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

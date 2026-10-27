"""Stock and revenue flows for one account: pickup, delivery, invoice, return.

Policy (unchanged from the single-company implementation):
  * pickup     : Stores -> In-Transit warehouse (Material Transfer)
  * delivered  : In-Transit -> customer (Delivery Note), then Sales Invoice
  * returned   : In-Transit -> Stores (Material Transfer) + courier cost
  * a returned order has no invoice at all (delivered => invoice only)
"""

import frappe
from frappe.utils import flt, nowdate

from .config import AccountConfig
from .constants import (
	MAX_DOCS_PER_HELPER,
	PAYMENT_NOT_PAID,
	PICKUP_STATUSES,
	POSTEX_COURIER,
)
from .utils import exclude_historical, log_error, net_rate, submit


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------
def _base_filters(config: AccountConfig) -> dict:
	"""Orders this account owns.

	The courier itself is checked in Python after loading the document: an empty
	Select is stored as NULL, and `IN ('', 'PostEx')` never matches NULL, so a
	SQL filter here would silently skip every order whose courier is still blank.
	"""
	return {
		"docstatus": 1,
		"company": config.company,
		"custom_postex_account": config.name,
	}


def _courier_is_ours(doc) -> bool:
	return (doc.get("custom_courier") or "") in ("", POSTEX_COURIER)


def _pickup_submitted(order) -> bool:
	entry = order.get("custom_postex_pickup_entry")
	if not entry:
		return False
	return frappe.db.get_value("Stock Entry", entry, "docstatus") == 1


def _pending_rows(so):
	rows = []
	for item in so.items:
		qty = flt(item.qty) - flt(item.delivered_qty)
		if qty > 0:
			rows.append((item, qty))
	return rows


def _claim_field(doctype, name, fieldname, value) -> bool:
	"""Verify-after-create: true when our document is the one stored."""
	frappe.db.set_value(doctype, name, fieldname, value)
	frappe.db.commit()
	winner = frappe.db.get_value(doctype, name, fieldname)
	return bool(winner) and winner == value


def _cancel_and_drop(doc):
	try:
		doc.flags.ignore_permissions = True
		doc.cancel()
	except Exception:
		pass
	try:
		frappe.delete_doc(doc.doctype, doc.name, force=True, ignore_permissions=True)
		frappe.db.commit()
	except Exception:
		log_error(
			"PostEx: could not remove duplicate document",
			f"{doc.doctype} {doc.name}\n{frappe.get_traceback()}",
		)


# ---------------------------------------------------------------------------
# 1. Pickup transfer
# ---------------------------------------------------------------------------
def pickup_transfer(config: AccountConfig, limit: int = MAX_DOCS_PER_HELPER) -> dict:
	"""Move stock from the order's warehouse into the account's transit warehouse."""
	result = {"created": [], "skipped": 0, "failed": []}

	filters = _base_filters(config)
	filters.update(
		{
			"custom_postex_pickup_entry": ("is", "not set"),
			"custom_postex_status": ("in", list(PICKUP_STATUSES)),
		}
	)
	filters.update(exclude_historical())

	orders = frappe.get_all(
		"Sales Order",
		filters=filters,
		fields=["name", "set_warehouse"],
		limit=limit,
		order_by="modified desc",
	)

	for order in orders:
		try:
			so = frappe.get_doc("Sales Order", order.name)
			if not _courier_is_ours(so):
				result["skipped"] += 1
				continue
			rows = []
			for item, qty in _pending_rows(so):
				source = (
					(item.warehouse or "").strip()
					or (so.set_warehouse or "").strip()
					or config.stores_warehouse
				)
				rows.append(
					{
						"item_code": item.item_code,
						"item_name": item.item_name,
						"description": item.description,
						"qty": qty,
						"uom": item.stock_uom,
						"stock_uom": item.stock_uom,
						"conversion_factor": 1,
						"s_warehouse": source,
						"t_warehouse": config.in_transit_warehouse,
					}
				)
			if not rows:
				result["skipped"] += 1
				continue

			se = frappe.get_doc(
				{
					"doctype": "Stock Entry",
					"stock_entry_type": "Material Transfer",
					"purpose": "Material Transfer",
					"company": so.company,
					"posting_date": nowdate(),
					"remark": f"PostEx pickup {so.name} / {so.get('shopify_order_number') or ''} "
					f"[{config.name}]",
					"items": rows,
				}
			)
			submit(se)

			if not _claim_field("Sales Order", so.name, "custom_postex_pickup_entry", se.name):
				_cancel_and_drop(se)
				result["failed"].append(f"{so.name}: duplicate pickup transfer prevented")
				continue

			result["created"].append(f"{so.name}={se.name}")
			frappe.db.commit()
		except Exception as exc:
			log_error("PostEx pickup transfer failed", f"{order.name}: {exc}", "Sales Order", order.name)
			result["failed"].append(f"{order.name}: {str(exc)[:120]}")

	frappe.db.commit()
	return result


# ---------------------------------------------------------------------------
# 2. Delivery note + invoice
# ---------------------------------------------------------------------------
def delivery_and_return(config: AccountConfig, limit: int = MAX_DOCS_PER_HELPER) -> dict:
	result = {
		"delivery_notes": [],
		"invoices": [],
		"returns": [],
		"return_charges": [],
		"failed": [],
	}
	_create_delivery_notes(config, result, limit)
	_create_invoices(config, result, limit)
	_process_returns(config, result, limit)
	return result


def _delivered_filters(config, **extra) -> dict:
	filters = _base_filters(config)
	filters["custom_postex_status"] = "Delivered"
	filters.update(exclude_historical())
	filters.update(extra)
	return filters


def _create_delivery_notes(config, result, limit):
	orders = frappe.get_all(
		"Sales Order",
		filters=_delivered_filters(config, custom_postex_delivery_note=("is", "not set")),
		fields=["name", "custom_postex_pickup_entry"],
		limit=limit,
		order_by="modified desc",
	)

	for order in orders:
		if not _pickup_submitted(order):
			result["failed"].append(f"{order.name}: no submitted pickup entry")
			continue
		try:
			so = frappe.get_doc("Sales Order", order.name)
			if not _courier_is_ours(so):
				continue
			dn = frappe.get_doc(
				{
					"doctype": "Delivery Note",
					"customer": so.customer,
					"customer_name": so.customer_name,
					"company": so.company,
					"posting_date": nowdate(),
					"ignore_pricing_rule": 1,
					"instructions": f"PostEx delivered {so.name} / {so.get('shopify_order_number') or ''} "
					f"[{config.name}]",
				}
			)
			for item, qty in _pending_rows(so):
				dn.append(
					"items",
					{
						"item_code": item.item_code,
						"item_name": item.item_name,
						"description": item.description,
						"qty": qty,
						"uom": item.uom,
						"stock_uom": item.stock_uom,
						"conversion_factor": 1,
						# net rate, so a discounted or zero-priced order keeps its amount
						"rate": net_rate(item.get("net_amount") or item.amount, qty),
						"price_list_rate": net_rate(item.get("net_amount") or item.amount, qty),
						"warehouse": config.in_transit_warehouse,
						"against_sales_order": so.name,
						"so_detail": item.name,
						"cost_center": item.cost_center or config.cost_center,
					},
				)
			if not dn.items:
				result["failed"].append(f"{so.name}: nothing left to deliver")
				continue

			submit(dn)
			if not _claim_field("Sales Order", so.name, "custom_postex_delivery_note", dn.name):
				_cancel_and_drop(dn)
				result["failed"].append(f"{so.name}: duplicate delivery note prevented")
				continue

			result["delivery_notes"].append(f"{so.name}={dn.name}")
			frappe.db.commit()
		except Exception as exc:
			log_error("PostEx delivery note failed", f"{order.name}: {exc}", "Sales Order", order.name)
			result["failed"].append(f"{order.name}: {str(exc)[:120]}")


def _create_invoices(config, result, limit):
	orders = frappe.get_all(
		"Sales Order",
		filters=_delivered_filters(
			config,
			custom_postex_delivery_note=("is", "set"),
			custom_postex_sales_invoice=("is", "not set"),
		),
		fields=["name", "custom_postex_delivery_note"],
		limit=limit,
		order_by="modified desc",
	)

	for order in orders:
		try:
			so = frappe.get_doc("Sales Order", order.name)
			if not _courier_is_ours(so):
				continue
			dn = frappe.get_doc("Delivery Note", order.custom_postex_delivery_note)
			if dn.docstatus != 1:
				result["failed"].append(f"{order.name}: delivery note not submitted")
				continue

			si = frappe.get_doc(
				{
					"doctype": "Sales Invoice",
					"customer": so.customer,
					"customer_name": so.customer_name,
					"company": so.company,
					"posting_date": nowdate(),
					"currency": so.currency,
					# No price list: the invoice must inherit the delivery note's
					# amounts, never re-price a zero/discounted line.
					"ignore_pricing_rule": 1,
					"custom_courier": POSTEX_COURIER,
					"custom_postex_account": config.name,
					"custom_postex_tracking_number": so.get("custom_postex_tracking_number"),
					"custom_postex_status": "Delivered",
					"custom_courier_payment_status": PAYMENT_NOT_PAID,
					"custom_postex_delivery_fee": flt(so.get("custom_postex_delivery_fee")),
					"custom_postex_delivery_tax": flt(so.get("custom_postex_delivery_tax")),
					"remarks": f"COD - PostEx {so.get('shopify_order_number') or so.name} delivered, "
					"cash collected by courier",
				}
			)
			# DeliveryNoteItem has no income_account; the account lives on the
			# Sales Order item, which is the correct source anyway.
			income_by_so_item = {item.name: item.get("income_account") for item in so.items}
			for dn_item in dn.items:
				qty = flt(dn_item.qty)
				amount = dn_item.get("net_amount") or dn_item.amount
				rate = net_rate(amount, qty)
				si.append(
					"items",
					{
						"item_code": dn_item.item_code,
						"item_name": dn_item.item_name,
						"description": dn_item.description,
						"qty": qty,
						"rate": rate,
						"price_list_rate": rate,
						"discount_percentage": 0,
						"discount_amount": 0,
						"uom": dn_item.uom,
						"stock_uom": dn_item.stock_uom,
						"conversion_factor": dn_item.conversion_factor,
						"warehouse": config.in_transit_warehouse,
						"delivery_note": dn.name,
						"dn_detail": dn_item.name,
						"sales_order": so.name,
						"so_detail": dn_item.so_detail,
						"cost_center": dn_item.cost_center or config.cost_center,
						"income_account": income_by_so_item.get(dn_item.so_detail)
						or dn_item.get("income_account"),
					},
				)
			if not si.items:
				result["failed"].append(f"{order.name}: delivery note has no items")
				continue
			for tax in so.taxes:
				si.append(
					"taxes",
					{
						"charge_type": tax.charge_type,
						"account_head": tax.account_head,
						"description": tax.description,
						"rate": tax.rate,
						"cost_center": tax.cost_center or config.cost_center,
					},
				)

			si.flags.ignore_permissions = True
			si.insert(ignore_permissions=True)

			# Last line of defence: never post an invoice that disagrees with the
			# delivery it bills (a re-priced or discounted line would show up here).
			expected = flt(dn.grand_total)
			if abs(flt(si.grand_total) - expected) > 0.01:
				si.delete()
				raise ValueError(
					f"invoice total {si.grand_total} differs from delivery note "
					f"{dn.grand_total}; not posted"
				)

			si.submit()
			if not _claim_field("Sales Order", so.name, "custom_postex_sales_invoice", si.name):
				_cancel_and_drop(si)
				result["failed"].append(f"{so.name}: duplicate invoice prevented")
				continue

			result["invoices"].append(f"{so.name}={si.name}")
			frappe.db.commit()
		except Exception as exc:
			log_error("PostEx sales invoice failed", f"{order.name}: {exc}", "Sales Order", order.name)
			result["failed"].append(f"{order.name}: {str(exc)[:120]}")


# ---------------------------------------------------------------------------
# 3. Return leg
# ---------------------------------------------------------------------------
def _process_returns(config, result, limit):
	filters = _base_filters(config)
	filters.update(exclude_historical())
	filters.update(
		{
			"custom_postex_status": "Returned",
			"custom_postex_delivery_note": ("is", "not set"),
			"custom_postex_sales_invoice": ("is", "not set"),
			"custom_postex_return_entry": ("is", "not set"),
		}
	)
	orders = frappe.get_all(
		"Sales Order",
		filters=filters,
		fields=[
			"name",
			"custom_postex_pickup_entry",
			"custom_postex_reversal_fee",
			"custom_postex_reversal_tax",
		],
		limit=limit,
		order_by="modified desc",
	)

	for order in orders:
		if not _pickup_submitted(order):
			continue
		try:
			so = frappe.get_doc("Sales Order", order.name)
			if not _courier_is_ours(so):
				continue
			_book_return_charges(config, so, order, result)
			_move_return_stock(config, so, result)
			frappe.db.commit()
		except Exception as exc:
			log_error("PostEx return transfer failed", f"{order.name}: {exc}", "Sales Order", order.name)
			result["failed"].append(f"{order.name}: {str(exc)[:120]}")


def _book_return_charges(config, so, order, result):
	"""A returned parcel cost the delivery attempt plus the reverse leg.

	No invoice exists (returned => no invoice), so the wallet takes the hit.
	"""
	delivery_cost = flt(so.get("custom_postex_delivery_fee")) + flt(
		so.get("custom_postex_delivery_tax")
	)
	reversal_cost = flt(order.custom_postex_reversal_fee) + flt(
		order.custom_postex_reversal_tax
	)
	total = delivery_cost + reversal_cost
	if total <= 0 or so.get("custom_postex_return_charges_entry"):
		return

	je = frappe.get_doc(
		{
			"doctype": "Journal Entry",
			"voucher_type": "Journal Entry",
			"company": so.company,
			"posting_date": nowdate(),
			"user_remark": f"PostEx return charges (delivery + reversal) "
			f"{so.get('shopify_order_number') or so.name} [{config.name}]",
			"accounts": [
				{
					"account": config.delivery_charges,
					"debit_in_account_currency": total,
					"cost_center": config.cost_center,
				},
				{
					"account": config.finance,
					"credit_in_account_currency": total,
					"cost_center": config.cost_center,
				},
			],
		}
	)
	submit(je)
	if not _claim_field("Sales Order", so.name, "custom_postex_return_charges_entry", je.name):
		_cancel_and_drop(je)
		result["failed"].append(f"{so.name}: duplicate return charges prevented")
		return
	result["return_charges"].append(je.name)


def _move_return_stock(config, so, result):
	rows = []
	for item, qty in _pending_rows(so):
		rows.append(
			{
				"item_code": item.item_code,
				"item_name": item.item_name,
				"description": item.description,
				"qty": qty,
				"uom": item.stock_uom,
				"stock_uom": item.stock_uom,
				"conversion_factor": 1,
				"s_warehouse": config.in_transit_warehouse,
				"t_warehouse": config.stores_warehouse,
			}
		)
	if not rows:
		return

	se = frappe.get_doc(
		{
			"doctype": "Stock Entry",
			"stock_entry_type": "Material Transfer",
			"purpose": "Material Transfer",
			"company": so.company,
			"posting_date": nowdate(),
			"remark": f"PostEx return {so.name} / {so.get('shopify_order_number') or ''} [{config.name}]",
			"items": rows,
		}
	)
	submit(se)
	if not _claim_field("Sales Order", so.name, "custom_postex_return_entry", se.name):
		_cancel_and_drop(se)
		result["failed"].append(f"{so.name}: duplicate return transfer prevented")
		return
	result["returns"].append(f"{so.name}={se.name}")

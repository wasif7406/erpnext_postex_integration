"""Resolution of a company's PostEx account and its account/warehouse mapping.

Every flow goes through here. No account, warehouse or cost center name is
ever hard-coded; the mapping rows of `PostEx Account` are the only source.
"""

import frappe
from frappe.utils import cint

from .constants import (
	ACCOUNT_RULES,
	ACCOUNT_ROLES,
	DEFAULT_WITHHOLDING_PERCENT,
	POSTEX_COURIER,
	REQUIRED_ROLES,
	ROLE_COST_CENTER,
	ROLE_DEBTORS,
	ROLE_DELIVERY_CHARGES,
	ROLE_FINANCE,
	ROLE_IN_TRANSIT,
	ROLE_STORES,
	ROLE_TAX_WITHHELD,
	ROLE_UNACCOUNTED,
	ROLE_UNSETTLED_COD,
	WAREHOUSE_ROLES,
)
from .exceptions import PostExConfigError

DOCTYPE = "PostEx Account"


# ---------------------------------------------------------------------------
# Account lookup
# ---------------------------------------------------------------------------
def get_account(account: str) -> "frappe.Document":
	"""Load one PostEx Account by name."""
	if not account:
		raise PostExConfigError("No PostEx Account given.")
	if not frappe.db.exists(DOCTYPE, account):
		raise PostExConfigError(f"PostEx Account {account} does not exist.")
	return frappe.get_doc(DOCTYPE, account)


def accounts_for_company(company: str, only_enabled: bool = True) -> list:
	filters = {"company": company}
	if only_enabled:
		filters["enabled"] = 1
	return frappe.get_all(
		DOCTYPE,
		filters=filters,
		fields=["name", "account_name", "is_default", "enabled", "enable_sync", "enable_auto_booking"],
		order_by="is_default desc, creation asc",
	)


def default_account(company: str) -> "frappe.Document":
	"""The company's default enabled account.

	One enabled account: it is the default, whether flagged or not.
	Several enabled accounts: exactly one must be flagged `is_default`, otherwise
	bookings and syncs would silently pick an arbitrary account, so we abort with
	a clear message instead of guessing.
	"""
	rows = accounts_for_company(company)
	if not rows:
		raise PostExConfigError(
			f"Company {company} has no enabled PostEx Account. "
			"Create one under PostEx Integration > PostEx Account."
		)
	if len(rows) == 1:
		return get_account(rows[0].name)

	flagged = [row for row in rows if cint(row.is_default)]
	if flagged:
		return get_account(flagged[0].name)

	raise PostExConfigError(
		f"Company {company} has {len(rows)} enabled PostEx Accounts and none is marked "
		"as Default. Set one as default (or disable the extra accounts) so bookings "
		"and syncs are unambiguous."
	)


def account_for_document(doc, account: str | None = None) -> "frappe.Document":
	"""Resolve the account that owns this Sales Order / Sales Invoice.

	Explicit document value wins, then the `account` argument, then the
	company default. A document may never point at another company's account.
	"""
	company = getattr(doc, "company", None)
	if not company:
		raise PostExConfigError(f"{doc.doctype} {doc.name} has no company.")
	from_doc = getattr(doc, "custom_postex_account", None)
	account = account or from_doc
	if account:
		acc = get_account(account)
		if acc.company != company:
			raise PostExConfigError(
				f"PostEx Account {acc.name} belongs to {acc.company}, not {company}."
			)
		return acc
	return default_account(company)


# ---------------------------------------------------------------------------
# Resolved configuration
# ---------------------------------------------------------------------------
class AccountConfig:
	"""A PostEx Account with its mapping resolved to concrete ERPNext records."""

	def __init__(self, account):
		if isinstance(account, str):
			account = get_account(account)
		self.account = account
		self.name = account.name
		self.company = account.company
		self.company_abbr = frappe.get_cached_value("Company", self.company, "abbr")
		self.courier = POSTEX_COURIER
		self.dry_run = bool(cint(account.get("dry_run")))
		self.withholding_percent = (
			account.get("withholding_percent")
			if account.get("withholding_percent") is not None
			else DEFAULT_WITHHOLDING_PERCENT
		)

		self._rows = {}
		for row in account.mapping or []:
			if row.role:
				self._rows[row.role] = row

		self.finance = self._account(ROLE_FINANCE)
		self.debtors = self._account(ROLE_DEBTORS)
		self.delivery_charges = self._account(ROLE_DELIVERY_CHARGES)
		self.tax_withheld = self._account(ROLE_TAX_WITHHELD)
		self.unsettled_cod = self._account(ROLE_UNSETTLED_COD)
		self.unaccounted = self._account(ROLE_UNACCOUNTED, required=False)

		self.in_transit_warehouse = self._warehouse(ROLE_IN_TRANSIT)
		self.stores_warehouse = self._warehouse(ROLE_STORES)
		self.cost_center = self._cost_center()

		self.base_url = (account.base_url or "").rstrip("/")
		self.token = account.get_password("api_token", raise_exception=False)

	# -- mapping primitives ------------------------------------------------
	def _row(self, role):
		return self._rows.get(role)

	def _account(self, role, required=True):
		row = self._row(role)
		value = (row.account or "").strip() if row else ""
		if not value:
			if required:
				raise PostExConfigError(
					f"PostEx Account {self.name}: mapping role '{role}' has no account selected."
				)
			return None
		return value

	def _warehouse(self, role):
		row = self._row(role)
		value = (row.warehouse or "").strip() if row else ""
		if not value:
			raise PostExConfigError(
				f"PostEx Account {self.name}: mapping role '{role}' has no warehouse selected."
			)
		return value

	def _cost_center(self):
		row = self._row(ROLE_COST_CENTER)
		value = (row.cost_center or "").strip() if row else ""
		if value:
			return value
		# Fall back to the company default; JE lines may also leave it blank.
		return frappe.get_cached_value("Company", self.company, "cost_center") or None

	def __repr__(self):
		return f"<PostExConfig {self.name} company={self.company}>"

	def as_dict(self) -> dict:
		return {
			"account": self.name,
			"company": self.company,
			"finance": self.finance,
			"debtors": self.debtors,
			"delivery_charges": self.delivery_charges,
			"tax_withheld": self.tax_withheld,
			"unsettled_cod": self.unsettled_cod,
			"unaccounted": self.unaccounted,
			"in_transit_warehouse": self.in_transit_warehouse,
			"stores_warehouse": self.stores_warehouse,
			"cost_center": self.cost_center,
		}


def get_config(company: str, account: str | None = None) -> AccountConfig:
	"""Resolve the config to use for a company / account."""
	if account:
		acc = get_account(account)
		if company and acc.company != company:
			raise PostExConfigError(
				f"PostEx Account {acc.name} belongs to {acc.company}, not {company}."
			)
		return AccountConfig(acc)
	if not company:
		raise PostExConfigError("A company is required to resolve a PostEx Account.")
	return AccountConfig(default_account(company))


def config_for_document(doc, account: str | None = None) -> AccountConfig:
	return AccountConfig(account_for_document(doc, account=account))


# ---------------------------------------------------------------------------
# Validation helpers used by the PostEx Account controller
# ---------------------------------------------------------------------------
def validate_mapping(doc):
	"""Structural validation of a PostEx Account's mapping table.

	Raises frappe.ValidationError (via frappe.throw) on hard errors, appends
	messages for warnings.
	"""
	seen_roles = set()
	seen_accounts = {}
	warnings = []

	for row in doc.mapping or []:
		role = (row.role or "").strip()
		if not role:
			frappe.throw("Every mapping row needs a role.")
		if role in seen_roles:
			frappe.throw(f"Mapping role '{role}' appears more than once.")
		seen_roles.add(role)

		values = [v for v in (row.account, row.warehouse, row.cost_center) if (v or "").strip()]
		if len(values) != 1:
			frappe.throw(
				f"Mapping row '{role}' must set exactly one of account, warehouse or cost center."
			)

		if role in ACCOUNT_ROLES:
			_validate_account(row, role, doc.company, seen_accounts, warnings)
		elif role in WAREHOUSE_ROLES:
			_validate_warehouse(row, role, doc.company)
		elif role == ROLE_COST_CENTER:
			_validate_cost_center(row, doc.company)
		else:
			frappe.throw(f"Unknown mapping role '{role}'.")

	for role in REQUIRED_ROLES:
		if role not in seen_roles:
			warnings.append(
				f"Mapping role '{role}' is not configured yet; the flows that need it "
				"will refuse to run until it is set."
			)

	for message in warnings:
		frappe.msgprint(message, indicator="orange", alert=True)


def _validate_account(row, role, company, seen_accounts, warnings):
	account = row.account
	meta = frappe.db.get_value("Account", account, ["company", "is_group", "root_type", "account_type"], as_dict=True)
	if not meta:
		frappe.throw(f"Account {account} does not exist.")
	if meta.company != company:
		frappe.throw(f"Account {account} belongs to {meta.company}, not {company}.")
	if meta.is_group:
		frappe.throw(f"Account {account} is a group account; select a leaf account.")

	root_types, account_types, allow_blank_type = ACCOUNT_RULES.get(role, ((), (), True))
	if root_types and meta.root_type not in root_types:
		frappe.throw(
			f"Role '{role}' needs an account of type {' / '.join(root_types)}; "
			f"{account} is {meta.root_type}."
		)
	if account_types:
		if meta.account_type not in account_types:
			frappe.throw(
				f"Role '{role}' needs account_type {' / '.join(account_types)}; "
				f"{account} is '{meta.account_type or 'none'}'."
			)
	elif not allow_blank_type and meta.account_type:
		warnings.append(f"Role '{role}': {account} has account_type '{meta.account_type}'.")

	if role in (ROLE_DEBTORS, ROLE_FINANCE) and meta.account_type == "Receivable" and role == ROLE_FINANCE:
		frappe.throw(f"{account} is a Receivable account; it cannot be the courier wallet.")

	other = seen_accounts.get(account)
	if other and other != role:
		warnings.append(
			f"Account {account} is mapped to both '{other}' and '{role}'. "
			"That is allowed but usually a mistake."
		)
	seen_accounts[account] = role

	# Two accounts of different companies must never share an account
	clash = frappe.db.sql(
		"""
		select parent, role from `tabPostEx Account Mapping`
		where account = %s and parent != %s and parenttype = 'PostEx Account'
		""",
		(account, row.parent or ""),
		as_dict=True,
	)
	for other_row in clash:
		other_company = frappe.db.get_value("PostEx Account", other_row.parent, "company")
		if other_company and other_company != company:
			frappe.throw(
				f"Account {account} is already used by PostEx Account {other_row.parent} "
				f"of company {other_company}. Accounts cannot be shared between companies."
			)


def _validate_warehouse(row, role, company):
	meta = frappe.db.get_value("Warehouse", row.warehouse, ["company", "is_group", "warehouse_type"], as_dict=True)
	if not meta:
		frappe.throw(f"Warehouse {row.warehouse} does not exist.")
	if meta.company != company:
		frappe.throw(f"Warehouse {row.warehouse} belongs to {meta.company}, not {company}.")
	if meta.is_group:
		frappe.throw(f"Warehouse {row.warehouse} is a group warehouse; select a leaf warehouse.")
	if role == ROLE_IN_TRANSIT and (meta.warehouse_type or "") != "Transit":
		frappe.throw(
			f"Role '{role}' needs a warehouse with Warehouse Type 'Transit'; "
			f"{row.warehouse} is '{meta.warehouse_type or 'none'}'."
		)
	if role == ROLE_STORES and (meta.warehouse_type or "") == "Transit":
		frappe.throw(f"Role '{role}' cannot be a Transit warehouse ({row.warehouse}).")


def _validate_cost_center(row, company):
	meta = frappe.db.get_value("Cost Center", row.cost_center, ["company", "is_group"], as_dict=True)
	if not meta:
		frappe.throw(f"Cost Center {row.cost_center} does not exist.")
	if meta.company != company:
		frappe.throw(f"Cost Center {row.cost_center} belongs to {meta.company}, not {company}.")
	if meta.is_group:
		frappe.throw(f"Cost Center {row.cost_center} is a group; select a leaf cost center.")


def ensure_single_default(doc):
	"""Only one default PostEx Account per company."""
	if not cint(doc.is_default):
		return
	others = frappe.get_all(
		DOCTYPE,
		filters={"company": doc.company, "is_default": 1, "name": ("!=", doc.name or "")},
		pluck="name",
	)
	for name in others:
		frappe.db.set_value(DOCTYPE, name, "is_default", 0, update_modified=False)

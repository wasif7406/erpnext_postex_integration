import frappe
from frappe import _
from frappe.model.document import Document

from postex_integration.config import ensure_single_default, validate_mapping


class PostExAccount(Document):
	def validate(self):
		self.validate_company()
		self.validate_name_per_company()
		ensure_single_default(self)
		validate_mapping(self)
		self.validate_warnings()

	def on_update(self):
		frappe.clear_cache()

	def on_trash(self):
		in_use = frappe.db.count("Sales Order", {"custom_postex_account": self.name})
		if in_use:
			frappe.throw(
				_(
					"PostEx Account {0} is used by {1} Sales Order(s). "
					"Disable it instead of deleting it."
				).format(self.name, in_use)
			)

	# ------------------------------------------------------------------
	def validate_company(self):
		if not self.company:
			frappe.throw(_("Company is required."))
		if not frappe.db.exists("Company", self.company):
			frappe.throw(_("Company {0} does not exist.").format(self.company))

	def validate_name_per_company(self):
		duplicate = frappe.db.get_value(
			"PostEx Account",
			{
				"company": self.company,
				"account_name": self.account_name,
				"name": ("!=", self.name or ""),
			},
			"name",
		)
		if duplicate:
			frappe.throw(
				_("Account name {0} already exists for {1} ({2}).").format(
					self.account_name, self.company, duplicate
				)
			)

	def validate_warnings(self):
		if self.enabled and not self.is_default and not self._company_has_default():
			frappe.msgprint(
				_(
					"{0} now has several enabled PostEx Accounts and none is Default. "
					"Automatic flows (auto booking, unassigned orders) will refuse to run "
					"until one is flagged as Default."
				).format(self.company),
				indicator="orange",
				alert=True,
			)
		if self.dry_run and self.enable_auto_booking:
			frappe.msgprint(
				_("Dry run is on, so auto booking will not send anything to PostEx."),
				indicator="orange",
				alert=True,
			)
		if self.enable_auto_booking and not (self.pickup_address or "").strip():
			frappe.msgprint(
				_("Auto booking is on but no pickup address is set; bookings will fail."),
				indicator="orange",
				alert=True,
			)
		if self.enabled and not self.get_password("api_token", raise_exception=False):
			frappe.msgprint(
				_("No API token set: sync, booking and settlement will be skipped."),
				indicator="orange",
				alert=True,
			)
		if not self.get_password("webhook_secret", raise_exception=False):
			frappe.msgprint(
				_("No webhook secret set: the PostEx webhook endpoint will reject calls."),
				indicator="orange",
				alert=True,
			)

	def _company_has_default(self) -> bool:
		"""True when another enabled account of this company is flagged Default."""
		return bool(
			frappe.db.exists(
				"PostEx Account",
				{
					"company": self.company,
					"enabled": 1,
					"is_default": 1,
					"name": ("!=", self.name or ""),
				},
			)
		)

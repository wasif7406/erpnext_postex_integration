import frappe
from frappe.model.document import Document


class PostExSettlementUpload(Document):
	"""One settlement-log reconciliation. Check first, then apply.

	The counters and the exceptions table are written by
	`postex_integration.api.check_settlement_upload` / `..._apply_settlement_upload`;
	nothing is computed here, so a saved record always shows the outcome of the
	run it describes.
	"""

	def validate(self):
		if self.from_date and self.account:
			pass
		if self.status == "Applied":
			# A settled file is history: editing it would make the counters lie.
			before = self.get_doc_before_save()
			if before and before.status == "Applied":
				for field in ("settlement_file", "account", "from_date"):
					if before.get(field) != self.get(field):
						frappe.throw(
							frappe._(
								"This upload has already been applied; its file and cutover "
								"cannot change. Create a new upload instead."
							)
						)

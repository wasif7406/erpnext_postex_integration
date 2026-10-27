app_name = "postex_integration"
app_title = "PostEx Integration"
app_publisher = "Aqreno Nova"
app_description = "Multi-company, multi-account PostEx courier integration"
app_email = "dev@aqrenonova.com"
app_license = "mit"
app_version = "0.1.1"

required_apps = ["erpnext"]

# ---------------------------------------------------------------------------
# Form scripts
# ---------------------------------------------------------------------------
doctype_js = {
	"Sales Order": "public/js/sales_order.js",
	"Sales Invoice": "public/js/sales_invoice.js",
}

# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------
# before_install runs *before* the app's doctypes are synced, which is what
# lets us adopt legacy UI-created "Custom" doctypes instead of colliding with
# them (see install.before_install).
before_install = "postex_integration.install.before_install"
after_install = "postex_integration.install.after_install"
after_migrate = "postex_integration.install.after_migrate"

# ---------------------------------------------------------------------------
# Document events
# ---------------------------------------------------------------------------
doc_events = {
	"Sales Order": {
		"validate": "postex_integration.booking.validate_account_change",
		"on_submit": "postex_integration.booking.on_sales_order_submit",
	}
}

# ---------------------------------------------------------------------------
# Scheduler
# ---------------------------------------------------------------------------
# Every job iterates the enabled PostEx Accounts and enqueues one short
# background job per account, so one slow/broken account cannot delay the
# others (and each job takes that account's filelock).
scheduler_events = {
	"cron": {
		"*/15 * * * *": [
			"postex_integration.tasks.sync_all_accounts",
		],
		"*/30 * * * *": [
			"postex_integration.tasks.settle_all_accounts",
		],
	},
	"daily": [
		"postex_integration.tasks.prune_logs",
	],
}

# ---------------------------------------------------------------------------
# Nothing is shipped as fixtures on purpose: custom fields are created from
# code (postex_integration/fields.py) so that an existing site's field
# positions/labels are never overwritten by a migrate.
# ---------------------------------------------------------------------------
fixtures = []

# Translations / Jinja helpers are not needed.

// PostEx Account: filter every mapping dropdown by the selected company and
// expose the operational actions (provision, webhook URL, sync, settle).
frappe.ui.form.on("PostEx Account", {
	refresh(frm) {
		set_mapping_queries(frm);
		if (frm.is_new()) {
			return;
		}
		frm.add_custom_button(__("Create default accounts"), () => {
			frappe.confirm(
				__(
					"Create the missing ledger accounts and the transit warehouse for {0} and map them?",
					[frm.doc.company]
				),
				() => {
					frappe.call({
						method: "postex_integration.api.create_default_accounts",
						args: { account: frm.doc.name },
						freeze: true,
						freeze_message: __("Provisioning accounts..."),
						callback: (r) => {
							const res = r.message || {};
							frappe.msgprint({
								title: __("Provisioning result"),
								message: [
									res.created && res.created.length
										? __("Created: ") + res.created.join(", ")
										: __("Nothing created"),
									res.mapped && res.mapped.length
										? __("Mapped: ") + res.mapped.join(", ")
										: "",
									res.missing && res.missing.length
										? __("Missing: ") + res.missing.join(" | ")
										: "",
								]
									.filter(Boolean)
									.join("<br>"),
								indicator: res.missing && res.missing.length ? "orange" : "green",
							});
							frm.reload_doc();
						},
					});
				}
			);
		});

		frm.add_custom_button(__("Show webhook URL"), () => {
			frappe.call({
				method: "postex_integration.api.webhook_url",
				args: { account: frm.doc.name },
				callback: (r) => {
					const url = (r.message || {}).url;
					if (!url) return;
					frappe.msgprint({
						title: __("PostEx webhook endpoint"),
						message: `<code style="word-break:break-all">${frappe.utils.escape_html(
							url
						)}</code><br><br>${__(
							"Register this URL in the PostEx merchant dashboard for this account. " +
								"Until it is registered, status changes only arrive with the 15-minute poll."
						)}`,
						wide: true,
						primary_action: {
							label: __("Copy URL"),
							action() {
								frappe.utils.copy_to_clipboard(url);
								frappe.show_alert({
									message: __("Webhook URL copied."),
									indicator: "green",
								});
							},
						},
					});
				},
			});
		});

		warn_when_webhook_is_silent(frm);

		frm.add_custom_button(
			__("Assign missing orders"),
			() => {
				frappe.confirm(
					__(
						"Label every PostEx order of {0} that has no PostEx Account yet? " +
							"A dry run is shown first.",
						[frm.doc.company]
					),
					() => assign_missing_orders(frm, 1)
				);
			},
			__("PostEx")
		);

		frm.add_custom_button(
			__("Sync now"),
			() => {
				frappe.call({
					method: "postex_integration.api.sync_now",
					args: { account: frm.doc.name },
					freeze: true,
					freeze_message: __("Syncing {0}...", [frm.doc.account_name]),
					callback: (r) => {
						const res = (r.message || {})[frm.doc.name] || {};
						frappe.msgprint({
							title: __("Sync result"),
							message: frappe.utils.escape_html(res.summary || JSON.stringify(res)),
							wide: true,
						});
						frm.reload_doc();
					},
				});
			},
			__("PostEx")
		);

		frm.add_custom_button(
			__("Settle now"),
			() => {
				frappe.call({
					method: "postex_integration.api.reconcile_payouts",
					args: { account: frm.doc.name },
					freeze: true,
					freeze_message: __("Checking settlements..."),
					callback: (r) => {
						const res = (r.message || {})[frm.doc.name] || {};
						frappe.msgprint({
							title: __("Settlement result"),
							message: __("Checked {0}, settled {1}, payments {2}", [
								res.checked || 0,
								res.settled || 0,
								(res.payments || []).length,
							]),
						});
					},
				});
			},
			__("PostEx")
		);
	},

	company(frm) {
		set_mapping_queries(frm);
	},
});

function set_mapping_queries(frm) {
	if (!frm.fields_dict.mapping) return;
	const filters = () => ({ company: frm.doc.company, is_group: 0 });
	frm.set_query("account", "mapping", filters);
	frm.set_query("warehouse", "mapping", filters);
	frm.set_query("cost_center", "mapping", filters);
}

function warn_when_webhook_is_silent(frm) {
	if (!frm.doc.last_webhook_on || !frm.doc.enable_sync) return;
	const hours = -moment(frm.doc.last_webhook_on).diff(moment(), "hours");
	if (hours > 24) {
		frm.set_intro(
			__(
				"No webhook call received since {0}. Check that the URL registered in the PostEx dashboard matches this account (press 'Show webhook URL').",
				[frm.doc.last_webhook_on]
			),
			"orange"
		);
	}
}

function assign_missing_orders(frm, dry_run) {
	frappe.call({
		method: "postex_integration.api.backfill_accounts",
		args: { account: frm.doc.name, dry_run: dry_run },
		freeze: true,
		freeze_message: __("Scanning orders..."),
		callback: (r) => {
			const report = r.message || {};
			const block = (report.accounts || {})[frm.doc.name] || {};
			const lines = [
				__("Submitted orders: {0}", [block.submitted_orders || 0]),
				__("Without an account: {0}", [block.without_account || 0]),
				__("PostEx candidates: {0}", [block.postex_candidates || 0]),
				dry_run
					? __("Would assign: {0}", [block.would_assign || 0])
					: __("Assigned: {0}", [block.assigned || 0]),
				block.other_courier
					? __("Skipped (other courier): {0}", [block.other_courier])
					: "",
				block.skipped || "",
			].filter(Boolean);

			const options = {
				title: dry_run ? __("Assign missing orders (dry run)") : __("Assign missing orders"),
				message: lines.map((line) => frappe.utils.escape_html(line)).join("<br>"),
				indicator: block.skipped ? "orange" : "blue",
			};
			if (dry_run && (block.would_assign || 0) > 0) {
				options.primary_action = {
					label: __("Apply"),
					action() {
						assign_missing_orders(frm, 0);
					},
				};
			}
			frappe.msgprint(options);
			if (!dry_run) frm.reload_doc();
		},
	});
}

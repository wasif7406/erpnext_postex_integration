// Courier Detail on Sales Order.
//  * shows only the fee set of the selected courier (no courier => nothing)
//  * PostEx Account is selectable, filtered to the order's company
//  * "Create PostEx Booking" asks which account to use when the company has more
//    than one enabled PostEx account
frappe.ui.form.on("Sales Order", {
	refresh(frm) {
		toggle_courier_fields(frm);
		set_account_query(frm);
		bind_booking_button(frm);
	},
	custom_courier(frm) {
		toggle_courier_fields(frm);
		bind_booking_button(frm);
	},
	company(frm) {
		set_account_query(frm);
		clear_account_if_other_company(frm);
	},
});

function clear_account_if_other_company(frm) {
	const account = frm.doc.custom_postex_account;
	if (!account) return;
	frappe.db.get_value("PostEx Account", account, "company").then((result) => {
		// frappe.db.get_value resolves to the value dict (or {message: {...}}).
		const data = result && result.message ? result.message : result || {};
		const account_company = data.company;
		if (account_company && account_company !== frm.doc.company) {
			frm.set_value("custom_postex_account", "");
			frappe.show_alert({
				message: __("PostEx Account cleared because the company changed."),
				indicator: "orange",
			});
		}
	});
}

function set_account_query(frm) {
	if (!frm.fields_dict.custom_postex_account) return;
	frm.set_query("custom_postex_account", () => ({
		filters: { company: frm.doc.company, enabled: 1 },
	}));
}

function toggle_courier_fields(frm) {
	const courier = frm.doc.custom_courier || "";
	const sets = {
		PostEx: [
			"custom_postex_delivery_fee",
			"custom_postex_delivery_tax",
			"custom_postex_reversal_fee",
			"custom_postex_reversal_tax",
		],
		"Quick Delivery": [
			"custom_qd_delivery_fee",
			"custom_qd_delivery_tax",
			"custom_qd_reversal_fee",
			"custom_qd_reversal_tax",
		],
		"Fast And Quick": [
			"custom_fq_delivery_fee",
			"custom_fq_delivery_tax",
			"custom_fq_reversal_fee",
			"custom_fq_reversal_tax",
		],
	};

	Object.keys(sets).forEach((name) => {
		sets[name].forEach((fieldname) => {
			if (frm.fields_dict[fieldname]) {
				frm.toggle_display(fieldname, courier === name);
			}
		});
	});

	const show_button =
		!frm.doc.custom_postex_tracking_number && (courier === "" || courier === "PostEx");
	if (frm.fields_dict.custom_create_postex_booking) {
		frm.toggle_display("custom_create_postex_booking", show_button);
	}
}

function bind_booking_button(frm) {
	const field = frm.fields_dict.custom_create_postex_booking;
	if (!field || !field.$input) return;
	field.$input.off("click").on("click", function () {
		if (frm.doc.custom_postex_tracking_number) return;
		if (frm.doc.docstatus === 0) {
			frappe.msgprint(__("Submit the Sales Order before creating a PostEx booking."));
			return;
		}
		choose_account_then_book(frm);
	});
}

function choose_account_then_book(frm) {
	frappe.call({
		method: "postex_integration.api.get_booking_accounts",
		args: { sales_order: frm.doc.name },
		callback: (r) => {
			const data = r.message || {};
			const accounts = data.accounts || [];

			if (!accounts.length) {
				frappe.msgprint({
					title: __("No PostEx account"),
					message: __("Company {0} has no enabled PostEx Account.", [
						frappe.utils.escape_html(data.company || ""),
					]),
					indicator: "red",
				});
				return;
			}

			// An account already stamped on the order wins: it was chosen
			// deliberately (or set by a previous booking).
			if (data.current) {
				confirm_booking(frm, data.current, account_label(accounts, data.current));
				return;
			}

			if (accounts.length === 1) {
				confirm_booking(frm, accounts[0].name, accounts[0].account_name);
				return;
			}

			const fallback = accounts.find((a) => a.is_default) || accounts[0];
			frappe.prompt(
				[
					{
						fieldname: "account",
						fieldtype: "Select",
						label: __("PostEx Account"),
						reqd: 1,
						default: fallback.name,
						options: accounts.map((account) => ({
							value: account.name,
							label:
								account.account_name +
								(account.is_default ? " (" + __("default") + ")" : ""),
						})),
					},
				],
				(values) => {
					confirm_booking(frm, values.account, account_label(accounts, values.account));
				},
				__("Choose PostEx account"),
				__("Book")
			);
		},
	});
}

function account_label(accounts, name) {
	const row = (accounts || []).find((account) => account.name === name);
	return row ? row.account_name : name;
}

function confirm_booking(frm, account, label) {
	frappe.confirm(__("Book this order on PostEx account {0}?", [label || account]), function () {
		frappe.call({
			method: "postex_integration.api.book_order",
			args: { sales_order: frm.doc.name, account: account },
			freeze: true,
			freeze_message: __("Creating PostEx booking..."),
			callback: function (r) {
				const result = r.message || {};
				if (result.ok) {
					frappe.show_alert({
						message: __("Booked: {0}", [result.tracking]),
						indicator: "green",
					});
					frm.reload_doc();
				} else {
					frappe.msgprint({
						title: __("PostEx booking failed"),
						message:
							frappe.utils.escape_html(result.error || __("Unknown error")) +
							(result.response
								? "<br><small>" +
								  frappe.utils.escape_html(result.response) +
								  "</small>"
								: ""),
						indicator: "red",
					});
				}
			},
		});
	});
}

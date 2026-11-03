// Courier Detail on Sales Order.
//  * shows only the fee set of the selected courier (no courier => nothing)
//  * PostEx Account is selectable, filtered to the order's company
//  * "Create PostEx Booking" asks which account to use when the company has more
//    than one enabled PostEx account
//  * "Fetch PostEx Status" pulls the live status of this one parcel from PostEx
//    (only for PostEx orders that already carry a tracking number)
//  * "Cancel PostEx Booking" cancels the booking at PostEx while nothing has
//    been delivered or invoiced (e.g. the customer cancelled the order)
frappe.ui.form.on("Sales Order", {
	refresh(frm) {
		toggle_courier_fields(frm);
		set_account_query(frm);
		bind_booking_button(frm);
		bind_fetch_button(frm);
		bind_cancel_button(frm);
		add_order_actions(frm);
		show_banners(frm);
	},
	custom_courier(frm) {
		toggle_courier_fields(frm);
		bind_booking_button(frm);
		bind_fetch_button(frm);
		bind_cancel_button(frm);
		add_order_actions(frm);
		show_banners(frm);
	},
	company(frm) {
		set_account_query(frm);
		clear_account_if_other_company(frm);
	},
});

// ---------------------------------------------------------------------------
// Banners (quarantine / cancelled booking)
// ---------------------------------------------------------------------------
function show_banners(frm) {
	const messages = [];
	if (frm.doc.custom_postex_needs_attention) {
		messages.push(
			__(
				"Quarantined after {0} consecutive sync failures: this order is skipped by every automatic sweep. Fix the cause, then press 'Re-queue for sync'.",
				[frm.doc.custom_postex_sync_failures || 5]
			)
		);
	}
	if (frm.doc.custom_postex_booking_cancelled) {
		const reason = frm.doc.custom_postex_cancel_reason
			? " (" + frappe.utils.escape_html(frm.doc.custom_postex_cancel_reason) + ")"
			: "";
		messages.push(
			__("PostEx booking cancelled on {0}{1}. Use 'Reopen PostEx Booking' if that was a mistake.", [
				frm.doc.custom_postex_cancelled_on || "",
				reason,
			])
		);
	}
	if (frm.doc.custom_postex_booking_doubt) {
		const reference =
			frm.doc.shopify_order_number || frm.doc.po_no || frm.doc.name;
		messages.push(
			__(
				"Booking unconfirmed: the last PostEx call failed after it may have reached PostEx, so it was not retried. Check reference {0} at PostEx — if the order exists, fetch its status; if it does not, press 'Clear booking doubt'.",
				[frappe.utils.escape_html(reference)]
			)
		);
	}
	if (!messages.length) return;
	const urgent =
		frm.doc.custom_postex_needs_attention || frm.doc.custom_postex_booking_doubt;
	frm.set_intro(messages.join(" "), urgent ? "red" : "blue");
}

function add_order_actions(frm) {
	frm.remove_custom_button(__("Fetch PostEx Status"), __("PostEx"));
	frm.remove_custom_button(__("Re-queue for sync"), __("PostEx"));
	frm.remove_custom_button(__("Reopen PostEx Booking"), __("PostEx"));
	frm.remove_custom_button(__("Clear booking doubt"), __("PostEx"));
	frm.remove_custom_button(__("Link PostEx Booking"), __("PostEx"));
	if (frm.doc.docstatus !== 1) return;

	// Fallback for sites where the Button field has not been migrated in yet.
	if (
		!frm.fields_dict.custom_fetch_postex_status &&
		frm.doc.custom_courier === "PostEx" &&
		frm.doc.custom_postex_tracking_number
	) {
		frm.add_custom_button(
			__("Fetch PostEx Status"),
			() => fetch_postex_status(frm),
			__("PostEx")
		);
	}

	if (frm.doc.custom_postex_booking_cancelled) {
		frm.add_custom_button(
			__("Reopen PostEx Booking"),
			() => {
				frappe.confirm(
					__(
						"Clear the cancellation and put this order back into the automatic sync? " +
							"Use this only if the booking was cancelled by mistake."
					),
					() => {
						frappe.call({
							method: "postex_integration.api.reopen_booking",
							args: { sales_order: frm.doc.name },
							freeze: true,
							callback: () => {
								frappe.show_alert({
									message: __(
										"Booking reopened; the status will be re-read from PostEx."
									),
									indicator: "green",
								});
								frm.reload_doc();
							},
						});
					}
				);
			},
			__("PostEx")
		);
	}

	if (!frm.doc.custom_postex_tracking_number && !frm.doc.custom_postex_booking_cancelled) {
		frm.add_custom_button(
			__("Link PostEx Booking"),
			() => link_existing_booking(frm),
			__("PostEx")
		);
	}

	if (frm.doc.custom_postex_booking_doubt) {
		frm.add_custom_button(
			__("Clear booking doubt"),
			() => {
				frappe.confirm(
					__(
						"Only continue after checking that PostEx has NO order for this " +
							"reference. Clearing this allows a new booking, which would create a " +
							"second parcel if the first one already exists. Continue?"
					),
					() => {
						frappe.call({
							method: "postex_integration.api.clear_booking_doubt",
							args: { sales_order: frm.doc.name },
							freeze: true,
							callback: () => {
								frappe.show_alert({
									message: __("Booking doubt cleared; you can book again."),
									indicator: "green",
								});
								frm.reload_doc();
							},
						});
					}
				);
			},
			__("PostEx")
		);
	}

	if (frm.doc.custom_postex_needs_attention || (frm.doc.custom_postex_sync_failures || 0) > 0) {
		frm.add_custom_button(
			__("Re-queue for sync"),
			() => {
				frappe.confirm(
					__("Clear the failure counter and put this order back in the automatic sync?"),
					() => {
						frappe.call({
							method: "postex_integration.api.requeue_order",
							args: { sales_order: frm.doc.name },
							callback: () => {
								frappe.show_alert({
									message: __("Re-queued for the next sync run."),
									indicator: "green",
								});
								frm.reload_doc();
							},
						});
					}
				);
			},
			__("PostEx")
		);
	}
}

// ---------------------------------------------------------------------------
// Manual matching: link this order to a booking that already exists at PostEx
// ---------------------------------------------------------------------------
function link_existing_booking(frm) {
	frappe.prompt(
		[
			{
				fieldname: "search",
				fieldtype: "Data",
				label: __("Reference, phone or customer"),
				default: frm.doc.shopify_order_number || frm.doc.po_no || "",
				description: __(
					"The reference PostEx knows this order by. A phone number or the customer name works too."
				),
			},
		],
		(values) => pick_existing_booking(frm, values.search),
		__("Find the PostEx booking"),
		__("Search")
	);
}

function pick_existing_booking(frm, search) {
	frappe.call({
		method: "postex_integration.api.find_bookings",
		args: { sales_order: frm.doc.name, search: search },
		freeze: true,
		freeze_message: __("Searching PostEx..."),
		callback: (r) => {
			const rows = r.message || [];
			// bookings already attached to another order cannot be linked again
			const choices = rows.filter((row) => !row.linked_to);
			if (!rows.length) {
				frappe.msgprint({
					title: __("No booking found"),
					message: __(
						"PostEx has no order matching {0}. Try the phone number or the customer name.",
						[frappe.utils.escape_html(search || "")]
					),
					indicator: "orange",
				});
				return;
			}
			if (!choices.length) {
				frappe.msgprint({
					title: __("Already linked"),
					message: __(
						"Every matching booking is already attached to another Sales Order."
					),
					indicator: "orange",
				});
				return;
			}

			frappe.prompt(
				[
					{
						fieldname: "tracking",
						fieldtype: "Select",
						label: __("Booking"),
						reqd: 1,
						options: choices.map((row) => ({
							value: row.tracking_number,
							label: [
								row.reference,
								row.tracking_number,
								row.status,
								row.date,
								row.amount ? format_currency(row.amount) : "",
							]
								.filter(Boolean)
								.join(" · "),
						})),
					},
				],
				(values) => confirm_existing_booking(frm, values.tracking),
				__("Link this booking"),
				__("Link")
			);
		},
	});
}

function confirm_existing_booking(frm, tracking) {
	frappe.confirm(
		__("Link tracking {0} to this order?", [frappe.utils.escape_html(tracking)]),
		() => {
			frappe.call({
				method: "postex_integration.api.link_booking",
				args: { sales_order: frm.doc.name, tracking: tracking },
				freeze: true,
				callback: (r) => {
					const res = r.message || {};
					if (!res.ok) {
						frappe.msgprint({
							title: __("Not linked"),
							message: frappe.utils.escape_html(res.error || ""),
							indicator: "red",
						});
						return;
					}
					frappe.show_alert({
						message: __("Linked: {0} ({1})", [res.tracking, res.status]),
						indicator: "green",
					});
					frm.reload_doc();
				},
			});
		}
	);
}

// ---------------------------------------------------------------------------
// Manual status fetch (Courier Detail > "Fetch PostEx Status")
// ---------------------------------------------------------------------------
function bind_fetch_button(frm) {
	const field = frm.fields_dict.custom_fetch_postex_status;
	if (!field || !field.$input) return;
	field.$input.off("click").on("click", function () {
		fetch_postex_status(frm);
	});
}

// ---------------------------------------------------------------------------
// Cancel a booking (Courier Detail > "Cancel PostEx Booking")
// ---------------------------------------------------------------------------
function bind_cancel_button(frm) {
	const field = frm.fields_dict.custom_cancel_postex_booking;
	if (!field || !field.$input) return;
	field.$input.off("click").on("click", function () {
		cancel_postex_booking(frm);
	});
}

function cancel_postex_booking(frm) {
	if (frm.doc.docstatus === 0) {
		frappe.msgprint(__("Submit the Sales Order before cancelling its PostEx booking."));
		return;
	}
	if (frm.doc.custom_postex_booking_cancelled) {
		frappe.show_alert({ message: __("This booking is already cancelled."), indicator: "blue" });
		return;
	}
	if (frm.doc.custom_courier !== "PostEx") {
		frappe.msgprint({
			title: __("Not a PostEx order"),
			message: __("Courier Issued is {0}; only PostEx bookings can be cancelled here.", [
				frappe.utils.escape_html(frm.doc.custom_courier || __("empty")),
			]),
			indicator: "orange",
		});
		return;
	}
	if (!frm.doc.custom_postex_tracking_number) {
		frappe.msgprint({
			title: __("Nothing to cancel"),
			message: __("This order has no PostEx tracking number yet."),
			indicator: "orange",
		});
		return;
	}

	frappe.prompt(
		[
			{
				fieldname: "reason",
				fieldtype: "Select",
				label: __("Reason"),
				reqd: 1,
				default: "Customer cancelled the order",
				options: [
					"Customer cancelled the order",
					"Duplicate booking",
					"Wrong address",
					"Out of stock",
					"Other",
				],
			},
			{
				fieldname: "note",
				fieldtype: "Small Text",
				label: __("Note (optional)"),
			},
		],
		(values) => {
			const reason = values.note
				? values.reason + ": " + values.note
				: values.reason;
			frappe.confirm(
				__(
					"Cancel booking {0} at PostEx? The parcel is cancelled on the courier side " +
						"and removed from the automatic sync.",
					[frm.doc.custom_postex_tracking_number]
				),
				() => {
					frappe.call({
						method: "postex_integration.api.cancel_booking",
						args: { sales_order: frm.doc.name, reason: reason },
						freeze: true,
						freeze_message: __("Cancelling the PostEx booking..."),
						callback: (r) => {
							const res = r.message || {};
							if (!res.ok) {
								frappe.msgprint({
									title: __("Booking not cancelled"),
									message: frappe.utils.escape_html(
										res.error || __("Unknown error")
									),
									indicator: "red",
								});
								return;
							}
							if (res.skipped) {
								frappe.show_alert({
									message: __("Already cancelled."),
									indicator: "blue",
								});
								frm.reload_doc();
								return;
							}
							const lines = [
								__("Booking {0} is cancelled.", [res.tracking || ""]),
							];
							if (res.note) lines.push(res.note);
							if (res.next_step) lines.push(res.next_step);
							frappe.msgprint({
								title: __("PostEx booking cancelled"),
								message: lines
									.map((line) => frappe.utils.escape_html(line))
									.join("<br>"),
								indicator: "green",
							});
							frm.reload_doc();
						},
					});
				}
			);
		},
		__("Cancel PostEx booking"),
		__("Continue")
	);
}

function fetch_postex_status(frm) {
	if (frm.doc.custom_courier !== "PostEx") {
		frappe.msgprint({
			title: __("Not a PostEx order"),
			message: __("Courier Issued is {0}; only PostEx orders can be fetched.", [
				frappe.utils.escape_html(frm.doc.custom_courier || __("empty")),
			]),
			indicator: "orange",
		});
		return;
	}
	if (!frm.doc.custom_postex_tracking_number) {
		frappe.msgprint({
			title: __("Not booked yet"),
			message: __(
				"This order has no PostEx tracking number yet. Create the booking first, then fetch its status."
			),
			indicator: "orange",
		});
		return;
	}

	frappe.call({
		method: "postex_integration.api.resync_order",
		args: { sales_order: frm.doc.name },
		freeze: true,
		freeze_message: __("Asking PostEx for this parcel..."),
		callback: (r) => {
			const res = r.message || {};
			if (!res.ok) {
				frappe.msgprint({
					title: __("Status not available"),
					message: frappe.utils.escape_html(res.error || __("Unknown error")),
					indicator: "red",
				});
				return;
			}

			const status = res.status || __("unknown");
			const lines = [];
			if (res.changed && res.previous_status) {
				lines.push(
					__("Status: {0} (was {1})", [status, res.previous_status])
				);
			} else {
				lines.push(__("Status: {0} (unchanged)", [status]));
			}
			if (res.postex_status && res.postex_status !== status) {
				lines.push(__("PostEx reports: {0}", [res.postex_status]));
			}
			if (res.tracking) lines.push(__("Tracking: {0}", [res.tracking]));
			if (res.delivery_fee) {
				lines.push(__("Courier fee: {0}", [format_currency(res.delivery_fee)]));
			}
			if (res.last_synced_on) {
				lines.push(__("Fetched: {0}", [res.last_synced_on]));
			}
			if (res.next_step) lines.push(res.next_step);

			frappe.msgprint({
				title: __("PostEx status"),
				message: lines.map((line) => frappe.utils.escape_html(line)).join("<br>"),
				indicator: res.changed ? "green" : "blue",
			});
			frm.reload_doc();
		},
	});
}

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
		!frm.doc.custom_postex_tracking_number &&
		!frm.doc.custom_postex_booking_doubt &&
		!frm.doc.custom_postex_booking_cancelled &&
		(courier === "" || courier === "PostEx");
	if (frm.fields_dict.custom_create_postex_booking) {
		frm.toggle_display("custom_create_postex_booking", show_button);
	}

	// "Fetch PostEx Status" complements the booking button: it only makes sense
	// once the parcel exists at PostEx (tracking number) and this is a PostEx
	// order, so no other courier is ever polled.
	const show_fetch = courier === "PostEx" && !!frm.doc.custom_postex_tracking_number;
	if (frm.fields_dict.custom_fetch_postex_status) {
		frm.toggle_display("custom_fetch_postex_status", show_fetch);
	}

	// "Cancel PostEx Booking" only while the booking can still be cancelled:
	// PostEx, booked, not yet cancelled, and nothing delivered or invoiced.
	const status = frm.doc.custom_postex_status || "";
	const show_cancel =
		courier === "PostEx" &&
		!!frm.doc.custom_postex_tracking_number &&
		!frm.doc.custom_postex_booking_cancelled &&
		!frm.doc.custom_postex_sales_invoice &&
		!frm.doc.custom_postex_delivery_note &&
		// a submitted pickup transfer means the parcel is with the courier
		!frm.doc.custom_postex_pickup_entry &&
		!["Delivered", "Returned", "Cancelled"].includes(status);
	if (frm.fields_dict.custom_cancel_postex_booking) {
		frm.toggle_display("custom_cancel_postex_booking", show_cancel);
	}
}

function bind_booking_button(frm) {
	const field = frm.fields_dict.custom_create_postex_booking;
	if (!field || !field.$input) return;
	field.$input.off("click").on("click", function () {
		if (frm.doc.custom_postex_tracking_number) return;
		if (frm.doc.custom_postex_booking_doubt) {
			frappe.msgprint(
				__(
					"The last booking attempt was unconfirmed. Check the reference at PostEx " +
						"first, then press 'Clear booking doubt'."
				)
			);
			return;
		}
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

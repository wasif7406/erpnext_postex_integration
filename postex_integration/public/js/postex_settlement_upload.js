// PostEx Settlement Upload: check the export read-only, then apply it.
//
// "Check" runs reconcile() with dry_run=1 and fills the counters + exceptions;
// "Apply" books the settlements and return costs. Both are refused once the
// document is Applied, so a file can never be booked twice from this screen.
frappe.ui.form.on("PostEx Settlement Upload", {
	refresh(frm) {
		if (frm.is_new() || frm.doc.status === "Applied") {
			return;
		}
		frm.add_custom_button(__("Check"), () => run(frm, "check_settlement_upload", false));
		frm.add_custom_button(__("Apply"), () => {
			const cutover = frm.doc.from_date
				? __("Rows before {0} are skipped.", [frm.doc.from_date])
				: __("No cutover set — every historical row is in scope.");
			frappe.confirm(
				__("Book the settlements and return costs this file proves? {0} This cannot be undone from this screen.", [cutover]),
				() => run(frm, "apply_settlement_upload", true)
			);
		}).addClass("btn-primary");
	},
});

function run(frm, method, apply) {
	frappe.call({
		method: "postex_integration.api." + method,
		args: { name: frm.doc.name },
		freeze: true,
		freeze_message: apply ? __("Booking settlements...") : __("Checking the file..."),
		callback: (r) => {
			if (r.exc || !r.message) {
				return;
			}
			frm.reload_doc();
			const res = r.message;
			frappe.msgprint({
				title: apply ? __("Applied") : __("Checked"),
				indicator: res.exceptions ? "orange" : "green",
				message: [
					`<p>${frappe.utils.escape_html(res.summary)}</p>`,
					res.exceptions
						? `<p>${__("{0} row(s) need a human — see the Exceptions table.", [res.exceptions])}</p>`
						: `<p>${__("Nothing needs attention.")}</p>`,
					apply && res.booked ? `<p>${__("{0} document(s) created.", [res.booked])}</p>` : "",
				].join(""),
			});
		},
	});
}

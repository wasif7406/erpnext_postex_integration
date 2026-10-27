// Courier Detail on Sales Invoice: PostEx fee fields only for PostEx invoices.
frappe.ui.form.on("Sales Invoice", {
	refresh(frm) {
		toggle_postex_fields(frm);
	},
	custom_courier(frm) {
		toggle_postex_fields(frm);
	},
});

function toggle_postex_fields(frm) {
	const is_postex = (frm.doc.custom_courier || "") === "PostEx";
	["custom_postex_delivery_fee", "custom_postex_delivery_tax"].forEach((fieldname) => {
		if (frm.fields_dict[fieldname]) {
			frm.toggle_display(fieldname, is_postex);
		}
	});
}

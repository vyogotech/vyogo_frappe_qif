// Copyright (c) 2026, Vyogo Technologies and contributors
// For license information, please see license.txt

frappe.ui.form.on("Bank Statement Import", {
    refresh(frm) {
        if (!frm.is_new()) {
            const file_url = frm.doc.import_file || "";
            const is_qif = file_url.toLowerCase().endsWith(".qif") || frm.doc.custom_format === "QIF";

            // If pending or file is QIF, show "Start QIF Import"
            if (frm.doc.status === "Pending" || !frm.doc.status || is_qif) {
                frm.add_custom_button(__("Start QIF Import"), () => {
                    frappe.call({
                        method: "vyogo_frappe_qif.api.import_qif",
                        args: {
                            bank_statement_import: frm.doc.name,
                        },
                        freeze: true,
                        freeze_message: __("Staging QIF statement..."),
                        callback(r) {
                            if (!r.exc && r.message) {
                                frappe.msgprint({
                                    title: __("QIF Import Started"),
                                    indicator: "green",
                                    message: __("Staged {0} records. Background worker has been queued.", [r.message.total_staged]),
                                });
                                frm.reload_doc();
                            }
                        },
                    });
                }).addClass("btn-primary");
            }

            // Undo QIF Import with Ledger Guard protection
            if (frm.doc.status === "Success" || frm.doc.status === "Partial Success") {
                frm.add_custom_button(__("Undo QIF Import"), () => {
                    frappe.confirm(
                        __("Are you sure you want to rollback this QIF import? Unreconciled Bank Transactions will be deleted."),
                        () => {
                            frappe.call({
                                method: "vyogo_frappe_qif.api.undo_import",
                                args: {
                                    import_session: frm.doc.name,
                                },
                                freeze: true,
                                freeze_message: __("Rolling back import..."),
                                callback(r) {
                                    if (!r.exc && r.message) {
                                        frappe.msgprint({
                                            title: __("Rollback Complete"),
                                            indicator: "blue",
                                            message: r.message.message,
                                        });
                                        frm.reload_doc();
                                    }
                                },
                            });
                        }
                    );
                }).addClass("btn-danger");
            }
        }
    },
});

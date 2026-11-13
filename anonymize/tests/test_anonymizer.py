from odoo.tests import common, tagged


@tagged("post_install", "-at_install")
class Anonymizer(common.TransactionCase):
    def setUp(self):
        super().setUp()
        self.Fields = self.env["ir.model.fields"]
        self.Anonymizer = self.env["frameworktools.anonymizer"]

    def _field(self, model, name):
        return self.Fields.search([("model", "=", model), ("name", "=", name)])

    def _mark(self, fields, value):
        # base fields cannot be written through the ORM (like the module does)
        self.env.cr.execute(
            "update ir_model_fields set anonymize = %s where id in %s",
            (value or None, tuple(fields.ids)),
        )
        self.Fields.invalidate_cache()

    def test_defaults_apply_although_one_field_is_marked(self):
        """A single manually marked field must not switch off all defaults."""
        self.env.cr.execute("update ir_model_fields set anonymize = null")
        self._mark(self._field("res.partner", "comment"), "clear")
        self.Fields._apply_default_anonymize_fields()
        self.Fields.invalidate_cache()
        self.assertEqual(self._field("res.partner", "email").anonymize, "email")
        self.assertEqual(self._field("res.partner", "name").anonymize, "fullname")
        # the manual choice is kept
        self.assertEqual(self._field("res.partner", "comment").anonymize, "clear")

    def test_manual_choice_is_not_overwritten(self):
        self._mark(self._field("res.partner", "email"), "clear")
        self.Fields._apply_default_anonymize_fields()
        self.Fields.invalidate_cache()
        self.assertEqual(self._field("res.partner", "email").anonymize, "clear")

    def test_all_selection_values_are_implemented(self):
        field = self._field("res.partner", "street")
        for key, _label in self.Fields._fields["anonymize"].selection:
            self._mark(field, key)
            value = field._anonymize_value("Original")
            self.assertNotEqual(value, "Original", key)

    def test_delete_mail_tracking_values(self):
        if "mail.tracking.value" not in self.env:
            self.skipTest("mail not installed")
        partner = self.env["res.partner"].create({"name": "Tracked", "email": "a@b.c"})
        message = partner.message_post(body="x")
        email = self._field("res.partner", "email")
        self.env["mail.tracking.value"].create(
            {
                "field": "email",
                "field_desc": "Email",
                "field_type": "char",
                "old_value_char": "secret@example.com",
                "mail_message_id": message.id,
            }
        )
        self._mark(email, "email")
        self.Anonymizer._delete_mail_tracking_values()
        self.assertFalse(
            self.env["mail.tracking.value"].search(
                [("mail_message_id", "=", message.id)]
            )
        )

    def test_update_writes_all_rows(self):
        partners = self.env["res.partner"].create(
            [{"name": f"Bulk {i}", "email": f"bulk{i}@example.com"} for i in range(5)]
        )
        values = [{"id": p.id, "email": f"new{p.id}@example.org"} for p in partners]
        self.Anonymizer._update_table_with_new_values(
            "res_partner", values, commit=False
        )
        partners.invalidate_cache()
        self.assertEqual(
            partners.mapped("email"), [f"new{p.id}@example.org" for p in partners]
        )

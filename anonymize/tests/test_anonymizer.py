from odoo.tests import common, tagged


@tagged("post_install", "-at_install")
class Anonymizer(common.TransactionCase):
    def setUp(self):
        super().setUp()
        self.Fields = self.env["ir.model.fields"]
        self.Anonymizer = self.env["frameworktools.anonymizer"]

    def _invalidate(self):
        if hasattr(self.env, "invalidate_all"):
            self.env.invalidate_all()
        else:
            self.Fields.invalidate_cache()

    def _field(self, model, name):
        return self.Fields.search([("model", "=", model), ("name", "=", name)])

    def _mark(self, fields, value):
        # base fields cannot be written through the ORM (like the module does)
        self.env.cr.execute(
            "update ir_model_fields set anonymize = %s where id in %s",
            (value or None, tuple(fields.ids)),
        )
        self._invalidate()

    def test_defaults_apply_although_one_field_is_marked(self):
        """A single manually marked field must not switch off all defaults."""
        self.env.cr.execute("update ir_model_fields set anonymize = null")
        self._mark(self._field("res.partner", "comment"), "clear")
        self.Fields._apply_default_anonymize_fields()
        self._invalidate()
        self.assertEqual(self._field("res.partner", "email").anonymize, "email")
        self.assertEqual(self._field("res.partner", "name").anonymize, "fullname")
        # the manual choice is kept
        self.assertEqual(self._field("res.partner", "comment").anonymize, "clear")

    def test_manual_choice_is_not_overwritten(self):
        self._mark(self._field("res.partner", "email"), "clear")
        self.Fields._apply_default_anonymize_fields()
        self._invalidate()
        self.assertEqual(self._field("res.partner", "email").anonymize, "clear")

    def test_all_selection_values_are_implemented(self):
        field = self._field("res.partner", "street")
        for key, _label in self.Fields._fields["anonymize"].selection:
            self._mark(field, key)
            value = field._anonymize_value("Original")
            self.assertNotEqual(value, "Original", key)

    def test_delete_mail_tracking_values(self):
        email = self._field("res.partner", "email")
        if "mail.tracking.value" not in self.env:
            # without the table (e.g. 20.0 without mail_tracking) nothing breaks
            self._mark(email, "email")
            self.Anonymizer._delete_mail_tracking_values()
            return
        Tracking = self.env["mail.tracking.value"]
        partner = self.env["res.partner"].create({"name": "Tracked", "email": "a@b.c"})
        message = partner.message_post(body="x")
        # the tracked field is stored as "field" up to 16.0, as "field_id" after
        key = "field_id" if "field_id" in Tracking._fields else "field"
        vals = {
            key: email.id,
            "old_value_char": "secret@example.com",
            "mail_message_id": message.id,
        }
        for name, value in [("field_desc", "Email"), ("field_type", "char")]:
            if name in Tracking._fields and not Tracking._fields[name].related:
                vals[name] = value
        Tracking.create(vals)
        self._mark(email, "email")
        self.Anonymizer._delete_mail_tracking_values()
        self.assertFalse(Tracking.search([("mail_message_id", "=", message.id)]))

    def test_all_selection_values_have_a_sql_expression(self):
        """The bulk update needs a working SQL expression for every choice."""
        partner = self.env["res.partner"].create({"name": "Bulk", "street": "Alt 1"})
        for key, _label in self.Fields._fields["anonymize"].selection:
            expr = self.Anonymizer._fake_expr(key, 5)
            self.assertTrue(expr, key)
            self.env.cr.execute(
                f"select {expr} from res_partner where id = %s", (partner.id,)
            )
            self.assertNotEqual(self.env.cr.fetchone()[0], "Alt 1", key)

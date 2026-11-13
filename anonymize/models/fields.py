import random
from odoo import api, fields, models
from odoo.exceptions import ValidationError
from .cities import city_names


class Fields(models.Model):
    _inherit = "ir.model.fields"
    _domains = [
        "hotmail.com",
        "gmail.com",
        "aol.com",
        "mail.com",
        "mail.kz",
        "yahoo.com",
    ]

    anonymize = fields.Selection(
        [
            ["clear", "Clear"],
            ["fullname", "Name"],
            ["lastname", "Last-Name"],
            ["firstname", "First-Name"],
            ["street", "Street"],
            ["phone", "Phone"],
            ["city", "City"],
            ["email", "email"],
            ["number", "number"],
        ],
        "Anonymize",
    )
    anonymize_length = fields.Integer("Anonymize Length")

    @api.model
    def gen_phone(self):
        first = str(random.randint(0, 99999)).zfill(5)
        second = str(random.randint(0, 9999999)).zfill(7)
        last = str(random.randint(0, 99)).zfill(2)
        return "{}/{}-{}".format(first, second, last)

    @api.constrains("anonymize")
    def _check_anonymize_flag(self):
        for rec in self:
            if rec.anonymize and rec.ttype not in ["char", "text"]:
                raise ValidationError("Only chars can be anonymized!")

    @api.model
    def _apply_default_anonymize_fields(self, force=False):
        """Mark the usual personal fields for anonymization.

        Fields that already have a choice keep it (force=True overwrites);
        previously a single marked field switched off all defaults, so a
        database with one manual entry was hardly anonymized.
        """
        domain = [("ttype", "in", ["char", "text"])]
        if not force:
            domain.append(("anonymize", "=", False))
        for dbfield in self.env["ir.model.fields"].search(domain):
            for x in [
                ("phone", "phone"),
                ("lastname", "lastname"),
                ("firstname", "firstname"),
                ("city", "city"),
                ("zip", "number", 5),
                ("fax", "phone"),
                ("email", "email"),
            ]:
                assert x[1] in [
                    x[0] for x in self._fields["anonymize"].selection
                ], f"{x[1]} not in selection!"
                if x[0] in dbfield.name:
                    self.env.cr.execute(
                        "update ir_model_fields set anonymize = %s where id = %s",
                        (
                            x[1],
                            dbfield.id,
                        ),
                    )
                    if len(x) > 2:
                        self.env.cr.execute(
                            "update ir_model_fields set anonymize_length = %s where id = %s",
                            (
                                x[2],
                                dbfield.id,
                            ),
                        )

        name_domain = [
            ("model", "=", "res.partner"),
            ("name", "in", ["display_name", "name"]),
        ]
        if not force:
            name_domain.append(("anonymize", "=", False))
        for dbfield in self.env["ir.model.fields"].search(name_domain):
            self.env.cr.execute(
                "update ir_model_fields set anonymize = 'fullname'  where id = %s",
                (dbfield.id,),
            )

    @api.model
    def get_one_random_domain(self, domains):
        return random.choice(domains)

    @api.model
    def generate_random_email(self):
        import names

        return (
            names.get_full_name().replace(" ", ".")
            + "@"
            + self.get_one_random_domain(self._domains)
        )

    def _anonymize_value(self, val):
        import names

        if val is None or val is False:
            return None

        if self.anonymize == "fullname":
            return names.get_full_name()
        elif self.anonymize == "lastname":
            return names.get_last_name()
        elif self.anonymize == "firstname":
            return names.get_first_name()
        elif self.anonymize == "street":
            return f"{names.get_last_name()}str. {random.randint(1, 199)}"
        elif self.anonymize == "email":
            return self.generate_random_email()
        elif self.anonymize == "phone":
            return self.gen_phone()
        elif self.anonymize == "city":
            return random.choice(city_names)
        elif self.anonymize == "number":
            length = self.anonymize_length or 5
            return str(random.randint(0, 10**length - 1)).zfill(length)
        elif self.anonymize == "clear":
            if self.ttype in ["char", "text"]:
                return ""
            elif self.ttype in ["date", "datetime"]:
                return None
            elif self.ttype in ["int", "float"]:
                return 0
            else:
                raise NotImplementedError(self.ttype)
        else:
            raise NotImplementedError(self.anonymize)

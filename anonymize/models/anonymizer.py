import os
from odoo import api, models
from odoo.tools.sql import column_exists, table_exists
import logging

logger = logging.getLogger(__name__)


def _get_max_column_width(cr, tablename, fieldname):
    sql = (
        "SELECT character_maximum_length "
        "FROM information_schema.columns "
        "WHERE table_name = %s  AND column_name = %s;"
    )
    cr.execute(
        sql,
        (
            tablename,
            fieldname,
        ),
    )
    rec = cr.fetchone()
    if not rec:
        return None
    return rec[0]


def tabletype(cr, tablename):
    sql = (
        "SELECT table_name, table_type "
        " FROM information_schema.tables "
        " WHERE table_schema = 'public' "
        " AND table_name = %s;"
    )
    cr.execute(sql, (tablename,))
    rec = cr.fetchone()
    if not rec:
        return None
    ttype = rec[1]
    return {"BASE TABLE": "table", "VIEW": "view"}[ttype]


class Anonymizer(models.AbstractModel):
    _name = "frameworktools.anonymizer"

    @api.model
    def _rename_logins(self):
        self.env.cr.execute("select id, login from res_users where id > 2;")
        for rec in self.env.cr.fetchall():
            login = f"user{rec[0]}"
            self.env.cr.execute(
                "update res_users set login = %s where id=%s", (login, rec[0])
            )

    def _delete_mail_tracking_values(self):
        cr = self.env.cr
        # the module only depends on base: without mail there is no table
        if not table_exists(cr, "mail_tracking_value"):
            return
        for field in self.env["ir.model.fields"].search([("anonymize", "!=", False)]):
            cr.execute(
                """
                delete from mail_tracking_value where field_id = %s
                and mail_message_id in (select id from mail_message where model=%s)
            """,
                (field.id, field.model),
            )

    @api.model
    def _delete_critical_tables(self):
        self.env.cr.execute("delete from mail_mail;")

    @api.model
    def _run(self, force=False):
        if force:
            if force != self.env.cr.dbname:
                raise Exception(
                    "force must match the databasename {}".format(self.env.cr.dbname)
                )
        if not force and os.environ.get("DEVMODE") != "1":
            return
        import names

        KEY = "db.anonymized"
        if (
            not force
            and self.env["ir.config_parameter"].get_param(key=KEY, default="0") == "1"
        ):
            return
        self.env["ir.model.fields"]._apply_default_anonymize_fields()
        self.env.cr.commit()

        self._rename_logins()

        self._delete_critical_tables()
        self._delete_mail_tracking_values()
        self.env.cr.commit()
        self._anonymize_field_values()
        self._anonymize_extra_gaps()
        self.env["ir.config_parameter"].set_param(KEY, "1")

    @api.model
    def _anonymize_extra_gaps(self):
        """Schliesst PII-Luecken, die die Namens-Heuristik NICHT erfasst
        (Odoo-18 bvodin): Adressen, gespeicherte/berechnete Namensfelder,
        mobile, vat, Freitext, User-Logins. Deterministische, id-basierte
        Fake-Werte per Bulk-SQL. Referenznummern (*ref) werden bewusst NICHT
        angefasst (koennen referenzielle Bedeutung tragen)."""
        cr = self.env.cr

        def upd(table, sets, where="TRUE"):
            existing = [(c, e) for c, e in sets if column_exists(cr, table, c)]
            if not table_exists(cr, table) or not existing:
                return
            clause = ", ".join(f"{c} = {e}" for c, e in existing)
            logger.info(f"Gap-Closer: {table} ({len(existing)} Spalten)")
            cr.execute(f"UPDATE {table} SET {clause} WHERE {where}")
            cr.commit()

        upd(
            "res_partner",
            [
                ("street", "'Teststrasse ' || ((id % 500) + 1)"),
                ("street2", "NULL"),
                ("street_name", "'Teststrasse'"),
                ("street_number", "((id % 200) + 1)::text"),
                ("mobile", "CASE WHEN mobile IS NOT NULL AND mobile <> '' "
                    "THEN '+49 30 ' || lpad((((id*7) % 9000000) + 1000000)::text, 7, '0') END"),
                ("complete_name", "COALESCE(NULLIF(name,''), 'Partner ' || id)"),
                ("display_name_no_ref", "COALESCE(NULLIF(name,''), 'Partner ' || id)"),
                ("commercial_company_name", "CASE WHEN commercial_company_name IS NOT NULL "
                    "AND commercial_company_name <> '' THEN 'Firma ' || id END"),
                ("search_name", "lower(COALESCE(NULLIF(name,''), 'partner' || id))"),
                ("vat", "CASE WHEN vat IS NOT NULL AND vat <> '' "
                    "THEN 'DE' || lpad((((id*13) % 1000000000))::text, 9, '0') END"),
                ("function", "CASE WHEN function IS NOT NULL AND function <> '' THEN 'Mitarbeiter' END"),
                ("website", "CASE WHEN website IS NOT NULL AND website <> '' "
                    "THEN 'https://example.com/' || id END"),
                ("comment", "NULL"),
            ],
        )
        upd("partner_inls_user", [("name", "'user' || id || '@example.com'")], "name IS NOT NULL")
        upd("sub_user", [("name", "'user' || id || '@example.com'")], "name IS NOT NULL")
        upd("partner_registration", [("user_name", "'user' || id")], "user_name IS NOT NULL")
        upd("sale_order", [("order_comment", "NULL")], "order_comment IS NOT NULL")
        upd("pre_partner", [("order_comment", "NULL")], "order_comment IS NOT NULL")
        upd("sale_subscription", [("order_comment", "NULL")], "order_comment IS NOT NULL")
        upd("res_users", [("signature", "NULL")], "signature IS NOT NULL")
        upd("mail_alias", [("alias_full_name", "NULL")], "alias_full_name IS NOT NULL")
        upd("zbs_mapper", [("comment", "NULL")], "comment IS NOT NULL")

        # Bankdaten: Kontoinhaber-Namen + IBAN/Kontonummer
        upd(
            "res_partner_bank",
            [
                ("acc_holder_name", "CASE WHEN acc_holder_name IS NOT NULL AND acc_holder_name <> '' "
                    "THEN 'Kontoinhaber ' || id END"),
                # bewusst NICHT IBAN-foermig (sonst als "IBAN" fehl-erkannt):
                ("acc_number", "'KONTO' || id"),
                ("sanitized_acc_number", "'KONTO' || id"),
            ],
        )
        # pre_partner (Import-Staging): Spalten, die die Heuristik NICHT erfasst.
        # ACHTUNG: pre_partner.name ist NICHT res.partner.name -> wird vom Modul
        # NICHT anonymisiert -> hier UNBEDINGT ersetzen (kein COALESCE-Behalten!).
        upd(
            "pre_partner",
            [
                ("name", "'Partner ' || id"),
                ("display_name", "'Partner ' || id"),
                ("commercial_company_name", "CASE WHEN commercial_company_name IS NOT NULL "
                    "AND commercial_company_name <> '' THEN 'Firma ' || id END"),
                ("parent_name", "CASE WHEN parent_name IS NOT NULL AND parent_name <> '' "
                    "THEN 'Partner ' || id END"),
                ("street2", "NULL"),
                ("street_name", "'Teststrasse'"),
                ("street_number", "((id % 200) + 1)::text"),
                ("mobile", "CASE WHEN mobile IS NOT NULL AND mobile <> '' "
                    "THEN '+49 30 ' || lpad((((id*7) % 9000000) + 1000000)::text, 7, '0') END"),
                ("vat", "CASE WHEN vat IS NOT NULL AND vat <> '' "
                    "THEN 'DE' || lpad((((id*13) % 1000000000))::text, 9, '0') END"),
                ("comment", "NULL"),
                ("order_contact_number", "NULL"),
                ("reservation_extension_reason", "NULL"),
            ],
        )
        # Transiente Wizards mit Mail-Resten leeren
        upd("account_move_send_wizard",
            [("mail_body", "NULL"), ("recipient_mails", "NULL")])

    # SQL-Fake-Ausdruecke je anonymize-Typ (set-based, nutzen die PK id).
    _FN = ("(ARRAY['Anna','Max','Lena','Paul','Mia','Tom','Eva','Jan','Lea','Ben',"
           "'Nina','Finn','Sara','Leon','Emma','Noah','Lisa','Tim','Marie','Jonas'])"
           "[(mod(id,20))+1]")
    _LN = ("(ARRAY['Mueller','Schmidt','Meyer','Fischer','Weber','Wagner','Becker',"
           "'Hoffmann','Schulz','Koch','Bauer','Richter','Klein','Wolf','Neumann',"
           "'Schwarz','Zimmermann','Braun','Krueger','Hofmann'])[(mod(id,20))+1]")
    _CITY = ("(ARRAY['Berlin','Hamburg','Muenchen','Koeln','Frankfurt','Stuttgart',"
             "'Duesseldorf','Leipzig','Dortmund','Essen'])[(mod(id,10))+1]")

    def _fake_expr(self, atype, alen):
        if atype == "fullname":
            return f"{self._FN} || ' ' || {self._LN}"
        if atype == "firstname":
            return self._FN
        if atype == "lastname":
            return self._LN
        if atype == "street":
            return f"{self._LN} || 'str. ' || (mod(id, 199) + 1)"
        if atype == "city":
            return self._CITY
        if atype == "email":
            return "'user' || id || '@example.com'"
        if atype == "phone":
            return "'+49 30 ' || lpad((mod(id*7, 9000000)+1000000)::text, 7, '0')"
        if atype == "number":
            n = alen or 5
            return f"lpad((mod(abs(id), {10 ** n}))::text, {n}, '0')"
        if atype == "clear":
            return "''"
        return None

    def _anonymize_field_values(self):
        """Set-based (Bulk-SQL) Anonymisierung: EIN UPDATE pro Tabelle statt
        per-row (Faktor 100-1000 schneller, kein OOM). Fake-Werte id-basiert;
        uebersetzbare jsonb-Felder werden gewrappt."""
        from collections import defaultdict

        cr = self.env.cr
        dbfields = self.env["ir.model.fields"].search([("anonymize", "!=", False)])
        by_table = defaultdict(list)  # table -> [(col, expr)]
        for f in dbfields:
            try:
                table = self.env[f.model]._table
            except KeyError:
                continue
            if not table_exists(cr, table) or tabletype(cr, table) == "view":
                continue
            if not column_exists(cr, table, f.name) or not column_exists(cr, table, "id"):
                continue
            e = self._fake_expr(f.anonymize, f.anonymize_length)
            if not e:
                continue
            cr.execute(
                "select data_type from information_schema.columns "
                "where table_schema='public' and table_name=%s and column_name=%s",
                (table, f.name),
            )
            r = cr.fetchone()
            if r and r[0] == "jsonb":
                e = f"jsonb_build_object('en_US', ({e}))"
            by_table[table].append((f.name, e))

        for table, sets in by_table.items():
            clause = ", ".join(f"{c} = {e}" for c, e in sets)
            logger.info(f"Bulk-anonymize {table} ({len(sets)} Spalten)")
            cr.execute(f"UPDATE {table} SET {clause}")
            cr.commit()

    def _anonymize_records(self, recs, model_dbfields, table):
        res = []
        max_column_width = {}
        logger.info(f"Generating anonymizing {len(recs)} records of {table}")
        for i, rec in enumerate(recs):
            new_rec = {"id": rec["id"]}
            if not i % 100:
                quote = round(i / len(recs) * 100, 1)
                logger.info(
                    f"Anonymizing values {table} - "
                    f"progress: {i + 1} of {len(recs)} {quote}%"
                )

            for field in model_dbfields:
                v = field._anonymize_value(rec[field.name] or "")
                if isinstance(v, str):
                    if field.name not in max_column_width:
                        max_column_width.setdefault(
                            field.name,
                            _get_max_column_width(self.env.cr, table, field.name),
                        )

                    maxdblen = max_column_width[field.name]
                    if maxdblen is not None:
                        if maxdblen < len(v):
                            v = v[:maxdblen]

                new_rec[field.name] = v

            res.append(new_rec)
        return res

    def _update_table_with_new_values(self, table, new_values):
        if not new_values:
            return

        sql_fields = list(sorted(filter(lambda x: x != "id", new_values[0].keys())))
        # Odoo 16+ speichert uebersetzbare char/text-Felder als jsonb
        # ({"en_US": "..."}). Ein roher String-UPDATE wuerde dort
        # "invalid input syntax for type json" werfen -> gezielt wrappen.
        self.env.cr.execute(
            "select column_name from information_schema.columns "
            "where table_schema='public' and table_name=%s and data_type='jsonb'",
            (table,),
        )
        jsonb_cols = {r[0] for r in self.env.cr.fetchall()}
        updates = []
        for field in sql_fields:
            if field in jsonb_cols:
                updates.append(f" {field} = jsonb_build_object('en_US', %s::text)")
            else:
                updates.append(f" {field} = %s")
        sql_updates = ",".join(updates)
        del updates

        for i, rec in enumerate(new_values):
            sql_values = [rec[x] for x in sql_fields]
            self.env.cr.execute(
                f"update {table} set {sql_updates} where id = %s",
                tuple(sql_values + [rec["id"]]),
            )
            if not i % 100:
                quote = round(i / len(new_values) * 100, 1)
                logger.info(
                    f"{table} Writing to database done {i} of "
                    f"{len(new_values)}: {quote:.1f}%"
                )
                self.env.cr.commit()

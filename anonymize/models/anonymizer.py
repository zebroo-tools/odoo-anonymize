import os
from odoo import _, api, fields, models, SUPERUSER_ID
from odoo.tools.sql import column_exists, table_exists
from odoo.exceptions import UserError, RedirectWarning, ValidationError
import random
import logging

from psycopg2.extras import execute_values

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
    return cr.fetchone()[0]


BATCH_SIZE = 1000


def _get_column_type(cr, tablename, fieldname):
    cr.execute(
        "SELECT format_type(a.atttypid, a.atttypmod) FROM pg_attribute a "
        "WHERE a.attrelid = %s::regclass AND a.attname = %s",
        (tablename, fieldname),
    )
    return cr.fetchone()[0]


def tabletype(cr, tablename):
    sql = (
        "SELECT table_name, table_type "
        " FROM information_schema.tables "
        " WHERE table_schema = 'public' "
        f" AND table_name = '{tablename}';"
    )
    cr.execute(sql)
    rec = cr.fetchone()
    if not rec:
        return None
    ttype = rec[1]
    return {"BASE TABLE": "table", "VIEW": "view"}[ttype]


class Anonymizer(models.AbstractModel):
    _name = "frameworktools.anonymizer"
    _domains = [
        "hotmail.com",
        "gmail.com",
        "aol.com",
        "mail.com",
        "mail.kz",
        "yahoo.com",
    ]

    @api.model
    def _rename_logins(self):
        self.env.cr.execute("select id, login from res_users where id > 2;")
        for rec in self.env.cr.fetchall():
            login = f"user{rec[0]}"
            self.env.cr.execute(
                "update res_users set login = %s where id=%s", (login, rec[0])
            )

    @api.model
    def gen_phone(self):
        first = str(random.randint(00000, 99999))
        second = str(random.randint(10000, 99999)).zfill(7)

        last = str(random.randint(1, 99)).zfill(2)
        while last in ["1111", "2222", "3333", "4444", "5555", "6666", "7777", "8888"]:
            last = str(random.randint(1, 9998)).zfill(4)

        return "{}/{}-{}".format(first, second, last)

    @api.model
    def get_one_random_domain(self, domains):
        return random.choice(domains)

    @api.model
    def generate_random_email(self):
        from . import fast_names as names

        return (
            names.get_full_name().replace(" ", ".")
            + "@"
            + self.get_one_random_domain(self._domains)
        )

    def _delete_mail_tracking_values(self):
        cr = self.env.cr
        if not table_exists(cr, "mail_tracking_value"):
            return
        # up to 13.0 the tracked field is stored by name (column "field"),
        # from 14.0 on by id (column "field_id")
        by_id = column_exists(cr, "mail_tracking_value", "field_id")
        for field in self.env["ir.model.fields"].search([("anonymize", "!=", False)]):
            cr.execute(
                f"""
                delete from mail_tracking_value
                where {"field_id" if by_id else "field"} = %s
                and mail_message_id in (select id from mail_message where model=%s)
            """,
                (field.id if by_id else field.name, field.model),
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
        if not force and os.environ["DEVMODE"] != "1":
            return
        import names  # noqa: F401 - fail early if the library is missing

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
        self.env["ir.config_parameter"].set_param(KEY, "1")

    def _anonymize_field_values(self):
        dbfields = self.env["ir.model.fields"].search(
            [("anonymize", "!=", False)], order="model"
        )
        models = list(sorted(set(dbfields.mapped("model"))))
        for model in models:
            logger.info(f"Anonymizing model {model}")

            try:
                obj = self.env[model]
            except KeyError:
                continue
            table = obj._table
            cr = self.env.cr
            if not table_exists(cr, table):
                continue

            # check if table is a view
            if tabletype(self.env.cr, table) == "view":
                continue
            model_dbfields = dbfields.filtered(lambda x: x.model == model)
            effective_fields = model_dbfields.browse()

            for field in model_dbfields:
                if not column_exists(cr, table, field.name):
                    logger.info(f"Ignoring not existent column: {table}:{field.name}")
                else:
                    effective_fields |= field

            if not effective_fields:
                continue
            sql_fields = ",".join(sorted(effective_fields.mapped("name")))
            cr.execute(f"select id, {sql_fields} from {table} order by id desc")
            recs = cr.dictfetchall()
            new_values = self._anonymize_records(recs, effective_fields, table)
            self._update_table_with_new_values(table, new_values)

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

    def _update_table_with_new_values(self, table, new_values, commit=True):
        if not new_values:
            return

        sql_fields = list(sorted(filter(lambda x: x != "id", new_values[0].keys())))
        # one statement per batch instead of one per row (1.7 mio. mail
        # messages took hours); the values keep the column types of the table
        sql_updates = ",".join(f'"{field}" = v."{field}"' for field in sql_fields)
        columns = ",".join(f'"{field}"' for field in ["id"] + sql_fields)
        casts = ",".join(
            f'%s::{_get_column_type(self.env.cr, table, field)}'
            for field in ["id"] + sql_fields
        )
        for start in range(0, len(new_values), BATCH_SIZE):
            batch = new_values[start : start + BATCH_SIZE]
            execute_values(
                self.env.cr._obj,
                f'update "{table}" t set {sql_updates} '
                f"from (values %s) as v({columns}) where t.id = v.id",
                [tuple(rec[x] for x in ["id"] + sql_fields) for rec in batch],
                template=f"({casts})",
                page_size=BATCH_SIZE,
            )
            done = start + len(batch)
            logger.info(
                f"{table} Writing to database done {done} of "
                f"{len(new_values)}: {done / len(new_values) * 100:.1f}%"
            )
            if commit:
                self.env.cr.commit()
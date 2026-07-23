# Copyright 2026 Fermenteria Smakelig
# License LGPL-3.0 or later (http://www.gnu.org/licenses/lgpl).
import base64
import csv
import hashlib
import io
import logging

from odoo import fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class ContactImportCsvWizard(models.TransientModel):
    """Simple v1 column mapping: the person types the exact header names
    used in their CSV for name/email/phone, rather than a fully dynamic
    column-picker UI (which would need an onchange reading the uploaded
    file to populate live dropdowns - real complexity for a first cut).
    Defaults guess common header names; the person can override them.

    Supports two shapes for the name: a single combined column (e.g.
    plain "name"), or split first/last columns (e.g. Google Contacts'
    own CSV export, which uses "First Name"/"Last Name" rather than one
    "name" column) - at least one of the two must resolve to real
    columns in the uploaded file."""
    _name = "contact.import.csv.wizard"
    _description = "Import contacts from a CSV file for review"

    csv_file = fields.Binary(required=True, string="CSV File")
    csv_filename = fields.Char()
    batch_name = fields.Char(required=True, default=lambda self: "CSV import %s" % fields.Date.today())

    name_column = fields.Char(
        help="Single combined name column, if your file has one. Leave "
             "blank and use First/Last Name Column instead for exports "
             "like Google Contacts' CSV, which splits the name.",
    )
    first_name_column = fields.Char(
        default="First Name",
        help="e.g. 'First Name' - Google Contacts CSV export uses this.",
    )
    last_name_column = fields.Char(
        default="Last Name",
        help="e.g. 'Last Name' - Google Contacts CSV export uses this.",
    )
    email_column = fields.Char(
        default="E-mail 1 - Value",
        help="e.g. 'E-mail 1 - Value' for Google Contacts CSV export.",
    )
    phone_column = fields.Char(
        default="Phone 1 - Value",
        help="e.g. 'Phone 1 - Value' for Google Contacts CSV export.",
    )
    job_title_column = fields.Char(
        default="Organization Title",
        help="Maps to Job Title (res.partner.function). e.g. 'Organization Title'.",
    )
    labels_column = fields.Char(
        default="Labels",
        help="Maps to Tags (res.partner.category_id). e.g. Google's 'Labels' column.",
    )
    notes_column = fields.Char(
        default="Notes",
        help="Maps to Notes (res.partner.comment). e.g. Google's 'Notes' column.",
    )
    street_column = fields.Char(
        default="Address 1 - Street",
        help="Maps to Street (res.partner.street). e.g. Google's 'Address 1 - Street'.",
    )
    city_column = fields.Char(
        default="Address 1 - City",
        help="Maps to City (res.partner.city). e.g. Google's 'Address 1 - City'.",
    )
    zip_column = fields.Char(
        string="Postal Code column",
        default="Address 1 - Postal Code",
        help="Maps to ZIP (res.partner.zip). e.g. Google's 'Address 1 - Postal Code'.",
    )
    country_column = fields.Char(
        default="Address 1 - Country",
        help="Maps to Country (res.partner.country_id), resolved by name/code "
             "lookup. e.g. Google's 'Address 1 - Country'.",
    )
    extra_notes_columns = fields.Char(
        string="Extra columns to append to Notes",
        default="Middle Name, Nickname, Birthday, Organization Department, Organization Name",
        help="Comma-separated list of any other column names you want kept "
             "even though res.partner has no dedicated field for them. Each "
             "is added as a labeled line in Notes rather than being "
             "silently dropped. Defaults cover Google's common extras; add "
             "'E-mail 2 - Value, Phone 2 - Value' etc. if your export has "
             "multiple emails/phones per contact.",
    )

    def _get_contacts_profile(self):
        profile = self.env.ref("contact_import.import_match_profile_contacts", raise_if_not_found=False)
        if not profile:
            raise UserError("Contacts matching profile not found - module data may not have loaded correctly.")
        return profile

    def _resolve_name(self, row, fieldnames):
        """Returns the contact's display name from either the single
        name_column or by joining first_name_column + last_name_column,
        whichever is actually configured and present in this file."""
        if self.name_column and self.name_column in fieldnames:
            return (row.get(self.name_column) or "").strip()
        parts = []
        if self.first_name_column and self.first_name_column in fieldnames:
            parts.append((row.get(self.first_name_column) or "").strip())
        if self.last_name_column and self.last_name_column in fieldnames:
            parts.append((row.get(self.last_name_column) or "").strip())
        return " ".join(p for p in parts if p)

    def _build_notes(self, row, fieldnames):
        """Combines notes_column plus any extra_notes_columns into a single
        text block, each as a labeled line - e.g. 'Birthday: 1990-05-12'.
        Nothing here is dropped just because res.partner has no dedicated
        field for it."""
        lines = []
        if self.notes_column and self.notes_column in fieldnames:
            value = (row.get(self.notes_column) or "").strip()
            if value:
                lines.append(value)
        if self.extra_notes_columns:
            extra_cols = [c.strip() for c in self.extra_notes_columns.split(",") if c.strip()]
            for col in extra_cols:
                if col in fieldnames:
                    value = (row.get(col) or "").strip()
                    if value:
                        lines.append("%s: %s" % (col, value))
        return "\n".join(lines)

    def action_import(self):
        self.ensure_one()
        if not self.csv_file:
            raise UserError("Please choose a CSV file first.")

        raw_bytes = base64.b64decode(self.csv_file)
        try:
            text = raw_bytes.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = raw_bytes.decode("latin-1")

        reader = csv.DictReader(io.StringIO(text))
        if reader.fieldnames is None:
            raise UserError("Could not read any columns from this file - is it a valid CSV?")

        name_col_ok = self.name_column and self.name_column in reader.fieldnames
        first_last_ok = (
            (self.first_name_column and self.first_name_column in reader.fieldnames)
            or (self.last_name_column and self.last_name_column in reader.fieldnames)
        )
        if not name_col_ok and not first_last_ok:
            raise UserError(
                "Could not find a usable name column.\n"
                "Looked for Name column '%s', or First/Last Name columns "
                "'%s' / '%s'.\nColumns found in file: %s"
                % (self.name_column or "(blank)", self.first_name_column or "(blank)",
                   self.last_name_column or "(blank)", ", ".join(reader.fieldnames))
            )

        profile = self._get_contacts_profile()
        batch = self.env["import.batch"].create({
            "name": self.batch_name,
            "source_type": "csv",
            "profile_id": profile.id,
        })

        created = 0
        skipped = 0
        for row in reader:
            name = self._resolve_name(row, reader.fieldnames)
            if not name:
                skipped += 1
                continue
            email = (row.get(self.email_column) or "").strip() if self.email_column else ""
            phone = (row.get(self.phone_column) or "").strip() if self.phone_column else ""
            job_title = (row.get(self.job_title_column) or "").strip() if self.job_title_column else ""
            tag_names = (row.get(self.labels_column) or "").strip() if self.labels_column else ""
            street = (row.get(self.street_column) or "").strip() if self.street_column else ""
            city = (row.get(self.city_column) or "").strip() if self.city_column else ""
            zip_code = (row.get(self.zip_column) or "").strip() if self.zip_column else ""
            country_name = (row.get(self.country_column) or "").strip() if self.country_column else ""
            notes = self._build_notes(row, reader.fieldnames)

            # Stable ref so re-importing the same file doesn't create
            # duplicate staging rows for rows already seen before.
            source_ref = hashlib.sha256(
                ("%s|%s|%s" % (name, email, phone)).encode("utf-8")
            ).hexdigest()

            existing = self.env["import.staging.record"].search([
                ("profile_id", "=", profile.id),
                ("source_ref", "=", source_ref),
            ], limit=1)
            if existing:
                skipped += 1
                continue

            staging = self.env["import.staging.record"].create({
                "profile_id": profile.id,
                "batch_id": batch.id,
                "source_ref": source_ref,
                "display_name": name,
                "email": email or False,
                "phone": phone or False,
                "function": job_title or False,
                "tag_names": tag_names or False,
                "street": street or False,
                "city": city or False,
                "zip_code": zip_code or False,
                "country_name": country_name or False,
                "notes": notes or False,
                "raw_data": str(row),
            })
            staging.action_compute_candidates()
            created += 1

        _logger.info(
            "contact_import: CSV batch '%s' - %d staged, %d skipped (empty name or duplicate)",
            batch.name, created, skipped,
        )

        return {
            "type": "ir.actions.act_window",
            "name": "Import Batch",
            "res_model": "import.batch",
            "res_id": batch.id,
            "views": [(False, "form")],
            "target": "current",
        }

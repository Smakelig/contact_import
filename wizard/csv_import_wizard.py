# Copyright 2026 Fermenteria Smakelig
# License LGPL-3.0 or later (http://www.gnu.org/licenses/lgpl).
import base64
import csv
import io
import json
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
    street2_column = fields.Char(
        string="Street 2 column",
        default="Address 1 - Extended Address",
        help="Maps to Street 2 (res.partner.street2), a second address line. "
             "Best-guess default for Google's export naming - not verified "
             "against a live export, check against your actual file.",
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
    state_column = fields.Char(
        string="State/Province column",
        default="Address 1 - Region",
        help="Maps to State/Province (res.partner.state_id), resolved to a "
             "real record scoped to the resolved country. Best-guess "
             "default for Google's export naming - not verified against a "
             "live export, check against your actual file.",
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

    # (attribute, human label) for every optional CSV column mapping -
    # used to warn when a configured column doesn't actually exist in
    # the uploaded file, so a typo (or an unmodified Google-export
    # default against a non-Google file) doesn't silently leave a field
    # blank for every single row with no indication why. name/first/last
    # name columns aren't in this list - those are validated separately
    # up front and block the import outright if none resolve, since
    # without a name there's nothing to stage at all.
    _OPTIONAL_COLUMN_FIELDS = [
        ("email_column", "Email"),
        ("phone_column", "Phone"),
        ("job_title_column", "Job Title"),
        ("labels_column", "Tags/Labels"),
        ("notes_column", "Notes"),
        ("street_column", "Street"),
        ("street2_column", "Street 2"),
        ("city_column", "City"),
        ("zip_column", "Postal Code"),
        ("state_column", "State/Province"),
        ("country_column", "Country"),
    ]

    def _missing_column_warning(self, fieldnames):
        """Returns a one-line warning (or None) listing every configured
        optional column that isn't actually present in the uploaded
        file's headers - each of those fields will be blank on every
        staged row. extra_notes_columns is deliberately excluded: it's
        meant as an over-inclusive list of possible extras, so most
        entries not matching a given file is the expected case, not a
        typo."""
        missing = []
        for attr, label in self._OPTIONAL_COLUMN_FIELDS:
            column = getattr(self, attr)
            if column and column not in fieldnames:
                missing.append("%s ('%s')" % (label, column))
        if not missing:
            return None
        return (
            "Note: these configured columns were not found in the uploaded "
            "file and were left blank for every row: %s" % ", ".join(missing)
        )

    def _row_values(self, row, fieldnames):
        """Pulls every mapped column out of one CSV row into a plain
        dict of staging-field values. Raised out of the main loop's
        savepoint like everything else per-row, so a row with a value
        that can't be processed (e.g. an encoding artefact only this
        specific row triggers) is skipped cleanly rather than aborting
        every row after it."""
        return {
            "name": self._resolve_name(row, fieldnames),
            "email": (row.get(self.email_column) or "").strip() if self.email_column else "",
            "phone": (row.get(self.phone_column) or "").strip() if self.phone_column else "",
            "job_title": (row.get(self.job_title_column) or "").strip() if self.job_title_column else "",
            "tag_names": (row.get(self.labels_column) or "").strip() if self.labels_column else "",
            "street": (row.get(self.street_column) or "").strip() if self.street_column else "",
            "street2": (row.get(self.street2_column) or "").strip() if self.street2_column else "",
            "city": (row.get(self.city_column) or "").strip() if self.city_column else "",
            "zip_code": (row.get(self.zip_column) or "").strip() if self.zip_column else "",
            "state_name": (row.get(self.state_column) or "").strip() if self.state_column else "",
            "country_name": (row.get(self.country_column) or "").strip() if self.country_column else "",
            "notes": self._build_notes(row, fieldnames),
        }

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
            "processing_state": "queued",
        })

        # This part - decode + resolve column values per row - is pure
        # Python, no DB round-trips, so it stays fast even for a very
        # large file. What used to be slow here (a DB search for
        # dedup + record creation + matching per row, all inside one
        # HTTP request) is deferred to import.batch's background
        # processor instead - see models/import_batch.py. That's the
        # actual point of this rewrite: queuing thousands of rows this
        # way takes a couple of seconds; processing them can now take
        # as long as it needs to, in the background, without tying up
        # a web worker or risking a request timeout.
        log_notes = []
        missing_column_note = self._missing_column_warning(reader.fieldnames)
        if missing_column_note:
            log_notes.append(missing_column_note)

        pending_rows = []
        row_iter = enumerate(reader, start=2)
        last_row_seen = 1
        while True:
            try:
                row_number, row = next(row_iter)
            except StopIteration:
                break
            except csv.Error as exc:
                # The parser itself choked partway through the file
                # (bad quoting, truncated file, etc). Keep everything
                # read so far rather than losing it, and tell the
                # person the file was only partially read.
                _logger.exception(
                    "contact_import: batch '%s' CSV parser stopped around row %d",
                    batch.name, last_row_seen + 1,
                )
                log_notes.append(
                    "CSV parsing stopped early around row %d (%s) - the file may "
                    "be truncated or have a malformed quoted field. Only the rows "
                    "before this point were queued." % (last_row_seen + 1, exc)
                )
                break
            last_row_seen = row_number
            vals = self._row_values(row, reader.fieldnames)
            pending_rows.append({"row_number": row_number, "vals": vals, "raw": row})

        batch.write({
            "pending_rows_json": json.dumps(pending_rows) if pending_rows else False,
            "pending_count": len(pending_rows),
            "import_log": "\n".join(log_notes) if log_notes else False,
        })

        # Kick off background processing right away rather than waiting
        # for this cron's own scheduled interval - see
        # data/ir_cron_data.xml for why a scheduled fallback interval
        # still exists alongside this.
        cron = self.env.ref("contact_import.ir_cron_process_import_batches", raise_if_not_found=False)
        if cron:
            cron._trigger()

        _logger.info(
            "contact_import: CSV batch '%s' - %d row(s) queued for background processing",
            batch.name, len(pending_rows),
        )

        return {
            "type": "ir.actions.act_window",
            "name": "Import Batch",
            "res_model": "import.batch",
            "res_id": batch.id,
            "views": [(False, "form")],
            "target": "current",
        }

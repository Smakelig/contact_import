# Copyright 2026 Fermenteria Smakelig
# License LGPL-3.0 or later (http://www.gnu.org/licenses/lgpl).
import base64
import csv
import io
from unittest.mock import patch

from odoo.tests.common import TransactionCase, tagged


def _make_csv(rows, fieldnames=("Name", "E-mail 1 - Value", "Phone 1 - Value")):
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=fieldnames)
    writer.writeheader()
    writer.writerows(rows)
    return base64.b64encode(buf.getvalue().encode("utf-8"))


@tagged("post_install", "-at_install")
class TestWizardImport(TransactionCase):
    """Covers the queue-then-background-process pipeline: the wizard's
    action_import() only decodes/resolves/queues (no DB writes beyond
    the batch + pending_rows_json itself), and import.batch's
    _process_pending_chunk does the actual per-row creation/dedup/
    matching with per-row failure isolation.

    Row counts in these tests are deliberately kept well below
    import.batch._CHUNK_SIZE (200), so _process_pending_chunk's chunk
    loop runs exactly once and therefore commits exactly once - these
    tests are intentionally exercising code that commits mid-method
    (needed for real crash-durability), which is expected here, not an
    oversight.
    """

    def _run_wizard(self, rows, **wizard_vals):
        wizard = self.env["contact.import.csv.wizard"].create({
            "csv_file": _make_csv(rows),
            "csv_filename": "test.csv",
            "batch_name": "Test import",
            "name_column": "Name",
            "first_name_column": "",
            "last_name_column": "",
            "email_column": "E-mail 1 - Value",
            "phone_column": "Phone 1 - Value",
            **wizard_vals,
        })
        action = wizard.action_import()
        batch = self.env["import.batch"].browse(action["res_id"])
        # Process synchronously in the test rather than relying on the
        # real cron/trigger machinery (which needs a live job runner) -
        # calling the same method the cron calls is exactly what a real
        # cron tick does.
        batch._process_pending_chunk(row_budget=10_000)
        return batch

    def test_queuing_does_not_create_staging_records_yet(self):
        wizard = self.env["contact.import.csv.wizard"].create({
            "csv_file": _make_csv([{"Name": "Ana Garcia", "E-mail 1 - Value": "ana@example.com", "Phone 1 - Value": ""}]),
            "csv_filename": "test.csv",
            "batch_name": "Queue-only test",
            "name_column": "Name",
            "first_name_column": "", "last_name_column": "",
            "email_column": "E-mail 1 - Value", "phone_column": "Phone 1 - Value",
        })
        action = wizard.action_import()
        batch = self.env["import.batch"].browse(action["res_id"])
        self.assertEqual(batch.processing_state, "queued")
        self.assertEqual(batch.pending_count, 1)
        self.assertFalse(batch.staging_ids)

    def test_processing_creates_staging_records_and_marks_done(self):
        batch = self._run_wizard([
            {"Name": "Ana Garcia", "E-mail 1 - Value": "ana@example.com", "Phone 1 - Value": ""},
            {"Name": "Bruno Costa", "E-mail 1 - Value": "bruno@example.com", "Phone 1 - Value": ""},
        ])
        self.assertEqual(batch.processing_state, "done")
        self.assertEqual(batch.pending_count, 0)
        self.assertEqual(batch.staging_count, 2)
        names = batch.staging_ids.mapped("display_name")
        self.assertIn("Ana Garcia", names)
        self.assertIn("Bruno Costa", names)

    def test_empty_name_rows_are_skipped_not_staged(self):
        batch = self._run_wizard([
            {"Name": "", "E-mail 1 - Value": "noname@example.com", "Phone 1 - Value": ""},
            {"Name": "Real Person", "E-mail 1 - Value": "", "Phone 1 - Value": ""},
        ])
        self.assertEqual(batch.staging_count, 1)
        self.assertEqual(batch.skipped_count, 1)

    def test_cross_batch_duplicate_is_skipped(self):
        first = self._run_wizard([
            {"Name": "Repeat Person", "E-mail 1 - Value": "repeat@example.com", "Phone 1 - Value": ""},
        ])
        self.assertEqual(first.staging_count, 1)

        second = self._run_wizard([
            {"Name": "Repeat Person", "E-mail 1 - Value": "repeat@example.com", "Phone 1 - Value": ""},
        ])
        # Same name+email+phone as a row from an EARLIER batch -> skipped.
        self.assertEqual(second.staging_count, 0)
        self.assertEqual(second.skipped_count, 1)

    def test_same_batch_collision_is_not_deduped(self):
        # Two different people, same name, no email/phone to
        # disambiguate - hash identically within ONE file. Must NOT be
        # deduped against each other (see _create_from_import_row's own
        # docstring reasoning) - both should be staged for a human to
        # sort out, rather than the second silently vanishing.
        batch = self._run_wizard([
            {"Name": "Maria Garcia", "E-mail 1 - Value": "", "Phone 1 - Value": ""},
            {"Name": "Maria Garcia", "E-mail 1 - Value": "", "Phone 1 - Value": ""},
        ])
        self.assertEqual(batch.staging_count, 2)
        self.assertEqual(batch.skipped_count, 0)

    def test_missing_configured_column_logged_as_a_note(self):
        wizard = self.env["contact.import.csv.wizard"].create({
            "csv_file": _make_csv([{"Name": "Someone", "E-mail 1 - Value": "x@example.com", "Phone 1 - Value": ""}]),
            "csv_filename": "test.csv",
            "batch_name": "Missing column test",
            "name_column": "Name",
            "first_name_column": "", "last_name_column": "",
            "email_column": "E-mail 1 - Value",
            "phone_column": "Phone 1 - Value",
            "labels_column": "This Column Does Not Exist",
        })
        action = wizard.action_import()
        batch = self.env["import.batch"].browse(action["res_id"])
        self.assertIn("This Column Does Not Exist", batch.import_log or "")
        self.assertEqual(batch.error_count, 0, "a missing-column note is informational, not an error")

    def test_one_bad_row_does_not_abort_the_whole_batch(self):
        # Force _create_from_import_row to blow up on exactly one row,
        # to confirm the per-row savepoint isolates it - the good rows
        # on either side must still land.
        Staging = type(self.env["import.staging.record"])
        original = Staging._create_from_import_row
        call_count = {"n": 0}

        def flaky(self, profile, batch, vals, raw_row):
            call_count["n"] += 1
            if vals.get("name") == "Boom Person":
                raise ValueError("simulated failure")
            return original(self, profile, batch, vals, raw_row)

        rows = [
            {"Name": "Before Person", "E-mail 1 - Value": "before@example.com", "Phone 1 - Value": ""},
            {"Name": "Boom Person", "E-mail 1 - Value": "boom@example.com", "Phone 1 - Value": ""},
            {"Name": "After Person", "E-mail 1 - Value": "after@example.com", "Phone 1 - Value": ""},
        ]
        with patch.object(Staging, "_create_from_import_row", flaky):
            batch = self._run_wizard(rows)

        self.assertEqual(batch.staging_count, 2)
        self.assertEqual(batch.error_count, 1)
        names = batch.staging_ids.mapped("display_name")
        self.assertIn("Before Person", names)
        self.assertIn("After Person", names)
        self.assertNotIn("Boom Person", names)
        self.assertIn("Boom Person", batch.import_log or "")

    def test_name_and_first_last_name_are_mutually_supported(self):
        wizard = self.env["contact.import.csv.wizard"].create({
            "csv_file": _make_csv(
                [{"First Name": "Jane", "Last Name": "Doe"}],
                fieldnames=("First Name", "Last Name"),
            ),
            "csv_filename": "test.csv",
            "batch_name": "First/last name test",
            "name_column": "",
            "first_name_column": "First Name",
            "last_name_column": "Last Name",
            "email_column": "", "phone_column": "",
        })
        action = wizard.action_import()
        batch = self.env["import.batch"].browse(action["res_id"])
        batch._process_pending_chunk(row_budget=10_000)
        self.assertEqual(batch.staging_ids.display_name, "Jane Doe")

    def test_no_usable_name_column_raises_before_queuing_anything(self):
        from odoo.exceptions import UserError

        wizard = self.env["contact.import.csv.wizard"].create({
            "csv_file": _make_csv([{"Name": "Whoever"}], fieldnames=("Name",)),
            "csv_filename": "test.csv",
            "batch_name": "No usable name column",
            "name_column": "Wrong Column Name",
            "first_name_column": "Also Wrong",
            "last_name_column": "Still Wrong",
            "email_column": "", "phone_column": "",
        })
        with self.assertRaises(UserError):
            wizard.action_import()

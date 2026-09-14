# Copyright 2026 Fermenteria Smakelig
# License LGPL-3.0 or later (http://www.gnu.org/licenses/lgpl).
import json
import logging

from odoo import api, fields, models

_logger = logging.getLogger(__name__)


class ImportBatch(models.Model):
    """One row per import run - e.g. 'contacts.csv uploaded 2026-07-22'.
    Lets you see import runs at a glance rather than a flat, endless
    list of every staged record ever seen.

    Also owns background processing of large imports: a source wizard
    (e.g. contact.import.csv.wizard) does the fast, DB-free part itself
    - decoding the file and resolving column values into a plain dict
    per row - then hands the whole list to this model via
    pending_rows_json and returns immediately. The actual per-row DB
    work (dedup check, staging record creation, candidate matching -
    everything that was previously a slow, synchronous, one-request
    loop) happens here instead, driven by ir_cron_process_import_batches
    (see data/ir_cron_data.xml), in bounded chunks with a commit after
    each so a worker restart mid-run loses at most one chunk.
    """
    _name = "import.batch"
    _description = "One import run (a CSV upload, a Google sync, etc.)"
    _order = "create_date desc"

    # Rows fully processed (created/deduped + matched) per DB commit -
    # same durability reasoning the old synchronous wizard loop used:
    # a crash/restart mid-run loses at most this many rows' worth of
    # progress, not the whole batch.
    _CHUNK_SIZE = 200

    # Total rows ONE cron invocation will process, spread across
    # however many batches have pending work, oldest first. Bounds how
    # long a single invocation can run so one huge file can't
    # monopolize a cron worker and starve the OTHER real cron jobs on
    # this instance (Meta token refresh, OCR bill-scan, etc. - see
    # CLAUDE.md's infra notes, which is exactly why max_cron_threads
    # was raised to 2 in the first place). If work remains once this
    # budget is spent, the run re-triggers itself so processing
    # continues almost immediately rather than waiting a full interval.
    _MAX_ROWS_PER_CRON_CALL = 2000

    # Cap how many error lines accumulate in import_log across however
    # many chunks/cron calls a batch takes to finish - same reasoning
    # as before: a catastrophically bad file shouldn't be able to grow
    # this Text field without bound. Full detail always goes to
    # /var/log/odoo17/odoo.log via _logger.exception regardless.
    _MAX_LOGGED_ERRORS = 200

    name = fields.Char(required=True)
    source_type = fields.Selection([
        ("csv", "CSV Upload"),
        ("google", "Google Contacts"),
        ("vcard", "vCard File"),
    ], required=True)
    profile_id = fields.Many2one("import.match.profile", required=True)
    create_date = fields.Datetime(readonly=True)
    staging_ids = fields.One2many("import.staging.record", "batch_id", string="Staged Records")
    staging_count = fields.Integer(compute="_compute_counts")
    new_count = fields.Integer(compute="_compute_counts")
    linked_count = fields.Integer(compute="_compute_counts")
    created_count = fields.Integer(compute="_compute_counts")
    ignored_count = fields.Integer(compute="_compute_counts")

    processing_state = fields.Selection([
        ("queued", "Queued"),
        ("processing", "Processing"),
        ("done", "Done"),
    ], default="done", required=True, index=True, readonly=True,
        help="'done' is the default for any batch that isn't going through "
             "background processing (nothing was ever queued on it). A CSV "
             "import wizard run sets this to 'queued' immediately, then "
             "the background processor moves it to 'processing' once it "
             "starts consuming pending_rows_json, and to 'done' once "
             "nothing is left to process. System-managed only (readonly): "
             "a stray manual edit to 'done' while pending_rows_json still "
             "has rows would make the background processor think there's "
             "nothing left to do and stop resuming this batch.",
    )
    pending_rows_json = fields.Text(
        readonly=True,
        help="JSON list of not-yet-processed rows (each a dict with "
             "row_number/vals/raw), consumed by the background processor "
             "in chunks and rewritten with whatever's left after each "
             "chunk. Cleared (False) once empty. System-managed only.",
    )
    pending_count = fields.Integer(
        default=0, readonly=True,
        help="How many rows are still waiting to be processed in the "
             "background. Kept as a plain stored counter rather than "
             "computed by parsing pending_rows_json on every read, since "
             "that field can hold thousands of rows' worth of JSON.",
    )
    skipped_count = fields.Integer(
        default=0, readonly=True,
        help="Rows skipped for an expected reason (empty name, or an "
             "already-seen duplicate from an EARLIER batch - see "
             "import.staging.record._create_from_import_row). Tracked "
             "separately from created_count since a skipped row never "
             "becomes a staging record at all.",
    )

    error_count = fields.Integer(
        default=0, readonly=True,
        help="Rows from the source file that failed to import entirely - "
             "not counting rows skipped for expected reasons (empty name, "
             "already-seen duplicate). See Import Log for detail on each.",
    )
    import_log = fields.Text(
        readonly=True,
        help="Row-level errors/warnings from the run that created this "
             "batch. A row-creation failure means that row was skipped "
             "entirely (rolled back, nothing staged). A matching failure "
             "means the row WAS staged but its candidate matches weren't "
             "computed - use Recompute Matches on that row once the "
             "underlying issue is fixed.",
    )

    def _compute_counts(self):
        for batch in self:
            recs = batch.staging_ids
            batch.staging_count = len(recs)
            batch.new_count = len(recs.filtered(lambda r: r.state == "new"))
            batch.linked_count = len(recs.filtered(lambda r: r.state == "linked"))
            batch.created_count = len(recs.filtered(lambda r: r.state == "created"))
            batch.ignored_count = len(recs.filtered(lambda r: r.state == "ignored"))

    @api.model
    def _cron_process_pending_imports(self):
        """Entry point for ir_cron_process_import_batches. Processes up
        to _MAX_ROWS_PER_CRON_CALL rows total, spread across whichever
        batches still have pending work, oldest batch first. If work is
        still left when the budget runs out, re-triggers itself so
        processing continues almost immediately rather than waiting for
        this cron's own scheduled interval."""
        rows_done = 0
        batches = self.search(
            [("processing_state", "in", ("queued", "processing"))],
            order="create_date asc",
        )
        for batch in batches:
            if rows_done >= self._MAX_ROWS_PER_CRON_CALL:
                break
            rows_done += batch._process_pending_chunk(self._MAX_ROWS_PER_CRON_CALL - rows_done)

        remaining = self.search_count([("processing_state", "in", ("queued", "processing"))])
        if remaining:
            cron = self.env.ref("contact_import.ir_cron_process_import_batches", raise_if_not_found=False)
            if cron:
                cron._trigger()

    def _process_pending_chunk(self, row_budget):
        """Processes up to row_budget rows of THIS batch's
        pending_rows_json, in sub-chunks of _CHUNK_SIZE with a commit
        after each. Returns how many rows were actually consumed, so
        the caller can track its own overall budget across batches."""
        self.ensure_one()
        if not self.pending_rows_json:
            if self.processing_state != "done":
                self._finalize_processing()
            return 0

        try:
            pending = json.loads(self.pending_rows_json)
        except (ValueError, TypeError):
            _logger.exception(
                "contact_import: batch '%s' had unreadable pending_rows_json - "
                "marking done to avoid looping on it forever", self.name,
            )
            self.write({"pending_rows_json": False, "pending_count": 0})
            self._finalize_processing()
            return 0

        if self.processing_state == "queued":
            self.processing_state = "processing"

        profile = self.profile_id
        Staging = self.env["import.staging.record"]
        rows_consumed = 0

        while pending and rows_consumed < row_budget:
            chunk = pending[:self._CHUNK_SIZE]
            pending = pending[self._CHUNK_SIZE:]

            errors = 0
            skipped = 0
            error_lines = []

            for entry in chunk:
                row_number = entry.get("row_number", 0)
                vals = entry.get("vals") or {}
                raw = entry.get("raw") or {}
                label = vals.get("name") or "unnamed row"

                try:
                    with self.env.cr.savepoint():
                        staging = Staging._create_from_import_row(profile, self, vals, raw)
                except Exception as exc:  # noqa: BLE001 - one bad row must not stop the batch
                    errors += 1
                    _logger.exception(
                        "contact_import: batch '%s' row %d (%s) failed", self.name, row_number, label,
                    )
                    if len(error_lines) < self._MAX_LOGGED_ERRORS:
                        error_lines.append("Row %d (%s): %s" % (row_number, label, exc))
                    continue

                if staging is None:
                    skipped += 1
                    continue

                try:
                    with self.env.cr.savepoint():
                        staging.action_compute_candidates()
                except Exception as exc:  # noqa: BLE001 - matching failure shouldn't lose the row
                    _logger.exception(
                        "contact_import: batch '%s' row %d (%s) staged but matching failed",
                        self.name, row_number, label,
                    )
                    if len(error_lines) < self._MAX_LOGGED_ERRORS:
                        error_lines.append(
                            "Row %d (%s): staged, but matching failed (%s) - use "
                            "Recompute Matches on this row once fixed." % (row_number, label, exc)
                        )

            rows_consumed += len(chunk)

            # Persist progress and commit before moving to the next
            # sub-chunk - a crash/restart between chunks loses at most
            # this one chunk's worth of work, not the whole batch.
            self.write({
                "pending_rows_json": json.dumps(pending) if pending else False,
                "pending_count": len(pending),
                "error_count": self.error_count + errors,
                "skipped_count": self.skipped_count + skipped,
                "import_log": self._append_log(error_lines),
            })
            self.env.cr.commit()

        if not pending:
            self._finalize_processing()

        return rows_consumed

    def _append_log(self, new_lines):
        self.ensure_one()
        if not new_lines:
            return self.import_log
        existing = self.import_log or ""
        return (existing + "\n" if existing else "") + "\n".join(new_lines)

    def _finalize_processing(self):
        self.ensure_one()
        self.processing_state = "done"
        _logger.info(
            "contact_import: batch '%s' finished background processing - "
            "%d staged, %d skipped (empty name or duplicate), %d errored",
            self.name, self.created_count, self.skipped_count, self.error_count,
        )

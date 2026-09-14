# Contact Import — Module README

**Module:** `contact_import`
**Location:** `/opt/odoo17/odoo-server/fermenteria-custom/contact_import`
**Repo:** `github.com/Smakelig/discuss-addon` (tracked directly in the superproject — not a
submodule, same as `discuss_addons` and `discuss_push`)

## What this module does

A generic staging/review layer for importing contacts (and later other models — the schema is
deliberately model-agnostic) without creating duplicates:

CSV upload → `import.staging.record` rows → fuzzy match candidates (exact email, normalized
phone, trigram name similarity via PostgreSQL's `pg_trgm`) → a human explicitly links or creates
each row.

**Nothing writes to `res.partner` automatically.** Every match, however confident, waits for a
human to confirm it.

## Import error handling

The CSV wizard processes rows one at a time, each inside its own DB savepoint, so one bad row
(a constraint violation, an unexpected value) rolls back only that row instead of aborting the
whole file. A separate savepoint wraps candidate-matching per row, so if matching throws, the
staged row is kept — it just needs a manual "Recompute Matches" click. Progress commits to the
DB every 200 rows so a worker restart/timeout partway through a large file doesn't lose
everything already staged (still a single-request loop, not chunked/background processing —
see "future scope" below for that). Every run writes a summary (created / skipped-as-duplicate
or empty-name / errored) to the batch's Import Log, shown as a warning banner on the batch form
whenever `error_count > 0`. Full tracebacks always go to `/var/log/odoo17/odoo.log` regardless
of what's shown in the UI log.

Currently wired for CSV only. Its own docstring names "a Google sync" as a future profile the
schema already supports, not something built yet — despite the name, this is not a
Google-Contacts-specific tool, and shouldn't be assumed to have any Google integration if picked
up fresh.

## Odoo 17 standard alignment

Verified against the actual 17.0 source (`odoo/addons/base/models/res_partner.py`,
`res_country.py`, `phone_validation`), not memory:

- **State/Province** (`state_id`) and **Street 2** (`street2`) are standard `res.partner`
  fields we weren't mapping before - now staged and resolved (state lookup scoped to the
  resolved country, since `res.country.state`'s own uniqueness constraint is per-country, not
  global - matching by name/code without that scope risks resolving to the wrong country's
  state entirely).
- **Phone formatting**: `phone_validation` (auto-installed alongside `base`+`mail`, which this
  instance has) adds `_phone_format()` to every model, but it only fires as a form *onchange* -
  never on a server-side `create()`. `action_create_new()` now calls it explicitly after
  creating the record, formatting to `INTERNATIONAL` using the record's own `country_id`. Falls
  back to the raw typed value if the number can't be parsed for that country, rather than
  blanking it.
- **Email normalization**: uses Odoo's own `odoo.tools.email_normalize` (lowercases the domain,
  trims, strips a `Name <addr>` wrapper) instead of a bare `.strip()`. Falls back to the raw
  value if normalization can't make sense of it.

## Optional Instagram enrichment

If `instagram_manager` happens to be installed alongside this module (checked at runtime via
`'instagram.contact' in self.env.registry` - **no manifest dependency**, this module stays
installable on its own), `action_compute_candidates()` will also try to fill a blank
email/phone on a staged row from the business's own Instagram Contact Directory
(`instagram.contact`).

Those `instagram.contact.phone`/`.email` values are themselves only ever populated there from
Meta's "Download Your Information" synced-contacts export on an exact name match - see that
module's own field help text - a real, deliberately-vetted source, not something scraped from a
casual DM.

Matching policy is copied exactly from `instagram_manager`'s own
`instagram.contact.partner.reconcile.wizard`: only an **exact, unambiguous** `display_name`
match is used. Fuzzy or ambiguous matches are skipped outright, never guessed at - the same
"real mistake risk, not a cosmetic one" reasoning that wizard's own docstring gives for never
auto-linking on a fuzzy match. Fill-blanks-only: never overwrites a value the source file
itself provided. When it does fill something, `instagram_enrichment_note` on the staging row
records what happened and which Directory entry it came from, so a reviewer can see where the
value came from before confirming the row.

## Background processing for large imports

The CSV wizard no longer processes rows synchronously in one HTTP request. `action_import()`
now only decodes the file and resolves each row's column values (fast, no DB queries) into
`import.batch.pending_rows_json`, sets the batch to `processing_state = 'queued'`, and returns
immediately. `import.batch._process_pending_chunk()` - triggered right away via
`ir.cron._trigger()` (see `data/ir_cron_data.xml`), and driven on a 15-minute fallback interval
otherwise - does the actual per-row work (dedup check, staging record creation, candidate
matching, Instagram enrichment) in chunks of 200 with a DB commit after each, so a worker
restart mid-run loses at most one chunk, not the whole batch. A single cron invocation caps
itself at 2000 rows total across however many batches are pending, re-triggering itself if more
remains, so one huge file can't monopolize a cron worker and starve the OTHER scheduled jobs on
this instance (Meta token refresh, OCR bill-scan, etc. - see CLAUDE.md's infra notes, which
named "contact-import batches" by name as one reason `max_cron_threads` was raised to 2).

The batch form shows `processing_state`/`pending_count` and updates live as background
processing works through the file - safe to close and come back later.

## Review UI

- The staging record list has a proper search view now (filters for each state, a High
  Confidence filter, an Instagram-Enriched filter, group-by Batch/State) - this also fixes
  `action_import_staging_record`'s `search_default_new` context key, which had no matching
  filter to actually apply before.
- "Ignore Selected" and "Recompute Matches (Selected)" are available from the list view's
  gear/Action menu for a multi-selection - both underlying methods were already written to
  operate on a recordset (no `ensure_one()`), so this only needed the `ir.actions.server`
  binding (`binding_model_id` + `binding_type` + `binding_view_types`), per this project's own
  documented convention for exposing bulk actions in the Action menu.

## Tests

`tests/` covers the fuzzy matching/scoring logic (`test_matching.py`), the standardization work
- country/state resolution scoped correctly, phone formatting, email normalization
(`test_standardization.py`), the queue-then-background-process pipeline including per-row
failure isolation and both dedup cases (`test_wizard_import.py`), and the optional Instagram
enrichment integration, which skips cleanly if `instagram_manager` isn't installed
(`test_instagram_enrichment.py`). Run with `-u contact_import --test-enable --test-tags
contact_import` (or your usual `-d Fermenteria2.0` invocation with those flags added) against a
real Odoo 17 + Postgres instance - these haven't been executed in this repo, only written and
reviewed for correctness against the real 17.0 API.

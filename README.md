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

Currently wired for CSV only. Its own docstring names "a Google sync" as a future profile the
schema already supports, not something built yet — despite the name, this is not a
Google-Contacts-specific tool, and shouldn't be assumed to have any Google integration if picked
up fresh.

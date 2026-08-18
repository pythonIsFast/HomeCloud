# HomeCloud — Project Guide for Claude

HomeCloud is a self-hosted, minimal cloud dashboard — a small private "AWS
clone" built for learning purposes. It currently provides the foundation
(auth + resource registry + dashboard). Compute, storage and database services
will be added later as blueprints under `app/services/`.

**Read this file before writing any code in this repository.** It describes two
things you must not violate: the dependency policy and the resource pattern.

---

## 1. Hard rules

### 1.1 Dependency policy — no third-party libraries

`requirements.txt` contains **exactly two** entries and must stay that way:

```
flask
gunicorn
```

Forbidden — do not add, do not import, do not suggest:

- `Flask-SQLAlchemy`, SQLAlchemy, any ORM
- `Flask-Login`, `Flask-Session`, `Flask-WTF`, `WTForms`
- `PyJWT`, `authlib`, `passlib`, `bcrypt`
- `python-dotenv`, `marshmallow`, `pydantic`, `requests`
- any other PyPI package, and any frontend package (no React, Vue, Tailwind,
  no npm, no build step)

Allowed, because they ship *with* Flask and add no new install:

- `werkzeug` (used for password hashing via `werkzeug.security`)
- `jinja2` (templates), `click` (CLI commands)

If a task seems to require a library, **write the logic yourself with the
Python standard library** and add a short comment explaining the approach and
why the stdlib route was taken. Two examples already in the codebase:

- `app/auth/jwt.py` — JWT/HS256 implemented with `hmac`, `hashlib`, `base64`,
  `json`, `time` instead of PyJWT. A JWS is just
  `b64url(header).b64url(payload).b64url(hmac_sha256(...))`.
- `app/db.py` — plain `sqlite3` with raw SQL strings instead of an ORM.

### 1.2 SQL access

- Standard library `sqlite3` only, plain SQL statements, no ORM, no query builder.
- **Always use parameter binding** (`?` placeholders). Never build SQL with
  f-strings or string concatenation from request data.
- Go through the helpers in `app/db.py` (`query`, `execute`, `get_db`) so every
  request reuses one connection and `PRAGMA foreign_keys = ON` stays set.
- Schema changes belong in `app/schema.sql` and must be written so that
  re-running the script is harmless (`CREATE TABLE IF NOT EXISTS`, etc.).

### 1.3 Language

Everything user-visible and everything in the repository is **English**:
code, identifiers, comments, docstrings, commit messages, UI strings,
error messages, and this file. (Chat with the user may be in German.)

### 1.4 Do not start servers or run scripts unprompted

The user tests manually. Do **not** run `flask run`, `gunicorn`, or any Python
script without asking first.

---

## 2. The resource pattern (most important design decision)

**There is exactly one table for all service objects: `resources`.**

Every future service — compute, storage, database, dns, whatever — stores its
objects as rows in `resources`, distinguished by `service_type`. A service
**never creates its own table**.

| column         | meaning                                                          |
| -------------- | ---------------------------------------------------------------- |
| `id`           | surrogate key, referenced by `audit_log.resource_id`             |
| `user_id`      | owner; every query is scoped by it                               |
| `service_type` | which service owns the row: `compute`, `storage`, `database`, …   |
| `name`         | user-chosen name, unique per (user, service_type)                |
| `status`       | lifecycle state, see `resources.STATUSES`                        |
| `config_json`  | service-specific payload, serialized JSON object                 |
| `created_at` / `updated_at` | UTC timestamps written by SQLite (`datetime('now')`) |

Why this shape:

- The dashboard, audit log, quotas and permissions are written **once** and
  work for every service that is added later.
- Adding a service means adding a blueprint plus a `service_type` string — no
  migration, no new table, no dashboard change.
- The price is that service-specific fields are not individually indexable
  inside `config_json`. That is an accepted trade-off for a small private
  deployment.

Rules when adding a service:

1. Create `app/services/<name>/__init__.py` with a `Blueprint`
   (`url_prefix="/<name>"`) and register it in `app/__init__.py`.
2. Use the functions in `app/core/resources.py` (`create`, `list_for_user`,
   `get`, `set_status`, `set_config`, `delete`) instead of writing new SQL.
   Extend that module if a query is genuinely missing.
3. Put everything service-specific into the `config` dict — it is stored in
   `config_json`.
4. Protect endpoints with `@guards.login_required` (or `@guards.admin_required`)
   from `app/auth/guards.py`, and read the caller via `guards.current_user()`.
5. Write an `audit.log_action(...)` entry for every state change, using the
   `"<service>.<verb>"` naming convention (e.g. `compute.start`).

---

## 3. Layout

```
HomeCloud/
├── app/
│   ├── __init__.py        # application factory create_app(), blueprint registration
│   ├── config.py          # config from env vars + instance/secret_key handling
│   ├── db.py              # sqlite3 connection, query/execute helpers, init-db CLI
│   ├── audit.py           # append-only audit_log writer (cross-cutting)
│   ├── schema.sql         # the whole DDL for all four tables
│   ├── auth/
│   │   ├── __init__.py    # Blueprint "auth", url_prefix /auth
│   │   ├── jwt.py         # hand-written JWT (HS256) encode/decode/verify
│   │   ├── models.py      # users + api_keys SQL, password & key hashing
│   │   ├── guards.py      # current_user(), login_required, admin_required, cookies
│   │   └── routes.py      # /auth/login page, /auth/api/* JSON endpoints
│   ├── core/
│   │   ├── __init__.py    # Blueprint "core"
│   │   ├── resources.py   # the generic resource registry (see section 2)
│   │   └── routes.py      # dashboard page, /api/resources, /api/audit, /healthz
│   ├── services/          # one subpackage per service, added later
│   ├── static/
│   │   ├── css/style.css
│   │   └── js/{app.js,login.js,dashboard.js}
│   └── templates/{base.html,_icons.html,login.html,dashboard.html}
├── instance/              # SQLite DB + secret_key (gitignored, not in the repo)
├── wsgi.py                # gunicorn entry point
├── requirements.txt
├── .gitignore
└── CLAUDE.md
```

Import direction is one-way and must stay that way:

```
app/__init__.py  ->  auth, core
auth             ->  db, audit
core             ->  db, audit, auth.guards
services/*       ->  db, audit, auth.guards, core.resources
```

`db.py` and `audit.py` import nothing from `auth` or `core`.

---

## 4. Auth design (all hand-written)

- **Passwords**: `werkzeug.security.generate_password_hash` /
  `check_password_hash` (scrypt by default). Plaintext passwords are never
  stored or logged.
- **Sessions are stateless JWTs.** On login the server issues an HS256 token
  (`sub`, `email`, `role`, `iat`, `exp`) signed with `SECRET_KEY` and sets it as
  an `HttpOnly`, `SameSite=Lax` cookie named `homecloud_token`. Frontend JS
  never sees the token. Logout deletes the cookie.
  A consequence of statelessness: a leaked token stays valid until `exp`
  (default 12 h, `HOMECLOUD_JWT_TTL`). There is no revocation list.
- **API keys** for machine access: `hc_` + `secrets.token_urlsafe(32)`, sent as
  `X-API-Key`. Only the SHA-256 hash is stored. SHA-256 rather than a slow KDF
  is deliberate — the key is a 256-bit random secret, not a human password, and
  the lookup must be a single deterministic indexed query.
- **Credential precedence** in `guards.current_user()`:
  `Authorization: Bearer <jwt>` → `X-API-Key` → session cookie.
- The **first registered account becomes `admin`**; every later one is `user`.
  Set `HOMECLOUD_ALLOW_REGISTRATION=0` to close registration afterwards.
- Login failures return one generic `invalid credentials` message for both
  unknown email and wrong password (no account enumeration).

### Endpoints

| method   | path                     | auth  | purpose                            |
| -------- | ------------------------ | ----- | ---------------------------------- |
| `GET`    | `/`                      | yes   | dashboard page                     |
| `GET`    | `/healthz`               | no    | liveness probe for nginx           |
| `GET`    | `/auth/login`            | no    | login + registration page          |
| `POST`   | `/auth/api/register`     | no    | create account                     |
| `POST`   | `/auth/api/login`        | no    | issue JWT, set cookie              |
| `POST`   | `/auth/api/logout`       | no    | clear cookie                       |
| `GET`    | `/auth/api/me`           | yes   | current user                       |
| `GET`    | `/auth/api/keys`         | yes   | list own API keys (metadata only)  |
| `POST`   | `/auth/api/keys`         | yes   | create key, returns plaintext once |
| `DELETE` | `/auth/api/keys/<id>`    | yes   | delete own key                     |
| `GET`    | `/api/resources`         | yes   | list own resources, `?service_type=` |
| `GET`    | `/api/resources/<id>`    | yes   | one resource                       |
| `GET`    | `/api/audit`             | yes   | recent audit entries               |

---

## 5. Configuration

All configuration comes from environment variables (`app/config.py`); there is
no `.env` loader.

| variable                       | default                  | meaning                          |
| ------------------------------ | ------------------------ | -------------------------------- |
| `HOMECLOUD_SECRET_KEY`         | generated into `instance/secret_key` | HMAC key for JWTs    |
| `HOMECLOUD_JWT_TTL`            | `43200` (12 h)           | token lifetime in seconds        |
| `HOMECLOUD_COOKIE_SECURE`      | `0`                      | set to `1` behind HTTPS/nginx    |
| `HOMECLOUD_ALLOW_REGISTRATION` | `1`                      | set to `0` to close registration |

Rotating `HOMECLOUD_SECRET_KEY` (or deleting `instance/secret_key`) invalidates
all existing sessions.

---

## 6. Running it (the user does this, not you)

```bash
python3 -m venv .venv && . .venv/bin/activate && pip install -r requirements.txt
flask --app app init-db          # creates instance/homecloud.db from schema.sql
flask --app app run --debug --port 6002               # development
gunicorn --workers 2 --bind 127.0.0.1:6002 wsgi:app   # production
```

HomeCloud listens on **port 6002** by convention, in development as well as in
production. The port is not stored in the code -- it is passed on the command
line (`--port` / `--bind`), so keep using 6002 in any command or unit file.

Deployment target is a Proxmox LXC container with nginx as reverse proxy in
front of gunicorn on `127.0.0.1:6002`. When TLS is terminated by nginx, set
`HOMECLOUD_COOKIE_SECURE=1`.

---

## 7. Frontend conventions and visual language

Vanilla HTML/CSS/JS, no framework, no build step, no CDN, no npm, **no
webfonts** (an external font request is a forbidden dependency).

### The intended look

An **infrastructure console**, not a marketing dashboard: dense rows, hairline
rules instead of shadows, a warm-neutral greyscale, monospace for everything
machine-generated, and exactly one accent colour used as punctuation.
Reference points: Vercel Geist (restrained neutrality, mono numerals), Linear
(hairlines, near-black surfaces, tight tracking), AWS Cloudscape compact
density, GitHub Primer (14/12 px text scale).

### Anti-patterns — do not reintroduce these

They are the visual signature of generated UI and were deliberately removed:

- **No gradients.** Not on panels, not on the brand mark, not on text, not as a
  decorative background. Flat surfaces only.
- **No indigo/violet accent.** The accent is ochre (`--accent`), and it is used
  only for links, focus outlines and the active nav marker.
- **The primary button is monochrome** — near-black on light, near-white on dark
  (`--solid`). Never accent-coloured.
- **No shadows on panels.** Shadows exist only on floating overlays (menu,
  modal, toast) via `--shadow-overlay`.
- **Small radii only** (`--r-xs` 2 px … `--r-lg` 6 px). No `rounded-xl` cards.
  Pill shapes are limited to the 6 px status dot.
- **No row of big-number cards.** Aggregate figures go into the `.metrics`
  strip: one hairline box, inline cells, value in mono.
- **No card-wrapping everything and no nested cards.** One flat `.panel` per
  section; tables sit directly inside it.
- **Empty states are one left-aligned line** of text (`.empty`), optionally with
  a `<code>` pointer. No illustrations, no centred hero, no call-to-action art.
- **No icons inside labelled buttons.** Icons appear in the sidebar and in
  icon-only controls only.
- **No status pills.** Status is a coloured 6 px dot plus the plain word
  (`HC.status()`).
- **No marketing copy.** Labels state what a thing is (`status = running`,
  `0 rows`, `no service registered`), never how great it is. The sign-in page is
  a single narrow centred card — no split hero, no feature list, no tagline.

### Files

| file                        | role                                                    |
| --------------------------- | ------------------------------------------------------- |
| `templates/base.html`       | skeleton, favicon, theme bootstrap, toast region        |
| `templates/_icons.html`     | 16x16 SVG sprite, `<symbol id="i-...">`                  |
| `templates/login.html`      | centred sign-in / registration card                     |
| `templates/dashboard.html`  | shell: sidebar + topbar + four views                    |
| `static/css/style.css`      | tokens and all components                               |
| `static/js/app.js`          | shared `HC` helpers, loaded on every page               |
| `static/js/login.js`        | sign-in behaviour                                       |
| `static/js/dashboard.js`    | console behaviour                                       |

### Tokens

Every colour, size, radius and spacing value is a custom property in `:root`.
Never hardcode a hex value or a px font size in a component.

- Greyscale: `--n-0` … `--n-900` (warm, no blue tint), mapped to roles
  `--canvas`, `--surface`, `--surface-sunken/hover/active`, `--hairline`,
  `--hairline-strong`, `--fg`, `--fg-secondary`, `--fg-muted`, `--fg-faint`.
- Type scale: `--t-micro` 10.5 px (uppercase labels) · `--t-xs` 11.5 ·
  `--t-sm` 12.5 · `--t-base` 13 (body) · `--t-md` 14 · `--t-lg` 16 · `--t-xl` 20.
  Headings use negative tracking; only `.label-micro` uses positive tracking.
- Fonts: `--sans` (system stack) and `--mono`. Mono is structural, not
  decorative: ids, timestamps, counts, service types, breadcrumbs, key values.
  `font-variant-numeric: tabular-nums` is set on `body`.
- Density: rows are `--row-py` 7 px tall, nav items 26 px, buttons/inputs
  26–28 px, topbar 44 px, sidebar 216 px.
- Both themes are mandatory: light default, dark via `prefers-color-scheme`,
  explicit choice in `localStorage` (`homecloud-theme`) applied as
  `data-theme` on `<html>`. A new token must be added to the light block *and*
  both dark blocks.

### Components

`.panel` (+ `.panel-head` / `.panel-body[.flush]` / `.panel-foot`), `.metrics` +
`.metric`, `table.data` with `td.num` / `td.mono` / `td.primary` / `td.right`,
`.status` + `.dot`, `.tag`, `.btn` (+ `-solid` / `-quiet` / `-link` / `-danger` /
`-icon` / `-tall` / `-block`), `.field` + `.input` / `.select`, `.find` (filter
box with a `/` hint), `.empty`, `.bar` (loading), `.menu`, `.scrim` + `.modal`,
`.toast`, `.label-micro`, `.initials`, `kbd`.

### Behaviour rules

- **Never `innerHTML` with server data.** Build nodes and set `textContent`; use
  `HC.cell()`, `HC.status()`, `HC.tag()`. A static literal string is fine.
- All requests go through `HC.api(path, options)` — sets JSON headers, never
  throws, returns `{ok, status, data}`, redirects to `/auth/login?next=…` on 401.
- Feedback for every async action: `HC.setBusy(button, true)` (swaps the label
  for a 11 px spinner), `HC.renderLoadingRows(tbody, rows, cols)` while a table
  loads, `HC.toast(title, text, "success"|"error")` for the outcome.
- Timestamps from SQLite are UTC without a marker — render with
  `HC.formatDateTime()` (absolute, `YYYY-MM-DD HH:MM`) or `HC.formatAge()`
  (`5m ago`). Both add the `Z` before parsing.
- The console is one page with hash-routed views (`#overview`, `#resources`,
  `#activity`, `#keys`). A new view needs a `<section class="view"
  data-view="x">`, an entry in the `VIEWS` map and a `.nav-item[data-view="x"]`.
  Services without a blueprint are `.nav-item.is-disabled` placeholders.
- Keyboard: `/` focuses the resource filter, `Escape` closes menu, off-canvas
  nav and modal. Keep new shortcuts single-key and non-destructive.
- Accessibility is part of "done": one focus outline for everything,
  `aria-current="page"` on the active nav item, `aria-busy` on loading buttons,
  `aria-expanded` on disclosure controls, and a layout that works down to
  360 px (the sidebar goes off-canvas at 820 px).

---

## 8. Git workflow (follow this for every change)

1. `git pull --rebase`
2. **If the pull fails: stop and abort.** Do not code, do not force anything,
   report the failure to the user. (No remote is configured yet, so this step
   is a no-op / expected failure until one exists — in that case say so and
   continue.)
3. Write the code.
4. Test if needed (ask the user before running anything).
5. Extend `.gitignore` if new generated files appeared.
6. `git add .`
7. `git commit -m "short message"`

Commit messages: short, imperative, English (e.g. `add compute service
blueprint`). Never commit `instance/` contents — the DB and `secret_key` are
ignored on purpose. Do not push unless the user asks.

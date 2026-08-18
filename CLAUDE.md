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
│   │   └── js/{login.js,dashboard.js}
│   └── templates/{base.html,login.html,dashboard.html}
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
flask --app app run --debug      # development
gunicorn --workers 2 --bind 127.0.0.1:8000 wsgi:app   # production
```

Deployment target is a Proxmox LXC container with nginx as reverse proxy in
front of gunicorn on `127.0.0.1:8000`. When TLS is terminated by nginx, set
`HOMECLOUD_COOKIE_SECURE=1`.

---

## 7. Frontend conventions

- Vanilla HTML/CSS/JS, no framework, no build step, no CDN imports.
- Templates extend `app/templates/base.html`; the header always shows
  "HomeCloud".
- Data is loaded with `fetch()` against the JSON API; a `401` means the token
  expired → redirect to `/auth/login?next=…`.
- **Never use `innerHTML` with server data.** Build nodes and assign
  `textContent` (see `renderRow` in `dashboard.js`).
- Colors and spacing come from the CSS variables in `:root` — reuse them
  instead of hardcoding hex values.

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

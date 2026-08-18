# HomeCloud — Project Guide for Claude

HomeCloud is a self-hosted, minimal cloud dashboard — a small private "AWS
clone" built for learning purposes. It runs standalone on plain Linux hosts and
depends on no hypervisor management stack.

Present: auth, the shared resource registry, the console, quota administration,
and **compute** — Firecracker microVMs (section 8). Storage and database
services will follow as further blueprints under `app/services/`.

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
- Go through the helpers in `app/db.py` — `query`, `execute` (returns the new
  row id), `modify` (returns the affected row count, used for atomic state
  transitions), `get_db` for the request connection and `connect` for the
  worker. They are what guarantee the tuning PRAGMAs (WAL, busy_timeout,
  foreign_keys) are set on every connection; see section 9.
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

**What the rule does not forbid.** "One table for all service objects" is about
the things a service *manages* — VMs, buckets, databases. Platform plumbing may
have its own tables, and currently does:

| table    | why it is not a service object                                  |
| -------- | --------------------------------------------------------------- |
| `jobs`   | work queue between the web process and the privileged worker      |
| `limits` | quota policy, shared by every service, edited by admins           |

If you find yourself wanting a table for "my service's instances", that is the
rule biting — use `resources`. If you want one for a mechanism the platform
needs regardless of service, that is fine; document it here.

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
│   ├── audit.py           # append-only audit_log writer + prune-audit CLI
│   ├── jobs.py            # job queue: web process enqueues, worker claims
│   ├── limits.py          # per-user quota, installation default + overrides
│   ├── schema.sql         # the whole DDL (4 registry tables + jobs + limits)
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
│   ├── services/
│   │   └── compute/       # Firecracker microVMs (see section 8)
│   │       ├── __init__.py    # Blueprint "compute", url_prefix /compute
│   │       ├── flavors.py     # size catalogue
│   │       ├── service.py     # validation, state machine, quota, enqueue
│   │       └── routes.py      # JSON API
│   ├── vmm/               # host side: only the privileged worker runs this
│   │   ├── net.py             # tap device + NAT, deterministic addressing
│   │   ├── images.py          # base image build, per-VM disk, key injection
│   │   ├── firecracker.py     # config, spawn, Unix-socket API, console tail
│   │   ├── console.py         # worker-owned serial input bridge for the web terminal
│   │   ├── worker.py          # job loop + process supervision
│   │   └── __main__.py        # python -m app.vmm
│   ├── static/
│   │   ├── css/style.css
│   │   └── js/{app.js,login.js,dashboard.js,terminal.js,compute.js,admin.js}
│   └── templates/{base.html,_icons.html,login.html,dashboard.html}
├── instance/              # gitignored host state, never in the repo:
│                          #   homecloud.db, secret_key,
│                          #   bin/{firecracker,jailer}, images/, vms/
├── wsgi.py                # gunicorn entry point
├── requirements.txt
├── install.sh              # fresh Debian/Ubuntu installation (run as root)
├── update.sh               # in-place deployment update (run as root)
├── DEPLOYMENT.md           # short deployment entry point for the two scripts
├── .gitignore
└── CLAUDE.md
```

Import direction is one-way and must stay that way:

```
app/__init__.py  ->  audit, db, auth, core, services/*
auth             ->  db, audit
core             ->  db, audit, limits, auth.guards, auth.models
services/*       ->  db, audit, jobs, limits, auth.guards, core.resources
vmm/*            ->  db, audit, jobs, core.resources   (never auth, never core.routes)
```

`db.py`, `jobs.py`, `limits.py` and `audit.py` import nothing from `auth`,
`core`, `services` or `vmm`. The web application never imports `vmm` except for
two read-only helpers (`firecracker.tail_console`, and `images` inside the CLI
command), plus the scoped `console.send_input` Unix-socket client. That client
only sends keystrokes to a worker-owned bridge; it cannot access KVM, processes,
tap devices or VM files.

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
| `GET`    | `/api/resources`         | yes   | own resources, keyset-paged        |
| `GET`    | `/api/resources/<id>`    | yes   | one resource                       |
| `GET`    | `/api/audit`             | yes   | recent audit entries, keyset-paged |
| `GET`    | `/compute/api/flavors`   | yes   | size catalogue + own quota/usage   |
| `GET`    | `/compute/api/instances` | yes   | own instances, keyset-paged        |
| `POST`   | `/compute/api/instances` | yes   | create instance (queues a job)     |
| `GET`    | `/compute/api/instances/<id>` | yes | one instance                    |
| `POST`   | `/compute/api/instances/<id>/actions/<action>` | yes | start/stop/restart |
| `DELETE` | `/compute/api/instances/<id>` | yes | delete instance                 |
| `GET`    | `/compute/api/instances/<id>/console` | yes | serial terminal output  |
| `POST`   | `/compute/api/instances/<id>/console/input` | yes | send terminal keys |
| `GET`    | `/api/admin/limits`      | admin | defaults + accounts with usage     |
| `PUT`    | `/api/admin/limits`      | admin | change installation defaults       |
| `PUT`    | `/api/admin/limits/<user_id>` | admin | set a user's override         |
| `DELETE` | `/api/admin/limits/<user_id>` | admin | drop a user's override        |

Every list endpoint is **keyset-paged**: it accepts `?before_id=<id>&limit=<n>`
and answers with `next_before_id` plus `has_more`. There is no `OFFSET`
anywhere, and no endpoint returns an unbounded list.

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
flask --app app init-db               # creates/updates instance/homecloud.db
flask --app app compute-build-image   # one-off: build the microVM base image
flask --app app show-config           # check the resolved paths
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

A **warm infrastructure console**: dense enough to read like a tool, warm
enough to feel built rather than generated. Paper-toned neutrals (never pure
grey, never pure black), one brand hue in copper, one informational hue in
teal, three status hues, moderate rounding, and monospace for everything
machine-generated.

Reference points for structure and density: Vercel Geist (restrained palette,
tabular numerals, 6–8 px radii), Linear (hairlines carrying the structure,
tight negative tracking on headings), AWS Cloudscape (compact density on a 4 px
scale), GitHub Primer (13–15 px UI text scale).

**Colour always means something.** Every hue in the palette is tied to a role —
brand, informational, ok, warning, bad. There is no decorative colour, and
equally: the UI must not read as black-and-white. If a figure or state has a
meaning, give it its hue.

### Anti-patterns — do not reintroduce these

They are the visual signature of generated UI and were deliberately removed:

- **No gradients.** Not on panels, not on the brand mark, not on text, not as a
  decorative background. Flat fills only.
- **No indigo/violet/cyan accent, no neon on dark.** The brand hue is copper
  (`--accent`: `#b4552b` light, `#e08a57` dark); the informational hue is teal
  (`--info`). Status hues are muted, not saturated.
- **No glassmorphism**, no backdrop blur for decoration.
- **No row of big-number cards.** Aggregate figures live in the `.metrics`
  strip: one bordered box, inline cells, a `tone-*` hue per figure, and a zero
  drops back to `--fg-faint` (a zero is not news).
- **No card-wrapping everything and no nested cards.** One `.panel` per section;
  tables sit directly inside it.
- **Empty states are one left-aligned line** of text (`.empty`), optionally with
  a `<code>` pointer. No illustrations, no centred hero, no call-to-action art.
- **No icons inside labelled buttons.** Icons belong in the sidebar and in
  icon-only controls.
- **No marketing copy.** Labels state what a thing is (`status = running`,
  `0 rows`, `no service registered`), never how great it is. The sign-in page is
  one narrow centred card — no split hero, no feature list, no tagline.

### Rounding and depth

Earlier revisions over-corrected into a flat, colourless look. The current
settings are the intended balance — keep them:

- Radii: `--r-xs` 4 px (tags, kbd) · `--r-sm` 6 px (buttons, inputs, nav items,
  toasts) · `--r` 9 px (panels, metric strip, menu) · `--r-lg` 12 px (modal) ·
  `--r-pill` (status chips, count badges, dots). Nothing is square-cornered,
  nothing is a bubble.
- Two shadow levels only: `--shadow-sm` on resting surfaces (panels, metric
  strip, sign-in card) and `--shadow-overlay` on floating layers. Never invent a
  third.
- The primary button is brand-coloured (`.btn-solid` uses `--accent`).

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

- Surfaces: paper-toned and warm — `--canvas`, `--surface`,
  `--surface-sunken/hover/active`, `--hairline`, `--hairline-strong`. Text:
  `--fg`, `--fg-secondary`, `--fg-muted`, `--fg-faint`. Dark mode is warm
  charcoal (`#141210`), never `#000`.
- Hues, each with a `-soft` tinted companion for backgrounds: `--accent`
  (brand, copper), `--info` (teal), `--ok`, `--warn`, `--bad`. Tinted grounds
  are what `.status`, `.tag`, `.initials`, `.field-error` and the active nav item
  are built from.
- Type scale: `--t-micro` 11 px (uppercase labels) · `--t-xs` 12 · `--t-sm` 13 ·
  `--t-base` 13.5 (body) · `--t-md` 15 · `--t-lg` 17 · `--t-xl` 21 ·
  `--t-2xl` 24 (page titles, metric figures). Headings use negative tracking;
  only `.label-micro` uses positive tracking.
- Fonts: `--sans` (system stack) and `--mono`. Mono is structural, not
  decorative: ids, timestamps, service types, breadcrumbs, event names, key
  values. Metric figures are sans with `tabular-nums` — mono zeros read badly at
  24 px.
- Density: rows are `--row-py` 9 px, nav items and buttons 30 px, inputs 30 px
  (36 px on the sign-in form), topbar 48 px, sidebar 224 px.
- Both themes are mandatory: light default, dark via `prefers-color-scheme`,
  explicit choice in `localStorage` (`homecloud-theme`) applied as
  `data-theme` on `<html>`. A new token must be added to the light block *and*
  both dark blocks.

### Components

`.panel` (+ `.panel-head` / `.panel-body[.flush]` / `.panel-foot`), `.metrics` +
`.metric.tone-{brand,ok,warn,info}`, `table.data` with `td.num` / `td.mono` /
`td.primary` / `td.right` / `td.wrap`, `.status.is-{ok,warn,bad,idle}` (tinted
chip, dot + word), `.event.is-{ok,bad}` (flat mono log line), `.tag` (teal mono
identifier), `.btn` (+ `-solid` / `-quiet` / `-link` / `-danger` / `-icon` /
`-tall` / `-sm` / `-block`), `.field` + `.field-label` / `.input` / `.select` /
`.field-note` / `.field-error`, `.find` (filter box with a `/` hint), `.secret`
(password reveal), `.empty`, `.bar` (loading), `.menu`, `.scrim` + `.modal`,
`.toast`, `.label-micro`, `.initials`, `kbd`.

Status chips carry a tinted ground; audit events do not — a log of 50 rows full
of pills is noise, so `.event` stays flat mono text with a coloured dot.

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
  `#activity`, `#keys`, `#compute`, `#admin`). A new view needs a
  `<section class="view" data-view="x">`, an entry in `VIEW_LABELS` in
  dashboard.js and a `.nav-item[data-view="x"]`. A view only counts as available
  if its section is actually in the DOM — that is how `#admin` stays invisible
  to non-admins instead of rendering an empty page. Services without a blueprint
  are `.nav-item.is-disabled` placeholders.
- Compute's index is intentionally an overview: it creates and lists instances
  only. Clicking an instance opens its `#vm/<id>` dashboard, where resource
  allocation, lifecycle controls and the terminal live. Do not put VM controls
  back into the compute index.
- `terminal.js` is the in-house ANSI terminal renderer. Keep it dependency-free
  and bounded: it supports the serial-shell control sequences HomeCloud emits,
  retains at most 800 lines and must never use `innerHTML` for terminal output.
- **A service brings its own script** (`compute.js`, `admin.js`), loaded after
  `dashboard.js`, registering itself as
  `window.HCViews.<name> = { load: () => ... }`. The shell calls every
  registered `load()` on boot and on Refresh, so `dashboard.js` never has to
  know which services exist. Do not put service logic into `dashboard.js`.
- Poll only while something is in flight: the compute view refreshes every 3 s
  while an instance is `pending`/`creating`/`stopping`/`deleting`, and stops once
  everything has settled. Never poll a quiet page.
- Keyboard: `/` focuses the resource filter, `Escape` closes menu, off-canvas
  nav and modal. Keep new shortcuts single-key and non-destructive.
- Accessibility is part of "done": one focus outline for everything,
  `aria-current="page"` on the active nav item, `aria-busy` on loading buttons,
  `aria-expanded` on disclosure controls, and a layout that works down to
  360 px (the sidebar goes off-canvas at 820 px).

---

## 8. Compute service: Firecracker microVMs

An instance is a `resources` row with `service_type='compute'`. Inside it the
user is root and does whatever they want — isolation comes from KVM, which is
why there is no sandboxing code in this project.

### Privilege split (the central design decision)

```
browser ──HTTP──> Flask (unprivileged)  ──INSERT──> jobs table
                                                       │
                                            SELECT ... claim
                                                       ▼
                                    app/vmm worker (root) ──> tap, NAT, firecracker
```

The web process **never** touches KVM, tap devices, images or processes. It
validates, checks quota, writes a `resources` row plus a `jobs` row, and answers
`202`. The worker does the slow, privileged part. Consequences to respect:

- A gunicorn worker is recycled and its children would be orphaned, so spawning
  a VM from a request handler is not an option — not even "just for testing".
- Root is needed **only** for networking. Disk work is deliberately
  unprivileged: `mke2fs -d` builds a filesystem from a directory, `cp
  --sparse=always` copies the base image. No loop mounts, so no leaked mounts.
- If the worker is not running, instances stay in `pending`. That is correct
  behaviour, not a bug — the UI says so in the empty state.

### Addressing without a lease table

Each VM gets a /30 derived from its resource id:

```
offset  = id * 4                    id 1  -> 10.71.0.4/30, host .5, guest .6
network = 10.71.<offset//256>.<offset%256>/30
tap     = hc-vm<id>                 MAC   = 06:00 + the four address bytes
```

Verified deterministic and collision-free for the full range; ids above 16383
are rejected. Because the id *is* the reservation there is no allocation table,
no lock and no race. The guest gets its address from the kernel command line
(`ip=...`), so there is no DHCP server to run.

### State machine

```
pending ──> creating ──> running ⇄ stopped ──> deleting ──> deleted
                   ↘         ↘         ↙
                        error
```

Allowed transitions live in one table, `service.TRANSITIONS`. Every action is an
atomic `UPDATE ... WHERE id = ? AND status IN (...)` via
`resources.transition()`, so two parallel "start" requests produce exactly one
job — the loser gets a 409. Never compare statuses by hand in a route.

The worker also *reconciles*: a guest that runs `poweroff` makes firecracker
exit, and nothing would tell the database. Each idle pass checks the recorded
pid (including a `/proc/<pid>/cmdline` check, so a recycled pid cannot fool it)
and writes back `stopped`.

### Host setup (one-off, the operator does this)

```bash
# 1. binaries and images, all under instance/ and gitignored
#    firecracker + jailer  -> instance/bin/
#    vmlinux-6.1.155       -> instance/images/
#    ubuntu-24.04.squashfs -> instance/images/
# 2. build the writable base image (no root needed)
flask --app app compute-build-image
# 3. check the resolved paths
flask --app app show-config
# 4. run the privileged worker
sudo -E env "PATH=$PATH" .venv/bin/python -m app.vmm
```

Requirements: `/dev/kvm`, `ip`, `iptables`, `e2fsprogs`, `squashfs-tools`. The
worker refuses to start with a clear message if any of them is missing rather
than failing per-VM later.

### Web terminal, no SSH keys

Instance creation does not accept an SSH key. The base image masks the guest's
OpenSSH units and configures `serial-getty@ttyS0` to log in as root. The UI
opens that serial terminal only after HomeCloud has authenticated and authorized
the resource owner. The web process sends at most 4096 bytes through a
worker-owned Unix socket; the privileged worker alone holds the FIFO connected
to Firecracker stdin. Rebuild the base image and recreate existing VMs to apply
this access model to disks that were made before this change.

### Quota

`limits` holds the installation default (`user_id IS NULL`) and per-user
overrides. `limits.check_new_vm()` compares count *and* the sums of vCPU, memory
and disk against the effective limits before a row is written. These are real
limits, not advisory numbers: a microVM cannot exceed the vCPU and memory it was
configured with, and its disk is a fixed-size image.

---

## 9. Scaling notes

The current shape is one host, one SQLite file, one worker. Where that ends and
what was done about it:

### Already done

- **WAL, `busy_timeout=5000`, `synchronous=NORMAL`** in `db.connect()`. Without
  WAL a single INSERT blocks every concurrent SELECT; without the timeout a lock
  conflict fails instantly with "database is locked". Set them on a *fresh*
  connection — `PRAGMA journal_mode` is silently ignored inside a transaction.
- **Keyset pagination everywhere**, never `OFFSET`. Page 200 must cost what page
  1 costs.
- **Covering indexes** for the queries that exist:
  `resources(user_id, service_type, id DESC)`, `audit_log(user_id, id DESC)`,
  `jobs(status, host, id)`.
- **`last_used_at` throttling** on the API-key path. Stamping it on every
  request turned a read workload into a write-bound one; it now refreshes at
  most every 5 minutes, via a condition inside the UPDATE.
- **`prune-audit` CLI** — `audit_log` is append-only and would otherwise grow
  without bound. Run it from cron; there is no scheduler in this project.

### Known ceilings

- **One SQLite file means one host.** Several app servers sharing the file over
  NFS is broken, not slow. Rough estimate, unmeasured: WAL sustains hundreds of
  writes per second and many concurrent readers on local SSD.
- **One worker per host.** `jobs.host` already exists, so a second compute host
  is "run another worker"; nothing in the schema has to change.
- **Memory is the real limit on VM density**, not the database.

### Postgres migration checklist

The plan is to move when deploying. Deliberately no abstraction layer yet — it
would be guesswork. What has to change, all of it inside `db.py`, `jobs.py`,
`limits.py`, `audit.py` and `core/resources.py`:

| SQLite                       | Postgres                                  |
| ---------------------------- | ----------------------------------------- |
| `?` placeholders             | `%s`                                      |
| `datetime('now')`            | `now()`                                   |
| `datetime('now', '-5 minutes')` | `now() - interval '5 minutes'`         |
| `INTEGER PRIMARY KEY AUTOINCREMENT` | `GENERATED BY DEFAULT AS IDENTITY` |
| `INSERT OR IGNORE`           | `ON CONFLICT DO NOTHING`                  |
| `json_extract(col, '$.k')`   | `col::jsonb -> 'k'`                       |
| `cursor.lastrowid`           | `RETURNING id`                            |
| `sqlite3.Row`                | `RealDictCursor` or a row factory         |

A Postgres driver (`psycopg`) is a third-party package and therefore a
**documented exception** to section 1.1, agreed for the deployment step. It does
not open the door to other packages: the ORM ban, the hand-written JWT and the
no-frontend-dependencies rule all stay.

The atomic-claim pattern (`UPDATE ... WHERE status = 'queued'`) works unchanged
on Postgres, and can later be sharpened with `FOR UPDATE SKIP LOCKED`.

---

## 10. Git workflow (follow this for every change)

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
8. `git push`

Commit messages: short, imperative, English (e.g. `add compute service
blueprint`). Never commit `instance/` contents — the DB and `secret_key` are
ignored on purpose. Push only after the commit succeeds and the remote is the
intended repository.

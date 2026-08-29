# GitInTheVan

<img src="frontend/public/gitinthevan-full.svg" alt="GitInTheVan" width="400">

A self-hostable man-in-the-middle LLM router/proxy for roleplay and creative writing. It intercepts OpenAI-compatible chat completion requests, applies transformations (lorebooks, cantrips, verification), and forwards them to your configured LLM endpoint.

Licensed under Mozilla Public License 2.0.

## Contents

[Why It Exists](#Why-It-Exists)  
[Features](#Features)  
[Upcoming Features](#Upcoming-Features)  
[Quick Start](#Quick-Start)  
[Updating](#Updating)  
[Configuration](#Configuration)  
[Database Support](#Database-Support)  
[Using with JanitorAI](#Using-with-JanitorAI)  
[Using with Wyvern Chat](#Using-with-Wyvern-Chat)  
[Cantrips](#Cantrips)  
[Verification](#Verification)  
[Persistent Memory](#Persistent-Memory)  
[Conversation Summarization](#Conversation-Summarization)  
[Context Budgeting](#Context-Budgeting)  
[Memory Rules](#Memory-Rules)  
[Activation Hierarchy](#Activation-Hierarchy)  
[Debug Mode](#Debug-Mode)  
[Comparing Runs](#Comparing-Runs)  
[Command Tags](#Command-Tags)  
[Maps](#Maps)  
[Development](#Development)
[Starting Out With Cantrips](#Starting-Out-With-Cantrips)  
[Geting Support](#Getting-Support)  
[Giving Support](#Giving-Support)  
[Disclaimer on Use of AI](#Disclaimer-on-Use-of-AI)  
[License](#License)  

[GUI Documentation](./docs/user-guide.md)

## Why It Exists

I enjoy creative writing, but I spent the early 2000's absorbed in MSN Groups play-by-post roleplay as well as email based play-by-post. I spent a decade doing tabletop gaming online. With my life as hectic as it is, writing with people has become more difficult than I'd like, simply due to scheduling.

But writing with LLMs has allwoed me to explore a ton of concepts and stories in my limited spare time. As such, I have used platforms like SillyTavern, JanitorAI, and others for several years now.

I was growing furstrated with the 'llmisms' - The ocmmon tells of an LLM, along with the struggles around context, persistent memory, and following instructions.

I thougth Lorebary was poised to become a solution to it. I like the idea of a man-in-the-middle proxy. I loved that it was open source, even if it was only for the sake of verification.

My intention had been to contribute to its codebase, but when I finally got around to looking at it in depth I found that it had gone closed-source, becoming yet another 'free' black box AI platform.

This has driven me to build my own properly open-source solution.

I don't have any problems with Lorebary or its creators, I simply believe that an open source solution, especially one that is easy for self-hosting, is the best path forward. GitInTheVan decentralizes and democratizes stricter control over roleplay and creative writing with LLMs.

I will stress that I am not going to replicate or 100% replace Lorebary's functionality. GitInTheVan is a separate tool that is going to work in a similar manner, as a man-in-the-middle instruction management proxy, but the primary focus will be around three things.

**JavaScript empowered Lorebook support** - Lorebooks that are built with JavaScript instead of JSON, executed in a sandbox and with limited functionality. These are NOT JanitorAI Scripts, but it will be compatible with JanitorAI Scripts. I'm calling these Cantrips because they're going to be like small bits of magic compared to the old style lorebooks.

**Persistant memory** - The ability to set limited flags and use memory from one chat to the next, and the ability to have events automatically summarized by a specific model and endpoint using custom prompts.

**LLM validation** - You select a model, it can be a different model than your writing model. You give it its own instructions, either via script/lorebook or just as a system prompt. The LLM evaluates the response it receives from the roleplay endpoint, and if it determines that the bot did not follow instructions, it sends it back automatically up to 'n' times (You set the number of retries) along with an additional instruction. This can include instructions around ensuring the response isn't speaking for the user. It can also include evaluating if the response is accurate for the character - For example, on an adversarial roleplay, ensuring the character doesn't go from 'I hate you' to 'Marry me' in only a handful of messages.

## Features

- **Proxy Router** — Forwards OpenAI-compatible requests to any LLM endpoint (OpenWebUI, OpenRouter, local LLM servers)
- **Multi-User** — Per-user API keys, endpoints, and configurations with full admin management (edit, disable, delete with cascade cleanup, password reset, API key regeneration)
- **Lorebooks** — JSON worldbook system with keyword matching, constant/selective entries, enable/disable per lorebook, import from SillyTavern/Chub/JanitorAI formats, and file export. Supports pipeline positioning (pre-Driver, pre-Navigator, etc.) and LLM-facing instructions
- **Cantrips** — Sandboxed JavaScript execution compatible with JanitorAI scripts, with per-chat persistent storage via `context.chat_data`. Four pipeline positions (Pre-Driver, Driver-Callable, Pre-Navigator, Post-Navigator). LLM instructions field for tool notifications. Includes built-in templates (dice roller, status tracker, day counter, weather system)
- **Driver-Callable Tools** — Writing LLM can invoke cantrips as tools during generation via a notification-based, turn-tracked approach that works with any model. No OpenAI function-calling support required. Auto-disables when no tools are active. Infinite-loop prevention via turn budget
- **Verification** — LLM-based response checking (Navigator) with configurable rules, automatic resubmission with retry limits, verification logs, and per-rule endpoint/model overrides
- **Persistent Memory** — Database-backed memory system using `<memstore>` tags. LLM responses are scanned for key/value pairs, stored per-conversation, and injected as a `[PERSISTENT MEMORY]` context block on subsequent requests. No zero-width character encoding — the database is the source of truth
- **Expanded Memory Scopes** — Beyond per-chat memory, cantrips have access to two additional persistent stores: `context.user_data` (per-user global, shared across all chats and cantrips) and `context.cantrip_data` (per-user per-cantrip, persists across chats but isolated to one cantrip). Same get/set/keys/delete API as `chat_data`
- **Conversation Summarization** — Automatically compresses long conversations when token count exceeds a configurable threshold. Older dialogue is summarized by a user-selected LLM and replaced with a `[CONVERSATION SUMMARY]` context block, while recent messages are always forwarded verbatim
- **Forbidden Words** — Global per-user phrase list checked against responses before the Navigator runs. Supports plain-text and regex matching, case-insensitive by default. Matches surfaced to the Navigator as concrete violations
- **Command Tags** — Per-request pipeline overrides via inline tags: `<VERIFY:off>`, `<SUMMARY:on>`, `<MEMORY:off>`, `<FORBIDDEN:off>`, `<DRIVER:on>`. Optional `:persist` flag saves to conversation memory. `<CMD:reset>` clears persistent overrides. One-off > persistent > GUI precedence
- **Embedded Lorebook Extraction** — `<jslorebook>` tags in character card scenario content are automatically extracted, desanitized, and stripped before forwarding. Scripts available for execution alongside user cantrips
- **Prefill Normalization** — Provider-specific assistant message prefilling. Converts trailing assistant messages to system instructions for OpenAI-compatible endpoints. Anthropic/Google pass through natively
- **Content Bypass Plugins** — Three encoding methods (space separation, dot separation, character replacement) configurable per endpoint. Each endpoint can have its own bypass strategy. Includes ToS violation warning
- **Tagging System** — Activate lorebooks, cantrips, and verification rules via `<#type-name#>` delimiters in persona or message text. Tags are auto-stripped before forwarding to the LLM. Centralized tag management on the Tags and Groups page
- **Tags and Groups** — Centralized tag management for all lorebooks and cantrips. Create group collections that activate multiple resources via a single `<#grouptag#>` tag. Groups can be blanket-active (every message) or tag-activated. Members are deduplicated and missing members are handled gracefully
- **Content Discovery and Sync** — Link any git repository as a content pack. Browse, install (linked with update tracking), or fork (independent copy) cantrips, lorebooks, skills, scenario rules, and maps. Safety scanner checks for malicious code. Admins can link local filesystem folders as global content sources. Create your own packs from existing resources via the Pack Creator
- **Skills & Writing Samples** — Reusable instruction sets and style references attached to endpoints. Skills inject behavioral directives into the system message; Writing Samples inject style references before the last user message
- **Diagnostics** — Automated endpoint and configuration checker for troubleshooting connectivity issues
- **Per-Server Sharing** — Mark a cantrip, lorebook or map public to share it with other users on the same instance. Public resources are browsable by tag and importable by any user, while private resources stay owner-only. Toggle per resource in its editor or in bulk from the Tags and Groups page
- **Security Hardening** — Rate limiting (proxy + management API), configurable CORS origins, password strength validation, request body size limits, audit logging for admin actions, configurable JWT expiration, global caps for driver-callable turns and verification retries
- **Per-Endpoint API Keys** — Create multiple `gitv_` API keys per user, each mapped to a specific endpoint for multi-platform routing. Managed on each endpoint card in the UI
- **Admin Panel** — Global caps (turns/retries use min of user/global), Users tab (create, edit, disable, delete, password reset, key regeneration), Update tab (in-app version check with GitHub release notifications and download links), Backup tab (scheduled and on-demand database backups, download/restore/delete, dialect-aware for SQLite/PostgreSQL/MariaDB), read-only audit logs, read-only server logs with runtime log level override without restart. Red badge on Admin sidebar button when updates are available
- **Admin Sitewide Banner** — Configurable info/warning/danger banner shown to all users, including on the login page, for maintenance notices or announcements
- **Maps** — Multi-stage LLM pipelines that chain multiple Driver passes (e.g., Writing LLM > Gamemaster LLM > Narrator LLM) into a single request. Each stage has its own lorebooks, cantrips, endpoint, model, driver-callable turns, and verification. Output modes (persist/sanitize/discard) control how stage output feeds the next stage. Sticky vs stage-only resource attachments. Activated via `<#map-tag#>` tags. Export/import as self-contained JSON with resource dedup options (keep_both/reuse/overwrite)
- **Web UI** — Full management interface built with Svelte 5 including cantrip tester, verification tester, forbidden word scanner, code editor with syntax highlighting, jump-to-top/bottom navigation, and log viewer
- **Multi-Database Support** — SQLite (default), PostgreSQL, and MariaDB/MySQL backends. SQLite for single-instance self-hosting; PostgreSQL or MariaDB for multi-instance horizontal scaling with a shared database server
- **LiteLLM Provider Compatibility** — Optional provider selection on endpoints enables LiteLLM integration for automatic parameter translation, auth format handling, and response normalization across 100+ LLM providers (Gemini, OpenAI, Anthropic, OpenRouter, DeepSeek, xAI, and more). Endpoints without a provider set use raw HTTP passthrough (backward compatible)
- **Context Budgeting** — Weighted token budget allocation across cantrips and lorebooks. Cantrips access their share via `context.budget` and can dynamically scale output detail (full/summary/bullets) based on remaining tokens. Configurable per-user budget percentage and context window override
- **Memory Rules** — Taggable per-conversation summarization overrides. Rules can override the token threshold, keep-recent count, prompt, or disable summarization entirely for specific conversations. Activate via `<#memory-rule-tag#>` tags
- **Debug Mode** — Stage-based pipeline timeline covering every transformation, plus the objects that acted on it: which cantrips ran (with their code, output and logs) and which did not, which lorebook entries matched and from where, which skills were injected, and each map stage's endpoint, output and verification. Records real token counts from the endpoint's usage response, per-call latency, and pipeline overhead — the time the proxy itself cost. Reasoning is kept in full. Streaming and non-streaming requests are both captured. Available as a tab under Dashboard, gated by the Debug Mode toggle in Settings
- **Run Comparison** — Put up to four debug runs side by side and diff them against a baseline you pick with a radio button, switching baseline freely without re-running anything. Diffs the non-LLM components directly — triggered cantrips, cantrip code, lorebook entries, skills, pass/fail verification results, model and endpoint differences — alongside token and latency deltas. **Replay** re-sends a run through your current configuration so you can change one cantrip and compare, without going back to your chat client. Save runs to pin them against retention, and export a run or comparison as JSON or Markdown
- **Thinking/Reasoning Output** — `preserve_thinking` setting controls whether `<think>` blocks and `reasoning_content` fields are stripped from or preserved in client-facing responses. Verification tester displays model thinking output alongside judgment results. Debug pipeline captures thinking content in metadata
- **Update System** — In-app update notifications: checks GitHub releases automatically, shows red badge on Admin sidebar button when updates are available. Admin Update tab displays version comparison, release notes, and download links with step-by-step instructions. Platform-specific update scripts (Windows/macOS/Linux) handle server stop, database backup, dependency reinstall, frontend rebuild, and server restart, serving a maintenance page on the server's port for the duration of the update

## Database Support

GitInTheVan supports three database backends:

| Backend | Driver | Use Case |
|---------|--------|----------|
| **SQLite** (default) | `aiosqlite` | Single-instance self-hosting. Zero configuration. |
| **PostgreSQL** | `asyncpg` | Multi-instance deployments, horizontal scaling, high concurrency. |
| **MariaDB / MySQL** | `aiomysql` | Multi-instance deployments on existing MySQL infrastructure. |

SQLite is included by default. PostgreSQL or MariaDB drivers are optional extras:

```bash
pip install -e ".[postgres]"   # PostgreSQL (asyncpg)
pip install -e ".[mysql]"      # MariaDB / MySQL (aiomysql)
```

Set the database URL in your `.env`:

```
# SQLite (default)
GITV_DATABASE_URL=sqlite+aiosqlite:///./data/gitinthevan.db

# PostgreSQL
GITV_DATABASE_URL=postgresql+asyncpg://user:password@localhost:5432/gitinthevan

# MariaDB / MySQL
GITV_DATABASE_URL=mysql+aiomysql://user:password@localhost:3306/gitinthevan
```

**Connection pool settings** (PostgreSQL / MariaDB only, ignored by SQLite):

| Variable | Default | Description |
|----------|---------|-------------|
| `GITV_DB_POOL_SIZE` | `10` | Number of persistent connections in the pool |
| `GITV_DB_MAX_OVERFLOW` | `20` | Additional connections allowed beyond pool size under load |
| `GITV_DB_POOL_RECYCLE` | `3600` | Seconds before a connection is recycled (prevents stale connections) |

**Migrations** are dialect-aware and run automatically on startup. Advisory locking prevents concurrent migration races when multiple application instances start against the same database simultaneously.

> **Note:** SQLite is the default and needs no setup. PostgreSQL and MariaDB are supported and have been verified against live servers — full migration and CRUD runs against ephemeral test containers for each. Docker Compose files ship for all three backends: `docker-compose.sqlite.yml`, `docker-compose.postgres.yml` and `docker-compose.mariadb.yml`.

## Upcoming Features

The following are planned for future releases.

- **Debug Comparison** — Compare up to four debug runs side by side, diff the cantrips, lorebooks and verification results between them, and track token and latency cost per run
- **Per-Object Version History** — Snapshot a cantrip, lorebook or skill before each edit, then copy, download or restore an earlier version without touching anything else
- **Cantrip Chaining** — Multi-turn LLM interactions for complex systems like dice resolution and critical tables
- **Natural-Language Cantrip Generator** — Describe what you want in plain English and an LLM generates the cantrip code or lorebook

## Quick Start

### Docker (Recommended for self-hosting)

Choose one of three configurations based on your needs:

```bash
# Option 1: SQLite (simplest, single-instance, zero-config)
docker compose -f docker-compose.sqlite.yml up -d

# Option 2: MariaDB (multi-instance scaling, persistent database)
docker compose -f docker-compose.mariadb.yml up -d

# Option 3: PostgreSQL (multi-instance scaling, persistent database)
docker compose -f docker-compose.postgres.yml up -d
```

Once running, open `http://localhost:8000` in your browser. Data persists in mounted volumes (`./data/`, `./.deno/`).

To stop:
```bash
docker compose -f docker-compose.sqlite.yml down      # (or mariadb/postgres)
```

### Easy Deploy (Non-Docker)

For non-technical users, a deploy script handles everything: Python setup, Deno download, frontend build, configuration, and server startup.

**Windows:**
```bash
scripts\deploy-windows.bat
```

**macOS:**
```bash
./scripts/deploy-macos.sh
```

**Linux:**
```bash
./scripts/deploy-linux.sh
```

The script will:
1. Create a Python virtual environment and install dependencies
2. Download Deno automatically (for cantrip sandbox)
3. Build the web UI frontend (requires Node.js 24+)
4. Create a `.env` configuration file from the template
5. Generate a self-signed SSL certificate for HTTPS (LAN access)
6. Start the server

If Python 3.12+ is not installed, the script offers to install it automatically (Windows: winget, macOS: Homebrew, Linux: apt/dnf/pacman).

Once running, open `https://localhost:8000` in your browser. See [HTTPS and LAN Access](#https-and-lan-access) below for certificate trust instructions.

> **For localhost-only use**: If you only need access from the same machine, you can remove the SSL settings from `.env` to run in HTTP mode. The server will be available at `http://localhost:8000`.

### Manual Setup

If you prefer to set things up manually or the deploy script doesn't work for your setup:

#### Prerequisites

- Python 3.12+
- Node.js 24+ (for building the frontend)
- [Deno](https://deno.land/) runtime (for cantrip sandbox)

#### Steps

```bash
# Clone and enter the project
cd GitInTheVan

# Create Python virtual environment
python -m venv .venv

# Activate it
.venv\Scripts\activate        # Windows
source .venv/bin/activate     # macOS/Linux

# Install Python dependencies
pip install -e ".[dev]"

# Install Deno (for cantrip sandbox)
# Option A: Download from https://deno.land and place at .deno/deno.exe
# Option B: Install globally and set GITV_DENO_PATH in .env
# Option C: The app auto-detects deno from PATH
# Option D: Let the deploy script download it automatically

# Build the frontend
cd frontend
npm install
npm run build
cd ..

# Configure environment
cp .env.example .env
# Edit .env with your endpoint URL, API key, and secret key

# Start the server
.venv\Scripts\uvicorn app.main:app --reload     # Windows
.venv/bin/uvicorn app.main:app --reload          # macOS/Linux
```

Open `http://localhost:8000` in your browser to access the management UI.

### First Run

1. Navigate to `https://localhost:8000` (or `http://localhost:8000` if HTTPS is disabled)
2. Accept the self-signed certificate warning if prompted (see [HTTPS and LAN Access](#https-and-lan-access))
3. Click "First run? Setup admin" to create your admin account
4. Save your `gitv_` API key — this is used for proxy requests
5. Go to **Endpoints** and add your LLM endpoint
6. Go to **Settings** and set your default endpoint
7. Point your client (JanitorAI, etc.) at your proxy URL using your `gitv_` API key

### Updating

Use **Admin → Update** in the web UI. Click *Check for Updates*, then *Update Now*. The server backs up its database, installs the new version, and restarts on its own.

**If you are several releases behind**, the upgrade runs as a chain of single-release steps rather than one jump — 0.15.42 → 0.16.1 → 0.18.0, restarting after each one. This is deliberate: a release only guarantees it can migrate the database from the release immediately before it, so skipping releases can leave the database missing columns that newer code expects. The plan is fixed the moment you confirm it, so a release published while your upgrade is running cannot pull you somewhere you did not approve.

The Update tab shows which step is running. On Windows each step opens its own console window; that is expected. If a step fails, the tab shows the log and offers *Retry this step* or *Discard plan*.

#### Upgrading from 0.15.x – 0.18.0

Automatic chaining arrived in 0.19.0, so an install older than that cannot chain itself — its updater is a snapshot from before the feature existed. Upgrade those installs with the standalone script, which needs nothing but Python 3.12:

```bash
curl -O https://raw.githubusercontent.com/Tydorius/GitInTheVan/main/scripts/chain-update.py
python chain-update.py --root /path/to/GitInTheVan --dry-run
```

Drop `--dry-run` to run it. Stop the server first. It backs up the database before each step, downloads each release in order, applies that release's migrations, and verifies the version advanced before continuing.

If you already jumped straight from 0.15.x/0.16.1 to 0.18.0 and now see `no such column: skills.budget_weight`, upgrading to 0.19.0 fixes it: 0.18.0 added that column to the model without a migration, so it was missing on every upgraded database. 0.19.0 adds the migration and, on first start, checks the whole schema against the models and repairs anything else additively (after taking a backup).

#### `'netstat' is not recognized` and the server never comes back (Windows)

If an update ended with that message and the browser now shows "GitInTheVan is updating" forever, the update's maintenance page is still holding the port. This is fixed in 0.19.1, but because the updater runs the *currently installed* version's script, an install on 0.18.0 needs one manual pass first:

```
C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe -Command "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | Where-Object { $_.CommandLine -like '*_maintenance_server.py*' -or $_.CommandLine -like '*app.main*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"
```

Then delete `data\_maintenance_server.py`, `data\auto-update.bat`, and any stale `data\gitv.pid` — but **not** `data\update-chain.json` — and start the server again with `.venv\Scripts\python -m app.main`. The new version is usually already extracted; check the top of `CHANGELOG.md` to confirm.

The underlying cause is `C:\Windows\System32` missing from your `PATH`. GitInTheVan does not modify your `PATH` and never has, so this predates the app and will be breaking other software too — worth repairing in System Properties → Environment Variables.

#### Docker

Docker installs are outside the auto-updater. Pull the new image and recreate the container; your mounted `data/` volume is preserved. The same one-release-at-a-time rule applies — do not skip several releases in one pull.

### HTTPS and LAN Access

The deploy scripts automatically generate a self-signed SSL certificate so that GitInTheVan can be accessed from other devices on your local network. This is required because browsers block HTTP requests from HTTPS sites (like JanitorAI) — a restriction called *mixed content blocking*.

The deploy scripts automatically generate a **local Root CA** and **CA-signed leaf certificate** in `data/ssl/`. The CA signs the leaf, so trusting the CA once trusts all future leaf certs. This is required for iOS/macOS Safari (which cannot trust bare leaf certs).

**Trusting the certificate on each device:**

On every device/browser that will connect to GitInTheVan:

1. Open your GitInTheVan URL directly in the browser address bar (e.g. `https://10.0.0.187:8000`)
2. Follow the platform-specific instructions below to trust the certificate

> **Why this is necessary**: Browsers silently block background requests (like JanitorAI's API calls) to servers with untrusted certificates. Unlike direct navigation, there is no warning dialog — the request simply fails.

> **"Unable to connect" instead of a cert warning?** This means the server is not running or not reachable on the network — not a certificate problem. Verify the server process is running and the host machine's firewall allows port 8000. On macOS 15+, also check [Local Network permissions](#macos-15-local-network-permissions) below.

**Platform-specific notes:**

- **Firefox (all platforms)**: Firefox uses its **own certificate store**, separate from the OS. Installing the CA in Windows Certificate Manager or macOS Keychain does NOT help Firefox. You must either:
  - **Option A**: Import the CA into Firefox directly:
    1. Go to `about:preferences#privacy` → scroll to **Certificates** → **View Certificates** → **Authorities** tab
    2. Click **Import** → select `ca.pem` (or `ca.crt`) from `data/ssl/`
    3. Check **Trust this CA to identify websites** → **OK**
    4. Restart Firefox
  - **Option B**: Enable enterprise root reading (uses the Windows trust store):
    1. Go to `about:config`
    2. Set `security.enterprise_roots.enabled` to `true`
    3. Restart Firefox
- **Chrome/Edge (desktop)**: Uses the OS trust store. After installing the CA certificate (see below), restart the browser. Alternatively, type `thisisunsafe` on the warning page to bypass.
- **Safari (macOS/iOS)**: Safari does not offer a self-signed cert bypass for non-localhost addresses. You must import the certificate into **Keychain Access** (macOS) or install a profile (iOS). See below.
- **Firefox on Android**: Works via the standard warning page → **Accept the Risk**.

#### macOS 15 Local Network Permissions

macOS 15 (Sequoia) introduced a privacy feature that requires apps to request permission before connecting to local network devices. Non-Safari browsers (Firefox, Chrome, Edge) are blocked at the OS level — they never reach the server and cannot display the certificate warning.

**Symptoms**: "Unable to connect" or "No Information Available" in Firefox, but Safari works fine.

**Fix — grant Local Network access to your browser:**

1. Open **System Settings** (Apple menu → System Settings)
2. Go to **Privacy & Security** → **Local Network**
3. Find your browser (Firefox, Chrome, etc.) in the list
4. Toggle the switch to **ON**
5. Quit and reopen the browser (Cmd+Q, then relaunch)

After this, navigate to `https://YOUR-LAN-IP:8000` — Firefox will now reach the server and display the self-signed certificate warning with the "Accept the Risk" option.

> Safari is typically granted local network access by default as a native Apple app, which is why it works without this step.

**Importing the certificate into Windows:**

Windows doesn't show a trust prompt for self-signed certs. Import the CA certificate into the trust store:

1. Navigate to `data\ssl\` in your GitInTheVan folder
2. Double-click **`ca.crt`** (this opens the Windows Certificate wizard)
3. Click **Install Certificate** → **Local Machine** → **Next**
4. Select **Place all certificates in the following store** → **Browse** → **Trusted Root Certification Authorities** → **OK**
5. **Next** → **Finish**
6. Restart your browser

Alternatively, from an admin command prompt:
```cmd
certutil -addstore -f "ROOT" "data\ssl\ca.crt"
```

> Install the **CA cert** (`ca.crt`), not the leaf cert. The CA signs the leaf, so trusting the CA trusts all leaf certs it generates.

**Importing the certificate into macOS Keychain:**

If a browser on macOS doesn't offer a cert bypass, add the certificate to the system trust store:

1. On the server machine, copy `data/ssl/cert.pem` to the macOS device
2. Double-click the file to open it in **Keychain Access**
3. Select the **login** keychain and click **Add**
4. Find the "GitInTheVan" certificate, right-click → **Get Info**
5. Expand **Trust** → set "When using this certificate" to **Always Trust**
6. Close and enter your macOS password to confirm
7. Restart the browser

**Using with JanitorAI from another device:**

1. Complete the certificate trust steps above
2. In JanitorAI settings, set the Reverse Proxy URL to `https://YOUR-LAN-IP:8000/v1/chat/completions`
3. Use your `gitv_` API key

**Disabling HTTPS:**

If you only need localhost access, remove or comment out `GITV_SSL_CERTFILE` and `GITV_SSL_KEYFILE` in your `.env` file and restart the server.

**Managing certificates via Admin panel:**

Go to **Admin** → **Network** tab to view certificate status, regenerate with additional IP addresses, or check if HTTPS is active.

## Configuration

### Environment Variables (`.env`)

| Variable | Default | Description |
|----------|---------|-------------|
| `GITV_SECRET_KEY` | `change-me` | JWT signing secret. Change in production. |
| `GITV_DEBUG` | `false` | Enable debug logging |
| `GITV_LOG_LEVEL` | `INFO` | Log level (DEBUG, INFO, WARNING, ERROR) |
| `GITV_DATABASE_URL` | `sqlite+aiosqlite:///./data/gitinthevan.db` | Database URL (SQLite, PostgreSQL, or MariaDB) |
| `GITV_DB_POOL_SIZE` | `10` | Connection pool size (PostgreSQL / MariaDB only) |
| `GITV_DB_MAX_OVERFLOW` | `20` | Max overflow connections beyond pool size (PostgreSQL / MariaDB only) |
| `GITV_DB_POOL_RECYCLE` | `3600` | Connection recycle interval in seconds (PostgreSQL / MariaDB only) |
| `GITV_DENO_PATH` | *(auto)* | Path to Deno binary for cantrip sandbox |
| `GITV_DEFAULT_ENDPOINT_URL` | *(empty)* | Fallback endpoint URL (used when no `gitv_` API key is provided) |
| `GITV_DEFAULT_ENDPOINT_API_KEY` | *(empty)* | Fallback endpoint API key |
| `GITV_DEFAULT_ENDPOINT_MODEL` | *(empty)* | Fallback model name |
| `GITV_DEFAULT_ENDPOINT_API_BASE_PATH` | *(empty)* | Fallback API base path (e.g. `/api` for OpenWebUI) |
| `GITV_REQUEST_TIMEOUT` | `300` | Request timeout in seconds |
| `GITV_CORS_ORIGINS` | `*` | Comma-separated allowed CORS origins |
| `GITV_RATE_LIMIT_ENABLED` | `true` | Enable/disable rate limiting |
| `GITV_RATE_LIMIT_PROXY_PER_MIN` | `60` | Max proxy requests per minute |
| `GITV_RATE_LIMIT_API_PER_MIN` | `120` | Max management API requests per minute |
| `GITV_MAX_REQUEST_BODY_SIZE` | `10485760` | Maximum request body size in bytes (10MB) |
| `GITV_JWT_EXPIRATION_HOURS` | `24` | JWT token expiration time |
| `GITV_MIN_PASSWORD_LENGTH` | `8` | Minimum password length |
| `GITV_LOG_FILE` | *(auto)* | Path to log file. If empty, auto-creates `data/logs/gitinthevan.log` |
| `GITV_LOG_MAX_SIZE_MB` | `1` | Max log file size in MB before rotation |
| `GITV_LOG_RETENTION_DAYS` | `30` | Days to retain rotated log files |
| `GITV_SSL_CERTFILE` | *(empty)* | Path to SSL certificate file. Set to enable HTTPS. |
| `GITV_SSL_KEYFILE` | *(empty)* | Path to SSL private key file. Set to enable HTTPS. |
| `GITV_GENERATE_CERTS` | `true` | Auto-generate self-signed certificates during install. Set to `false` when behind a reverse proxy or providing your own certs. |
| `GITV_BEHIND_PROXY` | `false` | Set to `true` when behind a reverse proxy. Enables `X-Forwarded-*` header processing. |
| `GITV_HTTP_REDIRECT_PORT` | `80` | Port for HTTP→HTTPS redirect server. Set to 0 to disable. Only active when SSL is enabled and not behind a proxy. |

### Deployment Modes

GitInTheVan supports four deployment scenarios depending on how you expose it to clients:

#### Local Network (Default)

Auto-generates self-signed certificates. Devices must trust the CA certificate once.

```
Device ──https──> GitInTheVan :8000
```

No configuration needed — this is the default behavior.

#### Behind a Reverse Proxy (Pangolin, Cloudflare Tunnel, Tailscale Funnel)

The external service handles TLS. GitInTheVan runs plain HTTP internally.

```env
GITV_GENERATE_CERTS=false
GITV_BEHIND_PROXY=true
```

```
JanitorAI ──https──> Tunnel/Proxy ──http──> GitInTheVan :8000
```

#### Direct Internet with Your Own Certs (Let's Encrypt)

GitInTheVan runs HTTPS directly with real certificates.

```env
GITV_GENERATE_CERTS=false
GITV_SSL_CERTFILE=/etc/letsencrypt/live/your-domain/fullchain.pem
GITV_SSL_KEYFILE=/etc/letsencrypt/live/your-domain/privkey.pem
```

#### Behind nginx/Caddy/Apache

The web server handles TLS and reverse proxies to GitInTheVan.

```env
GITV_GENERATE_CERTS=false
GITV_BEHIND_PROXY=true
```

Example Caddyfile (automatic HTTPS):
```
gitv.your-domain.com {
    reverse_proxy localhost:8000
}
```

Example nginx config:
```nginx
server {
    listen 443 ssl;
    server_name gitv.your-domain.com;

    ssl_certificate /etc/letsencrypt/live/your-domain/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/your-domain/privkey.pem;

    location / {
        proxy_pass http://localhost:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $remote_addr;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

### Endpoints

Endpoints support a custom **API Base Path** field. Most OpenAI-compatible APIs use `/v1` (the default). OpenWebUI and some other platforms use `/api` instead. You can paste the full URL (e.g. `https://example.com/api/chat/completions`) when creating an endpoint and the path will be auto-detected.

Each endpoint can have a **Default Model** set, which is used for diagnostics tests and as a fallback when the client doesn't specify a model. The diagnostics pane on the Dashboard can query available models from the provider (for endpoints using LiteLLM) or accept a manually typed model name via the override checkbox.

### Client Configuration

Point any OpenAI-compatible client at:

```
URL:  https://localhost:8000/v1/chat/completions  (same machine, HTTPS enabled)
URL:  https://YOUR-LAN-IP:8000/v1/chat/completions  (other devices on LAN)
Key:  gitv_<your-api-key>
```

For JanitorAI: set the Reverse Proxy URL to the above and use your `gitv_` key as the API key. See [HTTPS and LAN Access](#https-and-lan-access) if connecting from another device.

## Using with JanitorAI

1. Complete [HTTPS certificate trust](#https-and-lan-access) if connecting from another device
2. In JanitorAI settings, go to API configuration
3. Set API to "OpenAI" mode
4. Set the Reverse Proxy URL to your GitInTheVan address (e.g. `https://10.0.0.187:8000/v1/chat/completions`)
5. Set the API key to your `gitv_` key
6. Select your model

All requests will flow through GitInTheVan, applying any configured lorebooks, cantrips, and verification rules.

## Using with Wyvern Chat

Wyvern Chat reaches custom providers **from your browser** rather than through its server-side proxy, so GitInTheVan
must be served over **HTTPS** — see [HTTPS and LAN Access](#https-and-lan-access). An `http://` LAN address will be
blocked by the browser as mixed content and cannot be made to work from Wyvern's HTTPS-hosted app.

Follow these steps in order. Several are counter-intuitive; the order matters.

1. **Delete any existing GitInTheVan connections.** Do not edit them — see the note below.
2. **Create a new custom provider.**
3. **Leave "No API Key Required" OFF.** This is the counter-intuitive one. The key is supplied separately in step 6,
   not embedded here.
4. **Turn "Local Network / Browser-Only" ON.**
5. **Save the provider.**
6. **Click the "Needs key" badge** next to the provider name in the AI Connections list and enter your `gitv_` key.
7. **Click the provider**, then **Fetch Models**.
8. If your provider supports model listing, your models appear. If not, type a model ID manually, save, and test it in
   a chat.

Use your GitInTheVan HTTPS address as the API URL, for example `https://YOUR-LAN-IP:8000/v1`.

> **Note — Wyvern stale connection state (as of 2026-08-24)**
>
> Wyvern persists its browser-versus-proxy routing decision on the *connection* record, and does not recompute it when
> the connection's provider is edited. A connection that was ever bound to a Browser-Only provider keeps that
> behaviour afterwards, and the Edit Provider dialog gives no indication of it — the settings look correct while the
> connection behaves otherwise.
>
> The practical consequence is that **editing an existing connection may silently do nothing.** If a GitInTheVan
> connection misbehaves, delete it and build a new one rather than adjusting it in place. This affected the original
> diagnosis of this integration: the setup appeared broken when the settings simply were not taking effect.

## Activation Hierarchy

Every lorebook, cantrip, verification rule, map and memory rule is either *on* or
*off* for a given request. One set of rules decides that, for every resource type,
and they are fixed.

**1. Off is the default.** Nothing participates unless something turns it on. With
nothing turned on, GitInTheVan is a plain proxy that forwards your request
unchanged.

**2. Activation only ever adds. Nothing deactivates.** There is deliberately no way
to express "off" — only "not yet on". No source can countermand another, so a
resource you asked for cannot be silently suppressed by something else.

**3. The closer to the message, the more authority.** A `<#type-name#>` tag in your
system prompt, character persona or message text is the most direct statement of
intent you can make, so it **always** activates that resource — whatever its Active
setting says.

**4. The Active checkbox is the blanket switch.** It applies when no tag has spoken
for that resource. **Active means "on for every request", not "available".**

### What that means in practice

| Active | Tag set | Tag in your message | Runs? |
|---|---|---|---|
| off | — | — | No |
| off | `dice` | — | No |
| off | `dice` | `<#cantrip-dice#>` | **Yes** — rule 3 |
| on | — | — | **Yes** — rule 4 |
| on | `dice` | — | **Yes** — rule 4; a tag never restricts |
| on | `dice` | `<#cantrip-dice#>` | **Yes** |

**To make something tag-only, turn Active off.** That is the whole mechanism: Active
off plus a tag means "run this only when I ask for it by name".

### Tag format

```
<#type-name#>              e.g. <#cantrip-dice#>, <#lore-ashfall#>
<#owner-type-name#>        another user's public resource
<#taggroup-name#>          activates every member of a group
```

Recognised types: `lore`, `cantrip`, `verify`, `map`, `memory-rule`, `taggroup`.
Tags are stripped from your message before it reaches the LLM.

### Maps and memory rules are chosen, not stacked

Only one map and one memory rule can apply to a request, so for these two the
Active checkbox does **not** mean "apply to everything" — a map is a multi-stage
pipeline, and running one on every message would quietly multiply what each message
costs you. They obey rules 1 to 3 exactly as everything else does, but their blanket
source is a single explicit choice instead:

- **Maps** — activated by `<#map-name#>`, or by the default map in your Settings.
- **Memory rules** — the first rule whose tag you used, otherwise your untagged
  default rule.

### Command tags are separate

`<VERIFY:off>` and friends switch *pipeline features* on and off for a request. They
are the one place where turning something **off** is possible, and they are
deliberately a separate system from resource activation. See
[Command Tags](#Command-Tags).

## Cantrips (JavaScript Lorebooks)

Cantrips are sandboxed JavaScript snippets that run in a Deno subprocess with no network, filesystem, or environment access. They are compatible with existing JanitorAI scripts.

### JanitorAI Context API

```javascript
const lastMessage = context.chat.last_message;
const messageCount = context.chat.message_count;
const charName = context.character.name;

// Modify character context (append-only recommended)
context.character.scenario += " Additional world context.";
context.character.personality += ", additional trait";
```

### GitInTheVan Extensions

```javascript
// Per-chat persistent storage (survives across cycles)
const day = context.chat_data.get('day') || 1;
context.chat_data.set('day', day + 1);

// Per-user global storage (shared across all chats and cantrips)
const theme = context.user_data.get('theme') || 'default';
context.user_data.set('theme', 'dark');

// Per-cantrip storage (persists across chats, isolated to this cantrip)
const level = context.cantrip_data.get('level') || 1;
context.cantrip_data.set('level', level + 1);

// Persistent memory (LLM-managed key/value store, per-conversation)
const location = context.memory.get('location');
context.memory.set('weather', 'stormy');
const allKeys = context.memory.keys();

// Context budget (when budgeting is enabled in Settings)
const budget = context.budget;  // {total, remaining, weight, share, detail_level}
if (budget.detail_level === 'bullets') {
    context.character.scenario += '- Brief notes only';
} else {
    context.character.scenario += 'Full detailed description...';
}

console.log('Debug output visible in cantrip tester');
```

### Pipeline Positions

Cantrips run at four configurable positions in the pipeline:

| Position | When | Context Available |
|----------|------|-------------------|
| **Pre-Driver** | Before the writing LLM | `context.character`, `context.chat_data` |
| **Driver-Callable** | LLM invokes via `<call:tool_name>` | `context.tool_call`, `context.tool_result`, `context.chat_data` |
| **Pre-Navigator** | After Driver responds, before verification | `context.response.content`, `context.chat_data` |
| **Post-Navigator** | After verification completes | `context.response.content`, `context.chat_data` |

### Driver-Callable Tools

Cantrips with the Driver-Callable position can be invoked by the writing LLM during generation:

1. A `[TOOL ACCESS]` block listing available tools is injected into the system prompt
2. The LLM responds with `<call:tool_name arg="value">` to invoke a tool
3. The cantrip executes, reads `context.tool_call.name` / `.args`, and writes `context.tool_result`
4. The result is returned as a `[TOOL RESULT]` message and turns are decremented
5. When turns reach 0, the notification is no longer injected — preventing infinite loops

Configure the maximum tool-call turns per request in **Settings > Driver-Callable Tools**.

### Testing Cantrips

Use the Cantrip Tester in the web UI (Cantrips page) to run a cantrip against sample context without forwarding to an LLM.

## Verification

Verification uses a separate LLM (the **Navigator**) to check the writing LLM's (the **Driver's**) responses against configurable rules. GitInTheVan uses van-themed terminology: **Driver** is the primary writing LLM, **Navigator** is the verification LLM.

1. Driver produces a response
2. The response is sent to the Navigator with your rule prompt
3. If the response violates the rule, the request is resubmitted with corrective instructions
4. Retries are limited (configurable per rule, default 2)

**Note:** When verification is enabled, responses are buffered (non-streaming) to allow checking before returning to the client.

## Persistent Memory

GitInTheVan provides database-backed persistent memory — no zero-width character encoding, no reliance on the LLM to preserve hidden data.

### How It Works

1. The LLM includes `<memstore>` tags in its response to save memories:
   ```
   <memstore key="location">Dragon's Breath Tavern</memstore>
   <memstore key="time">evening</memstore>
   ```
2. GitInTheVan extracts these tags, stores them in the database per-conversation, and **strips them** from the response before it reaches the user
3. On the next request, all stored memories for that conversation are injected as a system context block:
   ```
   [PERSISTENT MEMORY]
   location: Dragon's Breath Tavern
   time: evening
   [/PERSISTENT MEMORY]
   ```
4. The system uses **rolling hash conversation tracking** to identify which conversation a request belongs to. The hash is edit-tolerant: it excludes the current user message and the preceding assistant message, so editing or swiping the LLM's last response does not break the conversation chain or orphan its memories

Memories are managed via the **Memories** page in the web UI (view, edit, delete per conversation).

### Cantrip Access

Cantrips can also access persistent memory via `context.chat_data` — a per-chat key/value store that survives across conversation cycles:

```javascript
const day = context.chat_data.get('day') || 1;
context.chat_data.set('day', day + 1);
```

## Conversation Summarization

When conversations grow long, summarization automatically compresses older history to reduce token usage while preserving narrative context.

### How It Works

1. GitInTheVan estimates the token count of the request messages (~4 chars per token heuristic)
2. If the count exceeds the configurable **token threshold**, the older dialogue messages are sent to a user-selected LLM for summarization
3. The summarized messages are **removed** from the request and replaced with a single `[CONVERSATION SUMMARY]` system block
4. The most recent messages (configurable, minimum 3) are always forwarded verbatim — the summary and recent context never overlap
5. Summaries are cached per conversation using a boundary hash. Rerolls and forks reuse the cached summary without re-calling the summarization LLM. Continuations build on the prior summary (rolling pattern)
6. System messages (persona, lorebook constant entries, cantrip scenario additions) are preserved — only user/assistant dialogue is summarized

### Configuration

Configure in **Settings > Conversation Summarization**:

| Setting | Default | Description |
|---------|---------|-------------|
| Enabled | Off | Master toggle |
| Endpoint | *(default)* | Which LLM endpoint to use for summarization (can be a cheaper/faster model) |
| Model Override | *(blank)* | Override the model name for summarization calls |
| Token Threshold | 8000 | When estimated tokens exceed this, summarization triggers |
| Keep Recent Messages | 6 | Number of recent messages always sent verbatim (minimum 3) |
| Summarization Prompt | *(default)* | System prompt for the summarization LLM |

Summaries can be viewed and managed on the **Memories** page.

## Context Budgeting

Context Budgeting allocates a percentage of the model's context window for injected content (cantrips, lorebooks, memory). This prevents injected content from consuming too much of the context window and gives cantrips the information they need to scale their output dynamically.

### How It Works

1. Before Pre-Driver cantrips run, GitInTheVan calculates the total context window (auto-detected from model name or manually overridden) and the current token usage
2. A configurable percentage (default 10%) of the context window is allocated as the "injection budget"
3. The budget is divided across active cantrips and lorebooks proportionally by their **budget weight** (default 1.0 each)
4. Each cantrip receives a `context.budget` object with its share and a suggested detail level

### Cantrip Budget API

```javascript
const budget = context.budget;
// budget = {
//   total: 819,           // total injection budget in tokens
//   remaining: 655,       // tokens remaining after prior cantrips
//   weight: 2.0,          // this cantrip's configured weight
//   share: 327,           // tokens allocated to this cantrip
//   detail_level: "full"  // "full" | "summary" | "bullets"
// }
```

Detail levels are determined by the share size:
- **Full** (≥4000 tokens): Enough for detailed content
- **Summary** (≥1500 tokens): Condensed but complete
- **Bullets** (<1500 tokens): Minimal bullet-point format

### Configuration

Configure in **Settings > Context Budgeting**:

| Setting | Default | Description |
|---------|---------|-------------|
| Injection Budget (%) | 10 | Percentage of context window reserved for injections. Set to 0 to disable. |
| Context Window Override | 0 | Override the model's context window size. 0 = auto-detect from model name. |

Per-cantrip and per-lorebook **Budget Weight** fields control proportional allocation. A weight of 2.0 gets twice the tokens of a weight of 1.0.

## Memory Rules

Memory Rules allow overriding summarization behavior per conversation. Rules are taggable and follow the same pattern as verification rules.

### How It Works

1. Rules are evaluated in execution order when summarization is about to trigger
2. Tagged rules activate via `<#memory-rule-tag#>` in persona or message text
3. The first matching rule (tagged > untagged default) determines the summarization behavior
4. If no rules match, global settings apply

### Rule Fields

| Field | Description |
|-------|-------------|
| Name | Human-readable label |
| Summarization Enabled | Whether to summarize at all for matching conversations |
| Token Threshold | Override the global threshold (0 = use global) |
| Keep Recent | Override the global keep_recent (0 = use global) |
| Custom Prompt | Override the summarization prompt (empty = use global) |
| Tag | Activation tag for `<#memory-rule-tag#>` matching |

### Example Use Cases

- **Slow-burn RP**: More aggressive summarization (lower threshold, fewer recent messages) for political/intrigue roleplay with long histories
- **Casual chat**: Disable summarization entirely for short conversations that don't need compression
- **Custom prompt per character**: Different summarization focus (e.g., track relationship status vs. plot events)

Memory Rules are managed on the **Memories** page.

## Maps

Maps are workflow presets that chain multiple LLM stages into a single request. Each stage can have its own lorebooks, cantrips, endpoint, model, driver-callable turns, and verification, enabling multi-pass pipelines like Writing LLM > Gamemaster LLM > Narrator LLM.

### How It Works

1. A map is activated via a `<#map-tag#>` tag in persona or message text (one map per request, first match wins)
2. The standard single-stage pipeline runs when no map is active
3. Each stage executes in order: inject stage lorebooks and system instructions, run pre-driver cantrips, forward to the stage's LLM (with optional driver-callable tool loop), run post-driver cantrips, and optionally verify with the Navigator
4. Between stages, the output mode determines how the response feeds forward:
   - **Persist**: response becomes an assistant message for the next stage (default)
   - **Sanitize**: response wrapped in a `[STAGE N OUTPUT]` system block
   - **Discard**: response dropped (only used for verification within its own stage)
5. The final stage's response becomes the HTTP response

### Stage Routing

Each stage picks its endpoint in this order:

1. **Endpoint Tag** — every enabled endpoint whose Role Tag matches, in priority order, as a failover chain
2. **Endpoint** — a specific endpoint pinned to the stage
3. The user's default endpoint
4. The first enabled endpoint

Tagging is what makes a map portable. A stage that asks for a `planner` endpoint
means the same thing on someone else's install, where a pinned endpoint ID or a
`model_override` string does not — the importer points their own tags at whatever
models they run. Tags also give each stage failover for free.

### Resource Attachments

Lorebooks, cantrips, skills and samples attach to specific stages. Each attachment has a **Sticky** option:
- **Sticky**: the injection persists through all subsequent stages
- **Stage-only** (default): the injection applies to its own stage and no other

A cantrip attached to a stage runs **only on that stage**. Cantrips attached to no stage run once in the global pass before the first stage, as they do outside a map. Nothing runs twice.

`context.chat_data` and `context.memory` are shared across all stages, so state written in an early stage is readable later.

### Forbidden Words in a Map

Each stage's output is scanned against your Forbidden Words list, and any hits are passed to the **next** stage as a `[FORBIDDEN WORDS FLAGGED IN STAGE N OUTPUT]` block naming the phrases found.

Earlier stages are never shown the list. A model spending attention avoiding banned words writes stilted prose, so a writing stage is left to follow its prompt and a later editing stage is given the specific phrases to replace. A hit introduced by the final stage is logged — nothing downstream can act on it, so put your editing stage last.

### Import/Export

Maps export as a single JSON file containing all stages (including their endpoint tags), embedded resource contents, and configuration. Two export modes:

- **Embedded** (default): the full content of every attached resource travels with the map, so the file installs on a machine with nothing linked.
- **Linked**: resources whose origin is known are written as a reference instead of a copy, so the map tracks upstream and you maintain each cantrip in one place. Falls back to embedding anything it cannot reference.

Either way each resource carries a `source` block when its origin is known — the repo it came from and its path — which is what lets an import recognize resources you already have.

Four resource-handling modes on import:
- **Smart** (default): reuse by origin, then by content hash, else create a copy
- **Keep Both**: always create new copies
- **Reuse Existing**: link to same-named resources you already have
- **Overwrite**: update same-named resources

### Resource Identity and Deduplication

Install a dozen maps that all use the same dice cantrip and you get **one** dice cantrip. Two keys make that work without any central registry, so a self-hosted install is as capable as any other:

| Key | What it is |
|---|---|
| **Origin** | `(normalized repo URL, resource path)`. `git@github.com:x/y.git`, `https://github.com/x/y/` and `https://github.com/x/y.git` all reduce to the same string; a local repo reduces to an absolute path. |
| **Content hash** | sha256 over the fields that decide whether two resources are the same thing. Catches copies with no origin — hand-made ones, and ones imported from a file. |

A resource that a pack ships only *inside* a map is addressed through it: `maps/pipeline.json:Dice Controller`. That gives it a stable origin, and lets one map link a resource that lives in another.

**An origin match never overwrites.** If a map declares a trusted origin but ships a different payload, GitInTheVan links your installed copy, leaves it untouched, and reports the mismatch. A map cannot swap out code you have already vetted by claiming a trusted path. A repo you have not linked is never cloned because a map asked for it — the install reports what it needs and you decide.

Installing a map creates an entry for each object it carries, so the Installed list shows per-object scan results, and a resource is removed only when the last install that references it goes.

Maps are managed on the **Maps** page and integrate with content packs (`maps/` folder auto-discovered in git repos). Installing a map from a pack creates it inactive along with its resources. Each object is scanned independently through the same path a direct install uses, so a finding names the cantrip it came from rather than only the map.

## Debug Mode

Debug Mode captures each request through the proxy as an expandable timeline, with
every transformation step, the objects that acted on it, and what the run cost in
tokens and time.

### What It Captures

Each captured run shows a **Pipeline Timeline** covering:

- **Request-side stages**: memory injection, scenario summarization (pre/post),
  lorebook injection, skills injection, budget preparation, cantrip processing,
  conversation summarization, writing samples, driver-callable tools, prefill
  normalization, bypass encoding
- **Response-side stages**: pre-Navigator cantrips, forbidden words scan,
  verification checks, post-Navigator cantrips, memory extraction, bypass decoding
- **Map stages**: each stage's resolved endpoint, model override, output mode,
  attached resources, output, reasoning and verification result
- **Cantrips**: every cantrip that ran, with its code, a content hash of that code,
  duration, the fields it changed, its `console.log` output and any error. Cantrips
  that were considered and did **not** run are recorded too, with the reason
- **Objects, not counts**: which lorebook entries matched and which lorebook each
  came from, which skills and samples were injected, and what each cost in tokens.
  Every one links out to its editor in a new tab
- **Reasoning** in full, for models that return `reasoning_content`

Streaming and non-streaming requests are both captured.

### Metrics

Every run reports:

| Metric | Meaning |
|---|---|
| Tokens | Prompt plus completion across every upstream call |
| Injected tokens | What the pipeline added to the prompt: lorebooks, skills, memory, samples |
| Total time | Wall clock for the whole request |
| **Pipeline overhead** | Total time minus time waiting on upstreams — what GitInTheVan itself cost |
| Tokens/second | Completion tokens divided by upstream time |
| LLM calls | Upstream requests, including map stages, verification judges and failover retries |

Token counts come from the endpoint's `usage` response when it sends one. When it
does not, they are estimated from content length and the run is labelled
**estimated**; a run whose calls disagree is labelled **mixed**. The label is always
shown, because comparing an estimate against a measurement without saying so would
be misleading.

### Using Debug Mode

1. Enable **Debug Mode** in **Settings > Proxy Configuration**
2. Send requests through the proxy as normal
3. Open the **Debug** tab under **Dashboard**
4. Select a run to view its timeline, metrics and objects
5. Click any stage to expand it

Runs are pruned to the most recent 20 per user. **Save** a run to pin it: saved runs
are exempt from pruning and from **Clear All**, so a comparison baseline is not lost
to later traffic. The number of saved runs per user is an admin setting (default 10).

Runs from prior versions are auto-migrated and remain viewable; they simply carry no
metrics, since none were recorded at the time.

### Comparing Runs

**Debug > Compare runs** puts up to four runs side by side.

- Pick two to four runs, then choose a **baseline** with the radio at the top of any
  column. The baseline moves to the first column and every diff recomputes against
  it — no re-running, so you can change your reference point freely while iterating.
- Non-LLM components are diffed directly: cantrips added, removed, code-changed,
  output-changed or trigger-changed; lorebook entry, skill and sample sets with token
  deltas; pipeline stages present in only one run; verification pass/fail, retries and
  individual violations; and any difference in model, endpoint or provider.
- Metrics show per-column deltas against the baseline, coloured by direction.
- Response and reasoning are diffed as text, by word for short output and by line for
  long output or code.
- Anything a run touched links out to its editor **in a new tab**, so you keep the
  comparison open while you go and change the thing it showed you.
- The selection and baseline live in the URL, so a comparison can be reloaded or
  shared.

### Replay

**Replay** re-sends a run's original messages through your *current* configuration
and opens the result beside the original. This is what makes A/B iteration cheap:
change a cantrip, replay, compare — without returning to your chat client.

Replay uses a separate conversation id, so summaries, conversation hashes and memory
extraction cannot touch the real conversation, and memory writes are skipped.

> **Cantrip side effects are not sandboxed.** A cantrip that writes `user_data` or
> `cantrip_data`, rolls dice, or advances a counter will do so again on every replay.

### Sandbox Copies

**Create sandbox copy** forks a run into something you can fire from inside
GitInTheVan, as many times as you like.

It copies the request *and* the conversation state that request read — memories,
chat data, the summary — into a conversation of its own. Running the sandbox
evolves that copy, so the second run sees what the first one wrote, exactly as a
real conversation would. The chat it was forked from is never touched.

- **Run** fires it and opens the resulting debug run.
- **Edit prompt** changes what it sends, so you can vary the input as well as the
  configuration.
- **Reset** discards everything the runs accumulated and re-copies from the
  original.
- **Delete** removes the sandbox and its state. The original is untouched.

Cantrip side effects are *not* suppressed, and that is deliberate: a dice roll or
a balance change should happen, and it lands in the fork where you can inspect it
and re-run without consequence.

**Nothing is sent back to the service the request came from.** A sandbox run
starts here and its response is displayed here. JanitorAI, Wyvern or whatever
client produced the original request is not contacted and is not expecting to be
— the only outbound call is to your own configured LLM endpoint, exactly as with
any other request.

#### Sandbox or replay?

| | Replay | Sandbox |
|---|---|---|
| Runs | once | as often as you like |
| Conversation state | read-only, thrown away | forked, and it evolves |
| Prompt | fixed | editable |
| Use it for | did my change alter this exact output? | iterating on a scenario |

### Exporting

A single run or a whole comparison can be exported as **JSON** or **Markdown**. JSON
is lossless — the complete trace, including reasoning and cantrip code — so it can be
kept, diffed offline or attached to a bug report. Markdown is a readable report.

If a run exceeds the admin size limit, its per-stage message snapshots are dropped
first and the run is explicitly **marked as truncated** with the reason. Reasoning,
cantrip output and metrics are kept. Nothing is trimmed silently.


## Command Tags

Command tags are inline directives that override pipeline behavior for a single message or persistently for a conversation. They are placed in the user's message text and are automatically stripped before the request reaches the LLM.

### Syntax

```
<COMMAND:setting>          One-off (this request only)
<COMMAND:setting:persist>  Persistent (saved to conversation memory)
<COMMAND:reset>            Clears persistent override for this command
```

### Available Commands

| Command | Controls | Examples |
|---------|----------|----------|
| `VERIFY` | Verification (Navigator) | `<VERIFY:off>` skip for one message, `<VERIFY:off:persist>` skip until reset |
| `SUMMARY` | Conversation summarization | `<SUMMARY:off>` skip compression for one message |
| `FORBIDDEN` | Forbidden words scanner | `<FORBIDDEN:off>` disable forbidden word check |
| `MEMORY` | Memory injection + extraction | `<MEMORY:off>` skip memory for one message |
| `DRIVER` | Driver-callable tools | `<DRIVER:off>` disable tool access for one message |

### Precedence

Three tiers, highest to lowest:

1. **One-off** — applies to the current request only, then reverts
2. **Persistent** — saved to conversation memory, applies to all subsequent messages until `<CMD:reset>`
3. **GUI setting** — the default configured in the web UI (used when no override exists)

A one-off always overrides a persistent override for that request. The persistent override resumes on the next message.

### Persistence and Reset

```
User message 1: "Fight the dragon <VERIFY:off:persist>"    -> verification off, saved
User message 2: "Continue"                                 -> verification still off (persistent)
User message 3: "Keep going <VERIFY:on>"                   -> verification on (one-off overrides persistent)
User message 4: "More"                                     -> verification off again (persistent resumes)
User message 5: "Done <VERIFY:reset>"                      -> persistent cleared, GUI setting resumes
```

Persistent overrides are scoped per-conversation (tracked via rolling hash). Different chats have independent override state.

## Development

### Project Structure

```
GitInTheVan/
  app/
    models/       SQLAlchemy ORM models
    routers/      API route handlers
    services/     Business logic (proxy, cantrips, lorebooks, verification)
    main.py       FastAPI entry point
  frontend/
    src/          Svelte 5 source
  tests/          pytest test suite
  testing/        Cross-platform test harness (see testing/README.md)
  requirements/   Hash-pinned dependency lockfiles
  static/         Built frontend (generated)
```

### Building the Frontend

After any UI change:

```bash
cd frontend
npm run build
```

This outputs to `../static/`. Restart the Python server to pick up changes.

### Running Tests

```bash
.venv\Scripts\python -m pytest tests\ -v
```

Contributors need the development dependencies, which the deploy scripts install
only when asked:

```bash
scripts\deploy-windows.bat --dev
```

Without `--dev` you get `requirements/main.txt` — the end-user tree, which has no
pytest, ruff or pip-audit. Nothing in `app/` imports them, so a plain install is
complete for running the server.

### Dependency Lockfiles

Direct dependencies are pinned in `pyproject.toml`, but that does not constrain
what they pull in. `requirements/` holds hash-pinned lockfiles covering the whole
transitive tree, installed with `--require-hashes` so pip refuses any artifact
whose hash does not match:

| File | Used by |
|------|---------|
| `main.txt` | deploy/update scripts (end users) |
| `dev.txt` | deploy/update scripts with `--dev` (contributors) |
| `docker.txt` | `Dockerfile` |

Regenerate all three after changing `pyproject.toml` dependencies.
`tests/test_dependency_pinning.py` fails if a lockfile and `pyproject.toml`
disagree.

```bash
uv pip compile pyproject.toml --universal --generate-hashes --python-version 3.12 -o requirements/main.txt
uv pip compile pyproject.toml --extra dev --universal --generate-hashes --python-version 3.12 -o requirements/dev.txt
uv pip compile pyproject.toml --extra postgres --extra mysql --universal --generate-hashes --python-version 3.12 -o requirements/docker.txt
```

`--universal` is required: it emits environment markers so one lockfile is valid
on Windows, macOS and Linux. Without it, a lockfile generated on Windows silently
omits `uvloop` and every Linux/macOS install loses it. Each lockfile also records
its own generating command in its header comment.

### Cross-Platform Testing

`testing/` holds a harness that provisions a throwaway install on a remote
machine, runs the test suite against it, archives the logs, and deletes itself:

```bash
remote-test.bat -env .\testing\harness.env -target linux -branch main all
```

Targets are `macos`, `linux`, `docker`, and `windows`. It runs the real deploy
scripts rather than reimplementing them, so it verifies the actual end-user
install path. See [testing/README.md](./testing/README.md).

### Linting

```bash
.venv\Scripts\ruff check app\ tests\
```

## Starting Out With Cantrips

I created a ton of 'Scripts' for JanitorAI before they started purging long-time creators due to fresh new interpretations of old rules. You can find them at [Tydorius/JanitorAI_Scripts](https://github.com/Tydorius/JanitorAI_Scripts). There is also a skill file for creating your own.

Note that they use different mechanisms for persistent memory, essentially passing data into the prompt. They are heavily reliant on the LLM to follow instructions, moreso than GitInTheVan will rely on them. They also expect no post-LLM Scripts, so the Scripts are written expecting User > Script > LLM > User. Cantrips will support User > Script > LLM > Verification LLM > Script > User.

## Getting Support

Feel free to open issues for feature requests or bug reports. For bug reports, the more information you can provide the better.

## Giving Suport

If you like what I do, you can donate to me on ko-fi.

[ko-fi.com/tydorius](https://ko-fi.com/tydorius)

## Disclaimer on Use of AI

I am a cloud architect. I design and build systems for a living. I've been programming and scripting for thirty years, and I've been doing graphic design for nearly as long starting in Photoshop 6.0.

I utilized GLM 5.x for parts of this project. I utilized Gemini 3.5 to generate a draft logo (before remaking it in Inkspcape). This is an open source project that is free for personal use, and with my schedule it was only possible through the assistance of artificial intelligence.

I have no qualms about using AI in my work.

AI is not the enemy. Corporations are the enemy. One person not hiring an artist is not killing the art industry, it's corporations that are training models off stolen data and firing entire art departments en masse that are hurting the art industry. The same goes for all affected industries from authors to programmers. The same was true when robots became a core part of manufacturing.

Corporations are the ones that laid off workers without benefits. Corporations are the ones lobbying against rights. Corporations are the ones that keep us in the quagmire of supply and demand, late-stage capitalism, and existential suffering. We have the technology to have clean air, clean water, plentiful food, and plentiful shelter. We could be in a post-scarcity society right now, but greed holds us back.

I make use of the tools I have available.

## License

Mozilla Public License 2.0 — See [LICENSE](LICENSE)
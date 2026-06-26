# GitInTheVan

<img src="frontend/public/gitinthevan-full.svg" alt="GitInTheVan" width="400">

A self-hostable man-in-the-middle LLM router/proxy for roleplay and creative writing. It intercepts OpenAI-compatible chat completion requests, applies transformations (lorebooks, cantrips, verification), and forwards them to your configured LLM endpoint.

Licensed under Mozilla Public License 2.0.

## Contents

[Why It Exists](#Why-It-Exists)  
[Features](#Features)  
[Upcoming Features](#Upcoming-Features)  
[Quick Start](#Quick-Start)  
[Configuration](#Configuration)  
[Database Support](#Database-Support)  
[Using with JanitorAI](#Using-with-JanitorAI)  
[Cantrips](#Cantrips)  
[Verification](#Verification)  
[Persistent Memory](#Persistent-Memory)  
[Conversation Summarization](#Conversation-Summarization)  
[Context Budgeting](#Context-Budgeting)  
[Memory Rules](#Memory-Rules)  
[Debug Mode](#Debug-Mode)  
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
- **Tagging System** — Activate lorebooks, cantrips, and verification rules via `<#type-name#>` delimiters in persona or message text. Tags are auto-stripped before forwarding to the LLM
- **Content Discovery and Sync** — Link any git repository as a content pack. Browse, install (linked with update tracking), or fork (independent copy) cantrips, lorebooks, and rules. Safety scanner checks for malicious code. Auto-discovers resources from folder structure. "Download at your own risk" disclaimer
- **Diagnostics** — Automated endpoint and configuration checker for troubleshooting connectivity issues
- **Security Hardening** — Rate limiting (proxy + management API), configurable CORS origins, password strength validation, request body size limits, audit logging for admin actions, configurable JWT expiration, global caps for driver-callable turns and verification retries
- **Per-Endpoint API Keys** — Create multiple `gitv_` API keys per user, each mapped to a specific endpoint for multi-platform routing. Managed on each endpoint card in the UI
- **Admin Panel** — Global caps (turns/retries use min of user/global), Users tab (create, edit, disable, delete, password reset, key regeneration), Debug tab (pipeline capture viewer), read-only audit logs, read-only server logs with runtime log level override without restart. All in a single Admin page with five tabs
- **Maps** — Multi-stage LLM pipelines that chain multiple Driver passes (e.g., Writing LLM > Gamemaster LLM > Narrator LLM) into a single request. Each stage has its own lorebooks, cantrips, endpoint, model, driver-callable turns, and verification. Output modes (persist/sanitize/discard) control how stage output feeds the next stage. Sticky vs stage-only resource attachments. Activated via `<#map-tag#>` tags. Export/import as self-contained JSON with resource dedup options (keep_both/reuse/overwrite)
- **Web UI** — Full management interface built with Svelte 5 including cantrip tester, verification tester, forbidden word scanner, code editor with syntax highlighting, jump-to-top/bottom navigation, and log viewer
- **Multi-Database Support** — SQLite (default), PostgreSQL, and MariaDB/MySQL backends. SQLite for single-instance self-hosting; PostgreSQL or MariaDB for multi-instance horizontal scaling with a shared database server
- **LiteLLM Provider Compatibility** — Optional provider selection on endpoints enables LiteLLM integration for automatic parameter translation, auth format handling, and response normalization across 100+ LLM providers (Gemini, OpenAI, Anthropic, OpenRouter, DeepSeek, xAI, and more). Endpoints without a provider set use raw HTTP passthrough (backward compatible)
- **Context Budgeting** — Weighted token budget allocation across cantrips and lorebooks. Cantrips access their share via `context.budget` and can dynamically scale output detail (full/summary/bullets) based on remaining tokens. Configurable per-user budget percentage and context window override
- **Memory Rules** — Taggable per-conversation summarization overrides. Rules can override the token threshold, keep-recent count, prompt, or disable summarization entirely for specific conversations. Activate via `<#memory-rule-tag#>` tags
- **Debug Mode** — Captures the last 20 pipeline exchanges with full visibility (original messages, modified messages after pipeline processing, Driver response, and verification results). Dedicated Debug page for troubleshooting pipeline behavior

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

> **Note:** PostgreSQL and MariaDB support is implemented and unit-tested but has **not yet been tested against live database servers**. SQLite is the battle-tested default. Docker Compose configurations for all three backends are planned as part of the Docker distribution.

## Upcoming Features

The following are planned for future releases.

- **Per-server sharing** — Share resources among users on the same GitInTheVan instance via public flags
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

### HTTPS and LAN Access

The deploy scripts automatically generate a self-signed SSL certificate so that GitInTheVan can be accessed from other devices on your local network. This is required because browsers block HTTP requests from HTTPS sites (like JanitorAI) — a restriction called *mixed content blocking*.

**Trusting the certificate on each device:**

On every device/browser that will connect to GitInTheVan:

1. Open your GitInTheVan URL directly in the browser address bar (e.g. `https://10.0.0.187:8000`)
2. You'll see a security warning about the self-signed certificate
3. Click **Advanced** → **Accept the Risk and Continue** (Firefox) or **Proceed to site (unsafe)** (Chrome)
4. The GitInTheVan login page will load — the certificate is now trusted for future requests

> **Why this is necessary**: Browsers silently block background requests (like JanitorAI's API calls) to servers with untrusted certificates. Unlike direct navigation, there is no warning dialog — the request simply fails. You must accept the certificate via direct navigation first.

> **"Unable to connect" instead of a cert warning?** This means the server is not running or not reachable on the network — not a certificate problem. Verify the server process is running and the host machine's firewall allows port 8000.

**Platform-specific notes:**

- **Firefox (all platforms)**: Navigate to the URL, click **Advanced** → **Accept the Risk and Continue**. If no warning page appears, go to `about:preferences#privacy` → scroll to **Certificates** → **View Certificates** → **Servers** tab → **Add Exception**, enter the URL, and confirm.
- **Chrome/Edge (desktop)**: Click anywhere on the warning page and type `thisisunsafe` (no spaces) to bypass. Alternatively, go to `chrome://flags/#allow-insecure-localhost` and enable it (localhost only).
- **Safari (macOS/iOS)**: Safari does not offer a self-signed cert bypass for non-localhost addresses. You must import the certificate into **Keychain Access** (macOS) or install a profile (iOS). See below.
- **Firefox on Android**: Works via the standard warning page → **Accept the Risk**.

**Importing the certificate into macOS Keychain (Safari/Chrome):**

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

### Endpoints

Endpoints support a custom **API Base Path** field. Most OpenAI-compatible APIs use `/v1` (the default). OpenWebUI and some other platforms use `/api` instead. You can paste the full URL (e.g. `https://example.com/api/chat/completions`) when creating an endpoint and the path will be auto-detected.

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

### Resource Attachments

Lorebooks and cantrips attach to specific stages. Each attachment has a **Sticky** option:
- **Sticky**: the injection persists through all subsequent stages
- **Stage-only** (default): the injection is stripped after this stage completes

`context.chat_data` and `context.memory` are shared across all stages, so state written in an early stage is readable later.

### Import/Export

Maps export as a single JSON file containing all stages, embedded resource contents, and configuration. Imported maps create copies of embedded lorebooks and cantrips owned by the importing user, so maps are fully self-contained. Three resource-handling modes on import:
- **Keep Both**: always create new copies
- **Reuse Existing**: link to same-named resources you already have
- **Overwrite**: update same-named resources

Maps are managed on the **Maps** page and integrate with content packs (`maps/` folder auto-discovered in git repos).

## Debug Mode

Debug Mode captures pipeline data for the last 20 exchanges, providing full visibility into how GitInTheVan transforms requests and responses.

### What It Captures

Each captured exchange includes:
- **Original Messages**: The request as received from the client (before any pipeline processing)
- **Modified Messages**: The request as sent to the Driver (after lorebooks, cantrips, memory, budget, summarization)
- **Budget Data**: Token budget calculations and allocations
- **Tags**: Any activation tags detected
- **Driver Response**: The raw response from the writing LLM
- **Verification Results**: Check history with violations and retry details

### Using Debug Mode

1. Enable **Debug Mode** in **Settings > Context Budgeting**
2. Send requests through the proxy as normal
3. Open the **Debug** page in the sidebar
4. Select an exchange from the list to view the pipeline stages
5. Toggle between **Original**, **Modified**, and **Response** views

Debug exchanges are automatically pruned to the most recent 20 per user. Use **Clear All** to wipe all captured data.

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
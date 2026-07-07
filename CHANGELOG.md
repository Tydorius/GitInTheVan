# Changelog

All notable changes to GitInTheVan are documented in this file.

## [0.15.0] - 2026-07-07

### Added

- **Tags and Groups**: Centralized tag management and group collections for multi-resource activation
  - **Groups tab**: Create named collections of lorebooks and cantrips activated by a single `<#grouptag#>` tag
  - Groups can be blanket-active (applied every message) or tag-activated (activated when group tag appears in messages)
  - Groups are always private to the owner, activate in pre-LLM phase, and cannot nest
  - Missing members are silently skipped with a console warning
  - Deduplication: resources called multiple times in the same stage only activate once
  - **Tags tab**: Centralized view of all lorebook/cantrip tags with inline editing and public/private toggle
  - `tag_groups` + `tag_group_members` tables (migration 035)
  - API: `/api/tag-groups` CRUD + `/api/tag-groups/{id}/members`
  - Debug capture for tag group resolution stage (rule 16)
  - 16 tests covering API CRUD, group resolution, pipeline integration, deduplication, missing members

- **End-to-End Debug Mode**: Full pipeline stage tracking with timeline UI
  - Stage-based capture system: every pipeline step records before/after message snapshots, metadata, and settings
  - 16 capture points: memory injection, scenario summarization (pre/post), lorebook injection, skills, budget preparation, cantrip processing, conversation summarization, writing samples, driver-callable, prefill, bypass encoding, final messages, verification, bypass decoding, LLM response, memory extraction
  - Response-side stages track content transformations (cantrips, forbidden words, verification results)
  - Each stage shows what changed (with "changed" badge), relevant setting, and metadata (keywords matched, budget allocation, tool calls, debug logs, memory keys)
  - LLM thinking/reasoning content captured in debug metadata for models that return `reasoning_content` or `thinking` fields
  - New Debug.svelte with expandable timeline: click any stage to see before/after diff
  - Debug moved from Admin tab to Dashboard tab (visible to all users, gated by debug_mode)
  - Debug Mode toggle moved from Context Budgeting to Proxy Configuration in Settings
  - Backward compatible: old-format debug exchanges auto-migrated to stage-based format
  - 18 tests covering capture logic, API endpoints, and legacy migration

- **Thinking/Reasoning Output Support**: `preserve_thinking` setting now functional
  - SSE conversion (`_convert_to_sse`) now passes `preserve_thinking` to strip or keep `<think>` tags
  - LiteLLM streaming path captures `reasoning_content` deltas alongside `content` deltas
  - Verification tester displays model thinking output and raw LLM response in collapsible sections
  - Verification check history includes thinking content from each judgment

- **Update System**: In-app update notifications and update scripts
  - Backend: `GET /api/admin/update/check` checks GitHub releases API for newer versions
  - Backend: `GET /api/admin/update/download-info` returns zip URL and update instructions
  - Frontend: Red badge on Admin sidebar button when update is available
  - Frontend: "Update" tab in Admin page with version comparison, release notes, download link, and step-by-step instructions
  - Auto-checks for updates on page load and every 5 minutes (admin users only)
  - Update scripts: `scripts/update-windows.bat`, `scripts/update-macos.sh`, `scripts/update-linux.sh`
  - Scripts: stop server, backup database, reinstall dependencies, rebuild frontend, restart server
  - 13 tests for version parsing and update check API

### Changed

- Debug moved from standalone sidebar page to a tab under Dashboard (visible to all users)
- Debug mode toggle relocated from Context Budgeting section to Proxy Configuration in Settings
- Verification tester now sends `rule_id` in test requests (bug fix)

### Fixed

- CodeEditor multi-line syntax highlighting: highlight.js now processes the full code block instead of per-line, correctly highlighting multi-line comments (`/* ... */`), template literals, and other multi-line tokens
- Verification tester "Either prompt or rule_id must be provided" error: `rule_id` was collected in the dropdown but not sent in the API request

### Also includes all features and fixes from the unreleased 0.14.5 cycle:

- Local Folder Repos, Content Pack Creator, Scenario Summarization, Skills & Writing Samples, Deployment Modes, Local Root CA + Leaf Certificate, Per-Endpoint Default Model, HTTP→HTTPS Redirect, LiteLLM Provider Compatibility, Expanded Memory System (`user_data`/`cantrip_data`), Multi-Database Support (PostgreSQL/MariaDB), Docker Distribution, Deploy Script Hardening
- Fixes: Lorebook bare-array import, duplicate diagnostics results, LiteLLM error log noise, .env file loading/corruption, deploy script LAN_IP detection, startup banner

## [0.14.0] - 2026-06-23

### Added

- **Maps (Multi-Stage Pipelines)**: Workflow presets that chain multiple LLM stages (e.g., Writing LLM > Gamemaster LLM > Narrator LLM) into a single request. Each stage has its own lorebooks, cantrips, endpoint, model, driver-callable turns, and verification. Three output modes (persist/sanitize/discard) control how stage output feeds forward. Sticky vs stage-only resource attachments. Activated via `<#map-tag#>` tags. Global cap for max map stages in Admin settings.
  - `map_pipeline.py` stage execution engine (`resolve_map`, `run_map_pipeline`)
  - `maps` table, `map_stages` table, `map_stage_resources` table with migrations
  - Maps CRUD API and Maps editor UI (stages as cards, resource selectors, per-stage verification)
  - Export/import as self-contained JSON with resource dedup modes (keep_both/reuse/overwrite)
  - Content pack integration (`maps/` folder auto-discovery, safety scanner for map files)
- **In-App Documentation**: User guide now served as HTML at `/help`. Each management page has a `?` icon linking to the relevant guide section. HTML mirror of the markdown user guide with anchored section headers.
- **File Logging**: Auto-creates `data/logs/gitinthevan.log` when `GITV_LOG_FILE` is unset. Log rotation by size (`GITV_LOG_MAX_SIZE_MB`, default 1MB) and retention by age (`GITV_LOG_RETENTION_DAYS`, default 30 days). Server Logs tab reads from this file.
- **Mobile Responsive Sidebar**: Sidebar collapses to a hamburger menu on narrow screens.
- **Menu Icons**: Navigation items now have icons.

### Changed

- Admin page now has five tabs: Global Caps, Users, Debug, Audit Logs, Server Logs. Users and Debug are no longer standalone sidebar pages.
- Debug Mode toggle moved to Settings > Context Budgeting card; Debug viewer is the Admin > Debug tab.
- User guide rewritten with all 12 sections, correct screenshot paths, and Cantrip Snippets section.
- Cantrip authoring guide updated to document database-backed persistence (`context.chat_data`, `context.memory`, `context.budget`, `context.response`, `context.tool_call`) and Maps integration.
- Verification Test tab now has a rule dropdown to auto-load rule prompts.

## [0.13.1] - 2026-06-22

### Added

- **Per-Endpoint API Keys**: Create multiple `gitv_` API keys per user, each mapped to a specific endpoint. Enables multi-platform routing from a single GitInTheVan instance (e.g., one key for JanitorAI routing to endpoint A, another for SillyTavern routing to endpoint B). Each endpoint card shows its associated keys with enable/disable/delete controls. Default keys (no endpoint mapping) shown in a separate section
- **Admin Panel**: New Admin page (visible to admins only) with three tabs:
  - **Global Caps**: Set max driver-callable turns (default 2), max verification retries (default 3), and per-server rate limits. Uses `min(user_setting, global_cap)` — doesn't overwrite user preferences
  - **Audit Logs**: Read-only view of admin actions (user creation, deletion)
  - **Server Logs**: Read-only view of recent server log output with runtime log level override (DEBUG/INFO/WARNING/ERROR/CRITICAL). Takes effect immediately without restart
- **Per-Endpoint Content Bypass**: Bypass method moved from a global user setting to individual endpoint configuration. Each endpoint card shows its bypass method. The global Content Bypass card has been removed from Settings
- **Rate Limiting**: In-memory sliding window rate limiter on proxy endpoints (default 60/min) and management API (default 120/min). Configurable via `GITV_RATE_LIMIT_ENABLED`, `GITV_RATE_LIMIT_PROXY_PER_MIN`, `GITV_RATE_LIMIT_API_PER_MIN`. Returns HTTP 429 with `Retry-After` header when exceeded
- **Request Body Size Limit**: Rejects requests exceeding configurable maximum (default 10MB) with HTTP 413. Set via `GITV_MAX_REQUEST_BODY_SIZE`
- **Password Strength Validation**: Passwords must be at least 8 characters (configurable via `GITV_MIN_PASSWORD_LENGTH`) and contain at least one letter and one number. Enforced on setup, user creation, and password reset
- **Audit Logging**: Admin actions (user creation, user deletion, password reset) are logged with timestamp, action type, and target. Viewable via `/api/audit` endpoint. Auto-pruned to 1000 entries per user
- **CORS Configuration**: Origins are now configurable via `GITV_CORS_ORIGINS` environment variable (comma-separated, default `*`). When non-wildcard, `allow_credentials` is properly enforced
- **JWT Expiration Configuration**: Token lifetime now configurable via `GITV_JWT_EXPIRATION_HOURS` (default 24)

### Changed

- Content bypass is now resolved per-endpoint via routing, not from UserSettings
- `_resolve_target` returns `bypass_method` from the endpoint record
- Rate limit values from admin settings override env var defaults at runtime
- CORS middleware now uses configurable origins instead of hardcoded wildcard
- API key table (`api_keys`) now wired into routing — checked before legacy `User.gitv_api_key` fallback

### Fixed

- Deploy scripts: pip upgrade before install, Python version enforcement, auto-install prompt

### Security

- Default secret key warning: deployments should set `GITV_SECRET_KEY` to a strong value
- Rate limiting prevents brute-force attacks on proxy and management API
- Password strength requirements prevent weak passwords
- CORS origins are configurable instead of hardcoded wildcard

## [0.12.0] - 2026-06-22

### Added

- **Context Budgeting System**: Weighted token budget allocation across cantrips and lorebooks. Cantrips access their allocation via `context.budget` (total, remaining, weight, share, detail_level). Dynamic detail scaling (full/summary/bullets) based on remaining tokens. Configurable per-user budget percentage and context window override. Per-resource budget weight on cantrips and lorebooks.
- **Memory Rules System**: Taggable per-conversation summarization rules with override thresholds, keep_recent, and custom prompts. Rules activate via `<#memory-rule-tag#>` tags. UnTagged rules act as defaults. First matching rule wins (tagged > default).
- **Debug Mode**: Captures last 20 pipeline exchanges with full visibility (original messages, modified messages, response, verification results). Toggle in Settings. Dedicated Debug page with side-by-side pipeline view.
- **Per-Rule Verification Endpoints**: Each verification rule can specify its own endpoint and model, falling back to global settings when unset.
- **Deploy Script Python Auto-Install**: Windows (winget), macOS (Homebrew), and Linux (apt/dnf/pacman) deploy scripts now offer to install Python 3.12+ if not found or outdated.
- **Jump to Top/Bottom Buttons**: Code editor now has floating navigation buttons for scrolling to the start or end of long files.
- **Python Version Enforcement**: Deploy scripts now check for Python 3.12+ and refuse to continue with older versions.

### Fixed

- **Deploy Script Dependency Installation**: Scripts now upgrade pip before installing dependencies, preventing silent failures with hatchling/pyproject.toml editable installs on systems with bundled old pip.
- **Deploy Script Error Handling**: Windows script now checks pip install exit code and reports errors instead of silently continuing.

### Changed

- Node.js minimum version updated from 20+ to 24+ in deploy scripts (Vite 8/Rolldown requirement).

## [0.11.4] - 2026-06-22

### Fixed

- **Content Pack repo linking on Windows**: Dulwich leaves `.git/objects/pack/*.idx` file handles locked on Windows, causing `tempfile.TemporaryDirectory.__exit__` to raise `PermissionError` which propagated as 500 Internal Server Error. Replaced with custom `_WinTempDir` using `shutil.rmtree(ignore_errors=True)` to suppress cleanup errors.

## [0.11.3] - 2026-06-22

### Fixed

- **Session expiry redirect**: When JWT expires, the page now properly redirects to Login instead of showing a stale Dashboard with login URL. `initializeAuth()` in stores.ts properly validates the token and triggers `logout()` when 401 is received
- **Admin sidebar on first login**: `checkAdmin()` now called immediately after login in Login.svelte, no longer requires F5 refresh
- **Dashboard active state on first login**: Login redirects to `#/` explicitly so the hashchange fires and the Dashboard nav item highlights
- **Lorebook pipeline positions**: Pipeline position checkboxes now visible in the lorebook detail view with an "Edit Positions" modal
- **Content Pack repo linking**: Removed unsupported `depth=1` parameter from dulwich clone (caused Internal Server Error). Added better error messages for auth failures and 404s
- **API key lost on logout**: API key is no longer cleared from localStorage on logout. It's a proxy key (not auth credential) and the server only stores the hash, so clearing it forces regeneration on every session
- **Svelte 5 event syntax**: Fixed `on:blur` → `onblur` for Vite 8 / Svelte 5 compiler compatibility

### Added

- **Repo name autofill**: When linking a content pack repo, if the Name field is blank it auto-fills from the URL (e.g., `https://github.com/Tydorius/GitInTheVan-Public` → `Tydorius/GitInTheVan-Public`)

## [0.11.2] - 2026-06-22

### Security

- **Vite upgraded to 8.0.16**: Now on the latest Vite release with Rolldown bundler. Resolves all CVEs identified in the supply chain audit. 0 npm vulnerabilities
- Node.js 24.17.0 detected — full compatibility with Vite 8 and @sveltejs/vite-plugin-svelte 7

## [0.11.1] - 2026-06-22

### Security

- **Frontend toolchain upgraded**: Vite 5.4.21 → 7.3.5 (resolves CVE-2026-39365 path traversal, CVE-2025-32395 request bypass, CVE-2025-58751 symlink bypass). @sveltejs/vite-plugin-svelte 3.1.2 → 6.2.4 (resolves Svelte 5 compilation peer dependency conflicts, eliminates --force/--legacy-peer-deps bypasses)
- **Vite dev server hardened**: Server explicitly bound to `127.0.0.1` (prevents lateral network access). Filesystem strict mode enabled with deny list for `.env`, `package.json`, `package-lock.json`
- **Python dependencies pinned**: All dependencies changed from `>=` floor to exact `==` pins to prevent supply chain attacks via transitive dependency updates. Pinned: fastapi 0.136.3, uvicorn 0.49.0, httpx 0.28.1, sqlalchemy 2.0.50, aiosqlite 0.22.1, pydantic 2.13.4, pydantic-settings 2.14.1, python-jose 3.5.0, bcrypt 5.0.0, dulwich 1.2.6
- **API key regeneration**: New `POST /api/auth/regenerate-key` endpoint for self-service key rotation. Settings page shows "not available" message with regenerate button when key isn't in localStorage (after login, which only stores JWT)
- Fixed TypeScript optional parameter syntax incompatible with new Svelte 5 compiler

### Note

Vite 8 (latest) requires Node.js 20.19+. Currently on Vite 7 (Node 20.17 compatible). Upgrade to Vite 8 when Node.js is updated.

## [0.11.0] - 2026-06-22

### Added

- **Content Discovery and Sync (Phase 11)**: Link any git repository as a content pack and browse, install, or fork resources
- **Git repository linking via dulwich**: Pure-Python git library — no system binary dependency, works with any git endpoint (GitHub, Gitea, GitLab, local repos). Supports HTTPS clone with token authentication for private repos
- **Content pack format**: `descriptions.json` manifest with pack metadata and per-file descriptions. Auto-discovery when manifest is absent (scans type folders: `cantrips/`, `lorebooks/`, `rules/`, `maps/`)
- **Safety scanner**: Pre-install scan for cantrip JavaScript (network access, filesystem, process execution, eval, external URLs, infinite loops), lorebook entries (script tags, oversized content), and JSON validation. Three severity levels: critical (blocks), warning (allows with alert), info. All installs start disabled
- **Install vs Fork**: Install creates a linked copy (tracks repo for update notifications). Fork creates an independent copy the user owns and edits freely
- **Content browser UI**: New "Content Packs" page with repo management, browser panel (filter by type/author, sort by name/updated/type), installed items management (enable/disable, uninstall)
- **"Download at your own risk" disclaimer**: Prominent warning on every page and API response
- 21 new safety scanner tests (cantrip network/filesystem/process detection, eval/URL/loop warnings, lorebook script tags, JSON validation, file scanning)
- Migration 016 creates `linked_repos` and `installed_items` tables
- Dulwich dependency added

### Changed

- Bumped version to 0.11.0

## [0.10.0] - 2026-06-22

### Added

- **`<jslorebook>` Extraction**: Embedded JavaScript lorebook tags in character card scenario content are automatically extracted, desanitized (HTML entity decoding, newline unescaping), and stripped before forwarding to the LLM. Extracted scripts are available for execution alongside user cantrips
- **Prefill Normalization**: Provider-specific assistant message prefilling. When enabled and a trailing assistant message is detected, converts it to a system instruction for OpenAI-compatible providers (which don't support native prefill). Anthropic and Google endpoints pass through as-is (native support). Provider auto-detected from endpoint URL and model name
- **Content Bypass Plugins**: Three encoding methods to work around provider content filters:
  - Space Separation: inserts zero-width spaces between characters in sensitive words
  - Dot Separation: inserts periods between characters (more aggressive)
  - Character Replacement: replaces Latin characters with visually similar Cyrillic homoglyphs (most aggressive)
  - Includes prominent ToS violation warning in both the UI and API
  - Encoding applied to outgoing user messages; decoding applied to responses before returning to client
- Migration 016: `bypass_method` and `prefill_enabled` columns on `user_settings`
- 31 new Phase 10b tests covering jslorebook extraction (HTML unescaping, multiple blocks, message extraction, position detection), prefill normalization (provider detection, trailing assistant detection, OpenAI conversion, Anthropic passthrough), and bypass plugins (all three methods encode/decode round-trip, message-level application, ToS warning verification)
- Flow test expanded to 13 test groups: added jslorebook extraction, prefill normalization, and content bypass tests

### Changed

- Proxy pipeline stage 1 now extracts `<jslorebook>` blocks alongside tag/command tag extraction
- Prefill normalization and bypass encoding applied before forwarding; bypass decoding applied after response
- Settings API and UI now expose bypass method selector (with ToS warning) and prefill toggle
- Bumped version to 0.10.0

## [0.9.0] - 2026-06-21

### Added

- **Command Tags**: Inline tags for per-request pipeline overrides, parsed from user messages and stripped before forwarding to the LLM
- **Five controllable commands**: `<VERIFY:on|off>`, `<SUMMARY:on|off>`, `<FORBIDDEN:on|off>`, `<MEMORY:on|off>`, `<DRIVER:on|off>`
- **Persist flag**: Optional third parameter (`<VERIFY:off:persist>`) saves the override to the conversation's persistent memory. Applies to all subsequent messages in that chat until reset
- **Reset command**: `<VERIFY:reset>` clears any persistent override for that command in the current chat
- **Three-tier precedence**: One-off commands (no persist) supersede persistent commands, which supersede GUI settings. One-off applies to current request only; persistent applies until reset
- **Per-conversation isolation**: Persistent overrides are scoped to each conversation (tracked via rolling hash). Different chats have independent override state
- Command tags stored in `memories` table as `memory_type="command_override"` with prefixed keys (`__cmd_persist_*`)
- 25 new tests covering parsing (on/off/reset/persist, case-insensitive, duplicates), stripping (text and messages), extraction from user/assistant messages, and full resolve precedence (one-off > persistent > GUI, persistence across requests, reset, isolation between chats)
- Flow test: command tag test group verifying one-off override, tag stripping, persist, and reset

### Changed

- Proxy pipeline stage 1 now parses both `<#tag#>` activation tags and `<CMD:setting>` command tags
- Summarization, verification, forbidden words, memory injection/extraction, and driver-callable all check command overrides before running
- `None` (no override) means use the GUI setting; `True`/`False` means force on/off for this request
- Bumped version to 0.9.0

## [0.8.2] - 2026-06-21

### Fixed

- **Streaming memory extraction**: Pure streaming passthrough now buffers the response to extract `<memstore>` tags, strip them, and record the post-hash before re-emitting as SSE. Previously only non-streaming and verification-converted paths extracted memories.
- **Cantrip memory access**: Cantrips can now read and write persistent memory via `context.memory.get(key)`, `context.memory.set(key, value)`, `context.memory.keys()`, `context.memory.delete(key)`, `context.memory.all()`. Memory changes are saved to the database after cantrip execution.

### Changed

- Deno template extended with `context.memory` object (rebuilt in-template from `__memories` dict, same pattern as `context.chat_data`)
- `CantripResult` dataclass extended with `memories` field
- `process_cantrips` accepts optional `internal_chat_id` parameter for loading/saving memories
- Proxy passes `internal_chat_id` to `process_cantrips`
- `_forward_streaming` refactored: split into `_forward_streaming_raw` (passthrough) and `_forward_streaming_with_memory` (buffer + extract + re-emit)
- Bumped version to 0.8.2

## [0.8.1] - 2026-06-21

### Added

- **LLM Instructions field** on cantrips and lorebooks: a dedicated text field for LLM-facing instructions that appear in tool notifications. Used by `build_tool_notification` when available, falling back to Description if empty. Enables richer tool descriptions like argument syntax, usage examples, and expected output format
- Driver-Callable flow test: creates a dice rolling cantrip with `run_driver_callable=true` and `llm_instructions`, enables driver-callable turns, sends a request asking the LLM to roll dice, and verifies the tool loop executes without crashing
- `llm_instructions` field exposed in cantrip and lorebook API responses and create/update endpoints
- `llm_instructions` textarea in the cantrip editor UI

### Changed

- `build_tool_notification` now prefers `llm_instructions` over `description` when building the tool list for the Driver
- Bumped version to 0.8.1

## [0.8.0] - 2026-06-21

### Added

- **Driver-Callable Tool System**: The writing LLM (Driver) can now invoke cantrips as tools during generation using a notification-based, turn-tracked approach that works with any model — no OpenAI function-calling support required
- **Tool notification injection**: Before forwarding to the Driver, a `[TOOL ACCESS]` block is injected into the system prompt listing available tools (name + description) and turns remaining
- **Call tag parsing**: After the Driver responds, the system scans for `<call:tool_name arg="value">` tags. If found, the requested cantrip executes with args available via `context.tool_call`, and the result is returned as a `[TOOL RESULT]` message for the Driver's next turn
- **Turn tracking with auto-disable**: User-configurable turn budget (default 1). Each tool call decrements the counter. When turns reach 0, the tool notification stops being injected — the Driver no longer sees any tools, preventing infinite loops. Auto-disables when no active, tag-matched resources have `run_driver_callable=true`
- **`context.tool_call` and `context.tool_result`**: New cantrip context fields for driver-callable cantrips. `context.tool_call` provides the name and args from the call tag. Cantrips write their output to `context.tool_result` which is sent back to the Driver
- **Streaming compatibility**: When driver-callable is active, streaming requests are internally converted to non-streaming (the tool loop requires buffering to check for call tags), then converted back to SSE for the client
- **Driver-Callable settings**: Configurable turns (0 = disabled) on the Settings page
- 19 new tests covering call tag parsing, stripping, tool notification building, notification injection (append, insert, replace), and tool result formatting (244 tests total)

### Changed

- `CantripResult` dataclass extended with `tool_result` field
- Deno template extended with `context.tool_result` initialization
- Settings API response now includes `driver_callable_turns`
- Proxy pipeline updated: driver-callable loop runs before pre-Navigator/verification/post-Navigator stages when active
- Bumped version to 0.8.0

## [0.7.1] - 2026-06-21

### Added

- User management: edit username, reset password, regenerate API key, disable/enable users, delete users with full cascade cleanup of all user data (endpoints, cantrips, lorebooks, memories, summaries, forbidden words, verification rules/logs, chat data, conversation hashes, settings)
- `is_disabled` field on users — disabled users are blocked from login and proxy routing
- `/api/auth/me` endpoint exposing current user's id, username, and is_admin status
- Admin sidebar link conditionally visible based on `isAdmin` store (was hidden for everyone)
- Protection against deleting or disabling admin users
- Context Budgeting System design (Phase 12 planning): weighted token budget allocation across cantrips/lorebooks with dynamic detail scaling
- Memory Rules System design (Phase 12 planning): taggable per-conversation summarization rules with override thresholds/prompts
- Dependency version pinning added to Phase 13 (Security Hardening) planning

### Fixed

- Users page: invalid date display (created_at was not included in API response)
- Users page: admin sidebar link was hidden from all users including admins (`{#if !item.admin}` excluded everyone)

## [0.7.0] - 2026-06-21

### Added

- **Multi-Position Cantrips and Lorebooks (Phase 10)**: Cantrips and lorebooks now have four independent boolean position flags (checkboxes, not radio buttons) controlling when they execute in the pipeline
- **Pre-Navigator position**: Cantrips and lorebooks can run after the Driver (writing LLM) responds and before the Navigator (verification LLM) checks. Pre-Navigator cantrips have access to `context.response.content` to modify the response (regex cleanup, keyword checks, content formatting). Pre-Navigator lorebooks can inject correction notes into the verification context
- **Post-Navigator position**: Cantrips can run after verification completes for final cleanup (format correction, markdown repair, tag stripping). Also has access to `context.response.content`
- **Forbidden Words/Phrases Macro**: Global per-user list of forbidden phrases checked case-insensitively (or case-sensitively) against the Driver's response before the Navigator runs. Supports plain-text and regex matching. Matches are surfaced to the Navigator LLM as concrete violations in a `[FORBIDDEN CONTENT DETECTED]` block. Works with or without verification rules enabled (triggers verification loop alone if matches found and Navigator configured). Test scanner built into the Verification page
- **Post-Driver cantrip context**: Deno sandbox extended with `context.response.content`, `context.response.original_content`, and `context.response.modified` for cantrips at Pre-Navigator and Post-Navigator positions
- **Driver/Navigator terminology**: Documentation and UI now use "Driver" (writing LLM), "Navigator" (verification LLM), and "Summarizer" (summarization LLM) for clarity
- Position checkboxes in cantrip editor and lorebook CRUD
- Forbidden Words tab on Verification page with settings, phrase management, and test scanner
- 21 new Phase 10 tests covering forbidden word scanning (plain, regex, case sensitivity), forbidden words API CRUD, cantrip/lorebook position flags via API, and migration verification (225 tests total)

### Changed

- Cantrip loader now filters by position flags (`run_pre_driver`, `run_pre_navigator`, `run_post_navigator`) instead of the deprecated `hook_type` field. `hook_type` is retained for backward compatibility
- Verification `check_response` now accepts `forbidden_context` parameter; when forbidden words are matched, the summary is prepended to the Navigator's verification prompt
- Pipeline updated: tags stored in body_json for post-Driver access; pre-Navigator cantrips and forbidden word scan run before verification loop; post-Navigator cantrips run after verification
- Bumped version to 0.7.0

### Migrations

- `010_add_cantrip_position_flags`: Added `run_pre_driver`, `run_driver_callable`, `run_pre_navigator`, `run_post_navigator` to cantrips table
- `011_add_lorebook_position_flags`: Same four position flags added to lorebooks table
- `012_add_user_settings_forbidden_words_fields`: Added `forbidden_words_enabled`, `forbidden_words_case_sensitive`, `driver_callable_turns` to user_settings
- `013_create_forbidden_words_table`: Created `forbidden_words` table for phrase storage

## [0.6.0] - 2026-06-21

### Added

- Chat Memory Summarization (Phase 9): automatically compresses long conversations into a summary when the estimated token count exceeds a configurable threshold
- Conversations exceeding the threshold have their older dialogue summarized by a user-selected LLM endpoint and replaced with a `[CONVERSATION SUMMARY]` context block, while the most recent messages are always forwarded verbatim
- Rolling summary reuse: summaries are cached per conversation keyed by a boundary hash of the summarized messages, so rerolls/forks reuse the cached summary without re-calling the LLM; continuations build on the prior summary for efficiency
- System messages (persona, lorebook constant entries, cantrip scenario additions) are preserved during compression; only user/assistant dialogue turns are summarized
- Conversation-hash continuity preserved: the rolling conversation hash is captured before compression so fork/reroll detection is unaffected
- Summarization runs on the request side, so it works for both streaming and non-streaming requests (no buffering required)
- Configurable summarization settings: enable/disable, endpoint selection, model override, token threshold, number of recent messages to keep, and a customizable summarization prompt
- Summaries management view on the Memories page: list, view full summary text, and delete conversation summaries
- Dedicated `conversation_summaries` table for summary storage with backward-compatible additive migration (008, 009)
- 35 new summarization tests covering token estimation, boundary hashing, transcript formatting, message compression logic, LLM call handling, caching/reuse, threshold gating, settings CRUD, and summaries list/delete API

### Changed

- Edit-tolerant rolling hash: conversation resolution now excludes the current user message AND the preceding assistant message, so editing or swiping the LLM's most recent response no longer breaks the conversation chain or orphans its memories/summaries. The recorded anchor is the request messages exactly as sent (the bot response is no longer part of the hash).
- Backward-compatible legacy fallback: conversations recorded under the previous hashing scheme (which included the bot response) still resolve correctly on their next unedited turn, then transition to the new scheme
- Fork detection preserved: editing an older (non-most-recent) LLM message within the hash window still creates a new conversation chain
- 16 new conversation-hash tests covering the slicing logic, multi-turn chaining, edit tolerance, fork detection, legacy fallback, and record/dedup behavior
- Bumped version to 0.6.0
- Proxy pipeline now includes a summarization stage after cantrip execution (stage order: tag extraction, conversation resolution, memory injection, lorebook injection, cantrip execution, summarization, forward)

## [0.5.0] - 2026-06-19

### Added

- Core proxy engine: OpenAI-compatible request forwarding with streaming and non-streaming support
- Multi-user system with JWT authentication and per-user `gitv_` API key routing
- Endpoint management with custom API base paths (supports `/v1`, `/api`, and full URL auto-detection)
- Lorebook engine with keyword matching, constant/selective entries, position-based injection, and enable/disable toggle per lorebook
- Lorebook import from SillyTavern, Chub, and JanitorAI JSON formats (handles dict-keyed entries, alternative field names, numeric position codes)
- Lorebook JSON file export, file picker import, and manual JSON editing mode
- Cantrip system: sandboxed JavaScript execution via Deno subprocess with full JanitorAI context API compatibility
- GitInTheVan `context.chat_data` extension for per-chat persistent key/value storage across cycles
- Cantrip tester: run cantrips against sample context with custom messages and chat data without forwarding to an LLM
- Cantrip syntax validation via Deno dry-run with error line highlighting in the editor
- Cantrip templates: one-click install of pre-built cantrips (Simple Dice Roller, Status Tracker, Day Counter, Weather System)
- Real-world cantrip compatibility verified: Complex Lorebook, Dice Controller, Multiple Character, Adaptive Lorebook, Hidden Persistent Memory, Context Control, and Property Exploration templates
- Verification system: LLM-based response checking with configurable rules, two resubmission strategies (add_instructions, rewrite), configurable retry limits, and verification logs with auto-refresh
- Verification test endpoint for ad-hoc response checking
- Streaming-to-non-streaming conversion when verification is enabled, with SSE conversion back to client
- Diagnostic /audit endpoint with endpoint selector for troubleshooting connectivity and configuration
- Tagging system: activate lorebooks, cantrips, and verification rules via `<#type-name#>` delimiters in persona or message text; tags are auto-stripped before forwarding to LLM; duplicate tag prevention within each resource type
- Tag edit modal with pencil icon, copy-to-clipboard for full activation tag, and inline error display
- Web management UI built with Svelte 5: login/admin setup, dashboard with diagnostics, endpoints, cantrips (with templates), lorebooks (with manual JSON edit), verification (rules/settings/logs/test tabs), settings (streaming/UX), and user management
- Code editor component with syntax highlighting (JavaScript, JSON, Markdown), line numbers, and error line highlighting
- ON/OFF toggles on all resource list views (lorebooks, cantrips, verification rules)
- Streaming and UX settings: GITV status blocks, preserve thinking, simulated streaming speed
- API key display with show/hide toggle and copy-to-clipboard
- Endpoint API key field with show/hide toggle (avoids browser password manager interference)
- Login redirect when session expires
- Database migration system with backward-compatible schema updates
- Environment variable configuration via `.env` file
- Deploy scripts for Windows, macOS, and Linux (auto-downloads Deno, builds frontend, creates config)
- 152-test automated test suite covering all implemented features

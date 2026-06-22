# Changelog

All notable changes to GitInTheVan are documented in this file.

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

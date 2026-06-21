# Changelog

All notable changes to GitInTheVan are documented in this file.

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

# Changelog

All notable changes to GitInTheVan are documented in this file.

## [0.5.0] - 2026-06-19

### Added

- Core proxy engine: OpenAI-compatible request forwarding with streaming and non-streaming support
- Multi-user system with JWT authentication and per-user `gitv_` API key routing
- Endpoint management with custom API base paths (supports `/v1`, `/api`, and full URL auto-detection)
- Lorebook engine with keyword matching, constant/selective entries, position-based injection, and enable/disable toggle per lorebook
- Lorebook import from SillyTavern, Chub, and JanitorAI JSON formats (handles dict-keyed entries, alternative field names, numeric position codes)
- Lorebook JSON file export and file picker import
- Cantrip system: sandboxed JavaScript execution via Deno subprocess with full JanitorAI context API compatibility
- GitInTheVan `context.chat_data` extension for per-chat persistent key/value storage across cycles
- Cantrip tester: run cantrips against sample context with custom messages and chat data without forwarding to an LLM
- Real-world cantrip compatibility verified: Complex Lorebook, Dice Controller, Multiple Character, Adaptive Lorebook, Hidden Persistent Memory, Context Control, and Property Exploration templates
- Verification system: LLM-based response checking with configurable rules, two resubmission strategies (add_instructions, rewrite), configurable retry limits, and verification logs with auto-refresh
- Verification test endpoint for ad-hoc response checking
- Streaming-to-non-streaming conversion when verification is enabled, with SSE conversion back to client
- Web management UI built with Svelte 5: login/admin setup, dashboard, endpoints, cantrips, lorebooks, verification (rules/settings/logs/test tabs), settings, and user management
- ON/OFF toggles on all resource list views (lorebooks, cantrips, verification rules)
- API key display with show/hide toggle and copy-to-clipboard
- Endpoint API key field with show/hide toggle (avoids browser password manager interference)
- Login redirect when session expires
- Environment variable configuration via `.env` file
- 152-test automated test suite covering all implemented features

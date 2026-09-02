# Pipeline Order

The ordered stages a chat completion request passes through in `app/services/proxy.py`, taken from the actual `debug_capture()`/`debug_capture_response()` call sites (also the order shown in the Debug timeline).

Stage names below are the `name` argument to `debug_capture`/`debug_capture_response`; labels are what the Debug UI shows. A stage is only recorded when Debug Mode is on for the user, but it always *runs* — capture is observation, not gating.

## Request side (before the upstream LLM call)

Runs in this order for every request, map or not:

1. `tag_group_resolution` — expands `<#taggroup-x#>` tags into member tags (dedup applied). Reads/writes `_gitv_tags`.
2. `memory_injection` — injects the `[PERSISTENT MEMORY]` block built from stored `<memstore>` key/values for this chat (skippable via `<MEMORY:off>`).
3. `scenario_summarization_pre` — fires after memory injection, before lorebooks/cantrips; compresses an oversized system message (character card, OOC, memories) if a Pre-position scenario rule's threshold is exceeded.
4. `lorebook_injection` — keyword-matches active lorebook entries and injects them. **Only runs once, here.** The `Lorebook` model carries `run_pre_navigator`/`run_post_navigator`/`run_driver_callable` columns, but `_apply_lorebook_injection()` in `proxy.py` never reads them — lorebooks are effectively pre-driver-only in the live pipeline regardless of what those flags say. Treat the user-guide's "lorebooks support the same four pipeline positions as cantrips" as aspirational, not current behavior.
5. `skills_injection` — injects endpoint-attached skills into the system message; loads (but does not yet inject) writing samples.
6. `budget_preparation` — computes the context budget (`_gitv_budget`) that cantrips can read via `context.budget`.

Then the pipeline branches on whether a map is active (`resolve_map()` — tag match or `default_map_id`; skippable via a `<MAP...>`-suppressing command override). **Both branches share stages 1–6 above.**

### Map branch (see also: Maps section below)

7. Global cantrip pass at `pre_driver` position, **excluding** any cantrip bound to a map stage (`exclude_ids=stage_bound_cantrip_ids(map_active)`), captured as `pre_driver` (label "Cantrips (Global Pass, Pre-Map)").
8. `run_map_pipeline()` runs — see the Maps section below for its internal per-stage stages.
9. After all stages: `maybe_summarize_scenario(..., "post")` runs once (uncaptured in the map branch's own debug stages; mirrors the non-map `scenario_summarization_post` stage).
10. Response memories are extracted (`_extract_response_memories`) and the map's own verification result (if any stage had verification enabled) is threaded through to `_save_debug_exchange`.
11. Response is returned (converted to SSE first if the client asked for streaming).

### Standard (non-map) branch

7. Cantrips at `pre_driver` position (label "Cantrips (Pre-Driver)").
8. `conversation_summarization` — rolling-hash-triggered summarization of older turns.
9. `scenario_summarization_post` — fires after cantrips/skills/lorebooks; controls final system-message size.
10. `writing_samples` — injects endpoint-attached writing samples before the last message.
11. `driver_callable` — if any active cantrip has Driver-Callable checked and turns remain, injects the `[TOOL ACCESS]` notification listing callable tools.
12. `prefill` — normalizes prefill formatting for endpoints that support it.
13. `bypass_encoding` — applies the endpoint's configured content-bypass method (space/dot separation, homoglyph replacement) if not `none`.
14. `llm_parameters` — resolves and applies the merged parameter layers (see `parameter-layers.md`). Applied **last**, immediately before serialization, so no later stage can undo a configured value.
15. `final_messages` — pure snapshot marker; no transformation, just records the exact body about to go upstream.

The request is then forwarded. **What changes when verification or Driver-Callable is on:** if either is active and the client requested `stream: true`, the request is forced to `stream: false` for the upstream call (`_forward_non_streaming_verified`), the full response is obtained and processed, and only then converted back to a synthetic SSE stream (`_convert_to_sse`) for the client. Verification cannot check tokens it hasn't fully seen, and the tool-call loop needs the complete text to detect `<call:...>` tags, so both force buffering.

## Response side (after the upstream LLM call, standard branch)

1. LLM response received — via plain forward, the failover chain, or the Driver-Callable tool loop (which itself makes 1+ upstream calls, each one recorded to `run.llm_calls`).
2. `pre_navigator_cantrips` — cantrips at `pre_navigator` position run against `context.response.content`; can rewrite the response text.
3. `forbidden_words` — fast string-match scan of the (possibly cantrip-modified) response; matches are logged and, if the Navigator is enabled, prepended to the verification prompt as `[FORBIDDEN CONTENT DETECTED]`.
4. `verification` (via `run_verification_loop`) — runs configured, tag-activated rules in order; on violation, resubmits (Add Instructions or Rewrite strategy) up to the rule's retry limit. A rule that could not run (endpoint unreachable) is captured as "not enabled"/skipped, never as approved.
5. `post_navigator_cantrips` — final-cleanup cantrips at `post_navigator` position, run on the verified/approved response.
6. Response memories extracted — `<memstore>` tags parsed out of content and saved (`memory_extraction`), unless this is a replay (memory writes are explicitly suppressed for replays — see `debug-and-sandbox.md`).
7. `bypass_decoding` — reverses the request-side bypass encoding if one was applied.
8. `llm_response` — final captured content (and reasoning/thinking, if present) for the Debug trace.

Steps 2–5 are skipped if the response has no `choices` or empty content. The Driver-Callable tool loop, when active, runs its own sub-loop of upstream calls *before* step 2, and shares the same post-processing (2–7) once it exits the loop.

## Maps: internal stage order (`app/services/map_pipeline.py`)

For each stage, in `stage_order`:

1. `map_stage_cantrips` — cantrips bound to this stage (`only_ids`) run first.
2. `map_stage_parameters` — resolved parameter layers for this stage's endpoint/model.
3. `map_stage` — records the resolved endpoint, output mode, resource list (with `sticky` flags), verification/driver-callable config.
4. Stage's LLM call (`_forward_stage_llm`), recorded via `record_llm_call(purpose="MAP_STAGE", ...)`.
5. `map_stage_output` — the stage's raw response content.
6. `map_stage_verification` (if `stage.verification_enabled`) — approve/reject/errored, does not stop the chain on rejection (logs a warning and continues).
7. Forbidden-words scan (uncaptured as its own named stage inside the map loop; a match becomes sticky system context for later stages, or a logged warning if it lands in the final stage with nothing downstream to fix it).

Each stage rebuilds its request from a pristine `base_messages` snapshot, not the previous stage's mutated list — earlier injections do not compound unless a resource is marked **sticky** (persists into every later stage) or the stage's `output_mode` is `persist`/`sanitize` (see `#maps` → Output Modes and Sticky vs Stage-Only Resources). `discard` output never reaches later stages. Driver-Callable turns configured on a map stage are recorded but **not wired up** — the tool loop only runs on the non-map path, so a map stage's tools are never offered to that stage's LLM.

## See also

- `#cantrips`
- `#lorebooks`
- `#verification`
- `#memories`
- `#maps`
- `#debug`
- `#model-parameters`

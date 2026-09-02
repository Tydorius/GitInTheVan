# Debug and Sandbox

GitInTheVan's end-to-end request tracing, replay, sandboxing, and comparison system. Implemented across `app/services/debug.py`, `debug_metrics.py`, `debug_replay.py`, `debug_sandbox.py`, and the capture calls threaded through `proxy.py`/`map_pipeline.py`.

## Debug mode is per user

`UserSettings.debug_mode`, checked by `is_debug_mode(user_id)` — a boolean the user flips in Settings. It gates whether a request's pipeline is captured at all; when off, the pipeline runs identically but nothing is recorded, and capture never changes what a request does (it is a passive tap, not a control).

## What a run captures

`init_debug()` seeds the trace with the original message snapshot, extracted tags, and the client's own top-level body keys (`original_params` — captured before any stage can mutate them, so a replay can reproduce the client's real sampling settings, not just `{model, messages, stream}`). From there:

- **Stages** (`run.stages`, via `debug_capture`/`debug_capture_response`): one entry per pipeline stage, in execution order — name, label, detail summary, before/after message or content snapshot, a `changed` signal, and stage-specific metadata (matched lorebook entries, memory keys, resolved parameters + source layer, cantrip pass results, etc.). See `pipeline-order.md` for the full stage list.
- **LLM calls** (`run.llm_calls`, via `record_llm_call()`): one row per actual upstream call — `purpose` (`MAIN` for the Driver, `VERIFICATION_JUDGE`, `SUMMARIZER`, `MAP_STAGE`), endpoint id/name, provider, model requested vs. resolved, latency, status, token usage (from the provider's `usage` object when returned, otherwise estimated — never silently mixed with a real count), and the failover attempt number. A forwarder that skips this call is invisible to the run's cost accounting.
- **Cantrips** (`run.cantrips`): every cantrip *considered* at each position, whether it triggered, why not if it didn't (most often: tag not present), duration, and error if any. This is identity + outcome, not the cantrip's source code.
- **Injections**: captured as stage metadata rather than a separate list — e.g. `lorebook_injection`'s metadata carries the matched entries and their token counts; `skills_injection`'s carries which skills/samples loaded.
- **Verification**: `verification_data` — approved/rejected, retries used, and a `check_history` of per-attempt judgments (violations, severity, the judge's raw thinking/reasoning if the model returned any).

## Saved runs and pruning

Captured runs are pruned to the 20 most recent per user (`MAX_DEBUG_EXCHANGES`, hardcoded), skipping any run marked **saved**. Saving exempts a run from pruning entirely, up to an admin-configured cap (`max_saved_debug_runs`, default 10) — the save-slot counter in the UI refers to this cap, separate from the 20-run rolling window. Each trace is also individually size-bounded (`max_debug_exchange_kb`, default 512 KB) before being serialized to the DB, so one abnormally large run cannot blow out storage.

## Replay

Replay resends a saved run's original messages through the request pipeline again, under **today's** configuration, and links the result back to the source run. It goes through the real pipeline code path — no shortcut — which is what makes it evidence rather than a simulation.

Replay is isolated from the real conversation by giving it a synthetic chat id (`replay:<exchange_id>`) instead of resolving the actual conversation, and response-side memory writes (`<memstore>` extraction) are explicitly and unconditionally skipped for replays — this is a hard guard in `proxy.py`, not incidental.

**That isolation is narrower than it looks, and this matters for anyone relying on replay being side-effect-free.** `context.chat_data` is keyed by conversation id, so a cantrip's chat-scoped writes during a replay land in the synthetic `replay:...` chat and never touch the real conversation. But `context.user_data` and `context.cantrip_data` are keyed only by `user_id` (and cantrip id) — they are **not** conversation-scoped at all, replay or not. A cantrip that writes to `context.user_data` or `context.cantrip_data` during a replay writes to the same real, shared store a live request would. Replaying a run whose cantrips have such side effects (a balance change, a persistent counter) re-triggers those effects for real. Say this to a user before they replay a run with stateful driver-callable or pre-driver cantrips.

## Sandboxes

A sandbox (`create_sandbox`) forks a source run into a standalone, re-runnable conversation: it copies the memories, `ChatData` rows, and conversation summary the source request read into a new synthetic chat id, so repeated runs inside the sandbox accumulate their own state and the original conversation is never touched. From a sandbox you can run it, edit the prompt, reset it back to the forked starting state, or delete it (and its forked state) entirely.

**Nothing a sandbox does reaches the original chat platform.** A sandboxed run executes inside GitInTheVan and its response is shown in the sandbox UI; the client (JanitorAI, SillyTavern, etc.) that originated the source request is never contacted.

The same `user_data`/`cantrip_data` caveat as replay applies here too: the fork copies chat-scoped state only (memories, chat data, summary). `context.user_data` and `context.cantrip_data` are per-user, not per-chat, so they are never forked or isolated — a cantrip run inside a sandbox that touches either writes to the real, live per-user store, exactly as it would outside the sandbox.

## Compare

Puts 2–4 runs side by side with one designated **baseline** (radio-button selectable); the others are shown as deltas from it — token/timing differences with direction and percentage, and a word-level diff of the response text. A run served by a different model/endpoint/provider than the baseline is called out explicitly rather than silently compared.

## Export and deep links

**Export .md** and **Export .json** save a run, or a whole comparison, to a file — the JSON export is the full trace and is what belongs in a bug report. The Compare view is also directly linkable/bookmarkable via a hash URL carrying the compared run ids and baseline (`#/debug/compare?ids=<a>,<b>,...&baseline=<a>`), generated automatically when a comparison or sandbox-to-compare action completes.

## See also

- `#debug`
- `#cantrips`
- `#verification`
- `#memories`

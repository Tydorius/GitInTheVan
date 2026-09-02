# Parameter Layers

How GitInTheVan decides *how* to call a model (temperature, `reasoning_effort`, `max_tokens`, anything else a provider accepts) once a scope has already decided *which* model to call. Single source of truth: `app/services/llm_params.py`.

## What a parameter is

A `ParamDef`: `name`, `type`, `value`, plus optional `description`, `required`, `options` (an allow-list that turns the value into a dropdown in the UI and drops the value if it isn't in the list). Types (`PARAM_TYPES`): `string`, `string[]`, `number`, `float`, `integer`, `boolean`. `number` and `float` both coerce to a Python float — separate labels, same coercion.

**Reserved names**, which can never be used as a parameter: `messages`, `model`, `stream`, and anything starting with `_gitv`. Enforced twice — once in the Pydantic validator (`ParameterDef`), once again in `apply_to_body()` — because parameters also arrive through map-pack import, which can bypass the validator. `model` has its own dedicated override field at every scope instead; `stream` is owned by the pipeline (verification and Driver-Callable force it false, so a configured `stream` would silently break those requests).

**Caps**: at most 32 parameters per scope (`MAX_PARAMS_PER_SCOPE`), and the stored JSON blob is capped at 8192 characters (`MAX_BLOB_CHARS`).

A malformed or uncoercible parameter is dropped with a log line, never raised — `parse_params` and `coerce` run on every proxied request, and a corrupt blob degrading to "that parameter is absent" is the only acceptable failure mode on the hot path.

## Layer order: broadest to closest

`resolve()` takes an ordered list of `(label, params)` layers and merges them **broadest first**; the last layer to set a given key wins. The client's request body is *not* a layer — it is the body `apply_to_body()` writes the resolved values over, so a key nobody configured survives untouched, and a key that *is* configured always overwrites what the client sent.

| Order | Scope | Where configured |
|---|---|---|
| 1 (broadest) | User settings, per role | Settings → default requests / verification calls / summarization calls |
| 2 | Endpoint | Endpoints → Edit → Endpoint Parameters |
| 3 | Per-model, on that endpoint | Endpoints → Edit → Models → the model's own parameters |
| 4 (closest) | The specific call site | Verification rule / map stage / scenario summarization rule |

Concretely, for the Driver call: `_apply_llm_parameters()` in `proxy.py` builds `[("user settings", user_layer), (endpoint_label, candidate.parameters), (model_label, candidate.params_for_model(model))]` and calls `resolve()`. Map stages and verification calls build their own layer lists the same way, ending in their own call-site layer.

Every layer is provenance-tagged: `ResolvedParams.sources` records which label supplied each final value, which is what the Debug "LLM Parameters" stage displays.

## Seven independent call sites

There is no shared forwarder. Each of these applies `apply_to_body()` itself, and each is a place a future change can silently skip parameter application if it doesn't call it: the Driver call, the failover retry, the LiteLLM-provider forward path, the verification judge call, the verification retry call, both summarizers (conversation and scenario), and both map-stage call paths (generation and stage verification). Hard-coded values in these call sites (e.g. the judge's `max_tokens: 200`, a summarizer's default temperature) are seed defaults, not fixed values — a configured layer must be able to override them.

## LiteLLM routing (provider endpoints)

`_do_forward_litellm` builds its call from named kwargs, not a raw passthrough body. `LITELLM_NATIVE_PARAMS` (in `app/services/proxy.py`) is the frozenset of names LiteLLM accepts directly: `temperature`, `max_tokens`, `top_p`, `top_k`, `stream`, `stop`, `frequency_penalty`, `presence_penalty`, `seed`, `n`, `logprobs`, `user`, `reasoning_effort`, `max_completion_tokens`, `response_format`, `thinking`. Anything the user deliberately configured (present in the resolved parameter set) that falls outside this list is routed through LiteLLM's `extra_body` instead of being dropped. A name that only the *client* sent (never configured by the user) and that falls outside `LITELLM_NATIVE_PARAMS` is dropped on this path — the raw client body is not forwarded, and guessing which unknown keys a given provider will tolerate is what caused earlier `top_k` failures on Gemini. `reasoning_effort` and `max_completion_tokens` were missing from this set until 0.24.0, which is why a client-sent `reasoning_effort` used to vanish on every provider endpoint.

## Seeing what was applied

Turn on Debug Mode, send a request, open the run: the Pipeline Timeline has an `llm_parameters` stage ("LLM Parameters") whose metadata is the resolved value set plus the source layer for each key — the direct answer to "why did this call get `temperature: 0.1`?". Map stages get their own equivalent `map_stage_parameters` entry per stage.

## See also

- `#model-parameters`
- `#debug`
- `#endpoints`
- `#verification`
- `#maps`

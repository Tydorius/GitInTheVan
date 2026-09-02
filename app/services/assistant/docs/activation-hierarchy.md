# Activation Hierarchy

The single rule GitInTheVan uses to decide whether a resource (lorebook, cantrip, verification rule, map, memory rule) participates in a given request.

Every activation decision routes through `should_activate_resource()` in `app/services/tagging.py`. It is not one convention among several — it is the only correct way to answer "is this thing on for this request?" anywhere in the codebase.

## The four rules

1. **Off is the default.** A resource participates only if something turns it on. If nothing is turned on, GitInTheVan is a plain proxy.
2. **Activation is a union. Nothing deactivates.** Sources add; they never subtract. There is no way to express "off" — only "not yet on" — so no source can ever countermand another, and no ordering bug can silently suppress a resource the user asked for.
3. **Proximity wins.** The closer an activation is to the message, the more authority it carries. A `<#type-name#>` tag in the system prompt, persona, or message text is the most immediate expression of intent, so it activates the resource **regardless of its Active flag**.
4. **The Active flag is the blanket source.** It applies only when no tag has spoken for that resource. "Active" therefore means *on for every request*, not *eligible*. To make a resource tag-only, turn Active off.

Because of rule 2, "more authority" (rule 3) only ever means "guaranteed to activate." It never means "overrides to off." A resource with Active turned off in the UI still runs if a request tags it by name — that is correct behavior, not a bug: naming it in the message is the user asking for it directly.

## Active means blanket, not eligible

Read `is_active=True` as "runs on every request that reaches this pipeline," not as "available to be turned on by a tag." Tags do not need a resource to be Active to fire it (rule 3); Active-without-a-tag is what makes a resource run unconditionally (rule 4). These are two independent paths to "on," not a gate and a switch.

## Tag syntax

`<#type-name#>` (see `app/services/tagging.py`, `TAG_PATTERN`). Recognized type prefixes, from `TAG_TYPES` (matched longest-first because types can contain hyphens):

- `memory-rule` — `<#memory-rule-longform#>`
- `taggroup` — `<#taggroup-mygame#>` (expands to its member tags before activation runs — see `tags.md`)
- `cantrip` — `<#cantrip-dice#>`
- `verify` — `<#verify-tone#>`
- `lore` — `<#lore-worldbuilding#>`
- `map` — `<#map-poker#>`

A type absent from `TAG_TYPES` parses as `unknown` and its tags silently match nothing — no error, no log line. This is exactly how cantrip, map, and memory-rule tags were once unable to activate anything: a new resource type must be added to `TAG_TYPES` or its tags are permanently inert.

An `owner-type-name` form (`someuser-lore-castle`) addresses another user's **public** resource by that owner's tag. Tags are stripped from message content before the request reaches the LLM.

## Selection resources: the one exception, and only to rule 4

Maps and memory rules each pick exactly one winner per request, so Active cannot also mean "apply to everything" for them — blanket-running a multi-stage map would silently multiply every request's cost, and two simultaneously-active maps would race. They use `tag_matches_resource()` (rule 3 only — a plain yes/no on whether a tag named this specific resource) plus a single explicit blanket setting instead of the Active flag:

- Maps: `user_settings.default_map_id` (the "Default Map" in Settings).
- Memory rules: the one untagged rule acts as the default fallback.

They still obey rules 1–3 unchanged; only rule 4's mechanism differs (an explicit single-valued setting instead of a per-resource Active flag). This is the one documented departure from `should_activate_resource()`, and it narrows activation rather than widening it, so it does not violate rule 1.

## Command tags are a different system

`<VERIFY:off>`, `<SUMMARY:on:persist>`, etc. (see `app/services/command_tags.py`) switch **pipeline features** on or off for a request — they do not activate a resource. They are the only place in GitInTheVan where deactivation exists: `<VERIFY:off>` genuinely turns verification off for that message, which nothing in the Activation Hierarchy can do. Do not confuse the two systems: a `<#verify-tone#>` tag activates a specific verification *rule*; `<VERIFY:off>` disables the verification *feature* regardless of which rules would otherwise have fired. Full syntax and precedence in `tags.md` and the `#command-tags` guide section.

## Map stage binding is routing, not activation

A cantrip or lorebook attached to a specific map stage (via `only_ids`/`exclude_ids` in `process_cantrips()`) has already been activated by the Activation Hierarchy; the stage binding only decides *where in the map* it runs, so it fires once rather than once per stage. Do not read stage attachment as a second activation gate.

## Why isn't my X firing? — checklist

1. **Is Active on, or is there a matching tag in the message/persona/system prompt?** Rule 4 needs one or the other (rule 3 for maps/memory rules — Active alone is not enough for those).
2. **Does the tag's type prefix match `TAG_TYPES` exactly** (`lore-`, `cantrip-`, `verify-`, `map-`, `memory-rule-`, `taggroup-`)? A typo or unsupported type parses as `unknown` and matches nothing, silently.
3. **For a public resource owned by someone else**, did you use the `owner-type-name` form? A bare `type-name` tag only matches your own resources unless the resource is public.
4. **Is a command tag suppressing the pipeline feature** (`<VERIFY:off>`, `<MEMORY:off>`, etc.), including a persisted one from an earlier message? Check the Memories page for `__cmd_persist_` entries.
5. **For a map or memory rule**, is it the one explicitly set as default (`default_map_id` / untagged rule), or does the request carry its specific tag? Active alone will not run it.
6. **Was the resource excluded by a loader filtering `is_active` before the activation check ran?** This is a code-level bug, not a configuration problem — loaders must load candidates, not decisions, or rule 3 breaks.
7. **Is this a Driver-Callable cantrip that needs `turns > 0`** in Settings, or a map stage `driver_callable_turns` (map stages do not currently wire the tool loop — see `pipeline-order.md`)?

`tests/test_activation_hierarchy.py` is the executable form of these rules and covers every resource type.

## See also

- `#activation`
- `#cantrips`
- `#lorebooks`
- `#verification`
- `#memories`
- `#maps`
- `#tags-and-groups`
- `#command-tags`

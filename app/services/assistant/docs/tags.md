# Tags

Three distinct tag-like systems live in GitInTheVan's message text. They look similar and are easy to conflate; they do different jobs.

## Activation tags vs tag groups vs command tags

**Activation tags** — `<#type-name#>` — name one specific resource (a lorebook, cantrip, verification rule, map, or memory rule) and feed the Activation Hierarchy's rule 3 (proximity wins). See `activation-hierarchy.md` for the full rule set and the `TAG_TYPES` list (`memory-rule`, `taggroup`, `cantrip`, `verify`, `lore`, `map`).

**Tag groups** — `<#taggroup-name#>` — one tag that stands in for several activation tags at once. A group is a saved collection of member lorebooks and cantrips; embedding its tag is equivalent to embedding every member's own tag. Groups are always private to their owner, cannot nest (a group cannot contain another group), and can themselves be marked Active to blanket-apply without needing the tag at all.

**Command tags** — `<COMMAND:setting>` / `<COMMAND:setting:persist>` / `<COMMAND:reset>` — switch a pipeline *feature* on or off for a request; they do not name or activate a resource at all. Commands: `VERIFY`, `SUMMARY`, `FORBIDDEN`, `MEMORY`, `DRIVER` (case-insensitive). This is the one system in GitInTheVan where deactivation genuinely exists — `<VERIFY:off>` turns verification off outright, which no activation tag can do. Three-tier precedence per request: one-off override (this message only) beats a persisted override (`:persist`, saved to conversation memory as a `__cmd_persist_` entry, applies until `<COMMAND:reset>`), which beats the GUI default setting. All command tags are stripped before the request reaches any LLM.

Note: `docs/user-guide.md`'s Activation Hierarchy section (`#activation`) describes command tags with the syntax `<#gitv-...#>`; that does not match the implementation. `app/services/command_tags.py` (`COMMAND_TAG_PATTERN`) and the guide's own `#command-tags` section agree on the real syntax, `<VERIFY:off>` etc. — use that form.

## Group expansion

Tag groups expand at pipeline entry, in `resolve_group_tags()`, before any activation decision is made — so by the time `should_activate_resource()` runs, a `<#taggroup-castlescene#>` tag has already become the individual `<#lore-...#>`/`<#cantrip-...#>` tags of its members. Expansion is deduplicated: if a resource is reachable both by its own tag and via a group in the same request, it activates once, not twice. If a member of a group has since been deleted, the resolver logs a warning and continues with the remaining members rather than failing the request.

## Endpoint role tags

Separate from resource activation tags. Every endpoint carries a **Role Tag** — `default`, `driver`, `navigator`, `writing`, `validation`, `rules`, `tool_use`, or `custom` (with a free-text **Custom Tag Name** when `custom` is selected) — plus a numeric **Priority**. Endpoints sharing a role tag form a failover chain, tried in priority order (ties break on creation order). On *any* failure — any HTTP status other than 200, or a connection error, with no retryable/non-retryable distinction — the next candidate in the chain is tried. Candidates can differ in model, API key, and provider, which is the point: a free-tier key failing over to a paid one. Only when every candidate in the chain has failed does the request return an error.

The failover chain applies to: the Driver call, map stages, and — since 0.24.0 — the verification judge call and the verification retry call. It does not currently apply inside the Driver-Callable tool loop's internal forwards.

## Where tags appear in the UI

- **Tags tab** (Tags and Groups page): every lorebook and cantrip in one table with its activation tag and Public/Private visibility toggle.
- **Groups tab** (Tags and Groups page): create/edit tag groups, pick member lorebooks/cantrips, toggle group Active.
- Per-resource cards (Lorebooks, Cantrips, Verification Rules, Maps, Memory Rules) each show and let you edit that resource's own tag directly.
- **Endpoints page**: Role Tag and Priority fields per endpoint, under Failover configuration.

## See also

- `#tags-and-groups`
- `#activation`
- `#command-tags`
- `#endpoints`
- `#cantrips`
- `#lorebooks`
- `#verification`
- `#maps`

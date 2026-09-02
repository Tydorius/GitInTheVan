"""Assistant Pane backend (Phase 26).

The engine behind the in-app assistant: an allow-list registry of tools over
the user-scoped management API, permission evaluation, an in-process executor
that reaches the existing routers with the caller's own JWT, and the turn loop
that drives the user's own configured endpoint with native function calling.

Nothing here treats the model as a security control. Every boundary is
mechanical: the registry allow-list, the permission decision taken before any
call, and the router's own ownership and content-guard checks.
"""

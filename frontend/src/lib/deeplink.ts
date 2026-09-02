/**
 * Hash-route query parsing, so a link can name an object and not just a page.
 *
 * The app has always written links like `#/cantrips?id=abc` from the Packs page,
 * but `App.svelte` dropped everything after `?` and no page ever read the query,
 * so those links opened the right page with nothing selected. This is the
 * missing half.
 *
 * Links from Debug and Compare open in a new tab, which is why they are plain
 * `<a href="#/...">` with `target="_blank"` rather than assignments to
 * `location.hash` — the comparison the user is reading has to survive.
 */

export interface ParsedRoute {
  /** Path portion, always starting with `/`. */
  page: string
  /** Query parameters, decoded. Empty object when there are none. */
  params: Record<string, string>
}

/** Split a hash route into its page and its query parameters. */
export function parseRoute(hash: string): ParsedRoute {
  const route = (hash || '').replace(/^#/, '') || '/'
  const queryStart = route.indexOf('?')

  if (queryStart === -1) {
    return { page: route, params: {} }
  }

  const params: Record<string, string> = {}
  const search = new URLSearchParams(route.slice(queryStart + 1))
  search.forEach((value, key) => {
    params[key] = value
  })

  return { page: route.slice(0, queryStart) || '/', params }
}

/** Build a hash route from a page and parameters, omitting empty values. */
export function buildRoute(page: string, params: Record<string, string | number | undefined | null> = {}): string {
  const search = new URLSearchParams()
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined && value !== null && value !== '') {
      search.set(key, String(value))
    }
  }
  const query = search.toString()
  return query ? `#${page}?${query}` : `#${page}`
}

/**
 * Route map for linking to an object by resource type.
 *
 * `tab` is a reserved query param, not a resource type: several pages (Admin,
 * Dashboard, Packs, Skills, TagGroups, Verification) read `?tab=` to select a
 * sub-tab on load, independent of any `id`. A page ignores an unrecognised
 * `tab` value rather than raising, so an old or mistyped link just lands on
 * that page's default tab.
 */
const TYPE_ROUTES: Record<string, string> = {
  cantrip: '/cantrips',
  lorebook: '/lorebooks',
  lorebook_entry: '/lorebooks',
  skill: '/skills',
  sample: '/skills',
  map: '/maps',
  map_stage: '/maps',
  verification_rule: '/verification',
  memory_rule: '/memories',
  scenario_rule: '/memories',
  endpoint: '/endpoints',
  debug_run: '/',
}

/**
 * Link to an object of a given type, or null when the type has no page.
 *
 * Returns null rather than a dead `#/` so callers can render plain text instead
 * of a link that goes nowhere.
 */
export function resourceLink(type: string, id: string, extra: Record<string, string> = {}): string | null {
  const page = TYPE_ROUTES[type]
  if (!page || !id) return null
  return buildRoute(page, { id, ...extra })
}

/**
 * Replace the current route's parameters without adding a history entry.
 *
 * Used by Compare to keep its selection and baseline in the URL. `replaceState`
 * rather than setting `location.hash`, so clicking through four baselines does
 * not put four entries in the back button.
 */
export function replaceRouteParams(page: string, params: Record<string, string | number | undefined | null>): void {
  const next = buildRoute(page, params)
  if (window.location.hash !== next) {
    window.history.replaceState(null, '', next)
  }
}

# 1.33.0: one design system for every page

The pages the controller serves (Monitor, Configure, the deployment page, the setup steps, sign-in and the OAuth consent page)
shared no look: three palettes, three kinds of card, sidebar labels that changed from page to page. This release draws them
all with one design system, `labdeploy/theme.py`, built on Apple's Human Interface Guidelines as they apply to a web page: the
system typeface with tabular numbers and a small type scale, semantic colours used for one meaning each, a grouped light
canvas with white surfaces and hairline separators, 8-point spacing, 44-point targets on a phone, and brief motion that is
off when a person asks for reduced motion.

- The sidebar is a translucent material with one vocabulary everywhere (Monitor: Overview, Deployments, Health, Releases,
  Cost, Activity; Configure: Connections, Applications, Platform, People, Production, Advanced), the current page in the
  accent colour and the signed-in identity as a small card. On a phone it becomes a bar that stays at the top.
- Cards, settings rows, fields, switches, segmented controls, chips, callouts, tables and the sign-in card share one set of
  tokens, and the charts use the same palette.
- Motion: a page rises in as it loads, cards lift under the pointer, the switch slides, the live dot ripples and the Monitor
  toolbar stays at the top of the screen while the page scrolls; `prefers-reduced-motion` turns every animation off.
- The deployment page keeps its board, track, spotlight and journey, now in the same colours and type as the rest, with its
  tools drawn as buttons.
- The OAuth consent page is the same sign-in card as the settings page.

Nothing a page does changed: the same routes, forms, class names and strict headers (no script outside the deployment page,
no web font, no image, no `url(`), and the same tests pass.

# 1.32.0: company readiness, phases 0 and 2

This release implements phases 0 and 2 of the October 23 readiness plan. Codex started the work and Claude finished it. Phase 1
(company identity) is not part of this release.

Phase 0 adds a control catalog with a dependency map, and verified secrets now block every release path whatever the
code's review status. A contract check in CI runs for every pull request, and a written production-gate specification
describes a future gate without enforcing it.

Phase 2 adds:

- an operation ledger, with intents written off-host before any dispatch;
- a scheduler whose safety work cannot be starved;
- dependency resolution in isolated workers;
- a job broker that reserves quota before a job starts;
- an opt-in supervisor that recovers an interrupted production release through GitHub.

An independent review found fifteen issues, and thirteen are fixed with tests (docs/READINESS-IMPLEMENTATION.md). Every
new part is off until configured. `resolver_local_fallback: true` keeps lockfile generation on the controller until the
worker project exists. Nothing here has been exercised live; docs/READINESS-ROLLOUT.md lists the drills.

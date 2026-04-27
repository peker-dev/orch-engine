# Web Domain — Builder Guide

You are implementing work for a **responsive web** project. Output goes to actual files in the working directory; you must do the work before you return JSON.

## Hard rules every HTML page must obey

These are non-negotiable. The functional verifier will fail the cycle if any of these are missing:

- `<meta name="viewport" content="width=device-width, initial-scale=1">` — required on every HTML page.
- `<html lang="ko">` (or the project's primary locale) — required.
- Semantic HTML elements: `header` / `nav` / `main` / `article` / `section` / `footer` — not a sea of `<div>`s.
- Every `<img>` carries an `alt` attribute. Decorative images get `alt=""`. Never omit the attribute itself.
- Heading hierarchy: exactly one `<h1>` per page; `h2` and below in sequential order, no level skipping.
- Responsive CSS: a `max-width` container plus at least one `@media (max-width: 768px)` (or equivalent) break. Hard-coded `px` widths only is a fail.
- Color contrast: WCAG AA — 4.5:1 for body text, 3:1 for large text.
- External `<script>` tags use `defer` or `async`; inline scripts kept minimal.
- Fonts: prefer system fonts. If a webfont is necessary, use one or two faces only and account for loading performance.

## Scaffold expected at the project root

Required structure (create what's missing on greenfield runs):

- `project-root.md`
- `README.md`
- `index.html` — or `src/index.{html,tsx,vue,svelte}` for SPA/SSR
- `.gitignore` — framework-appropriate (node_modules, dist, build, .vite, .next, etc.)
- `memory/handoff.md`
- `memory/coding-rules.md`

Optional structure that earns no demerits when absent: `src/components/`, `src/styles/`, `src/pages/`, `public/`, `screenshots/`, `tests/`.

Seed files for greenfield: `README.md` template; `.gitignore` keyed to the chosen framework; an `index.html` skeleton that already includes `<meta viewport>` and `<html lang>`.

## Bootstrap order on a fresh project

1. Confirm `site_type` (static / spa / ssr / portfolio).
2. Choose framework or commit to vanilla.
3. Generate the `index.html` responsive skeleton — viewport meta + max-width container + a baseline media query.
4. Draft the `README` with site purpose + local run + deploy commands.
5. Apply the framework-appropriate `.gitignore` template.

## Change scope discipline

- A new page = a new HTML file (or a new route module). Don't tack pages onto existing files.
- Edits to an existing page stay in the smallest section diff that does the job.
- Global CSS edits must explicitly name the affected pages in `change_summary`.
- Brand-element edits (logo, color tokens, primary typography) require explicit user approval — hand back via `handoff(approve_gate)` instead of editing.

## Asset rules

- Images ship responsive — `srcset` or at least a separate mobile + desktop pair.
- Screenshots stored as WebP or as size-optimized PNG/JPG. Never check in unoptimized 4K originals.
- Brand tokens live as CSS custom properties (`--color-primary`, `--font-display`). Never hard-code hex values across the codebase.

## Self-check before declaring done

Before you return your utterance, walk through:

- HTML validator clean? (W3C parser produces zero errors.)
- Resize to 375px wide — any horizontal scroll? If yes, fix before reporting done.
- Every `<img>` has `alt`? Run a grep, don't trust visual inspection.
- Heading hierarchy intact? No `h1` duplicates, no `h2`→`h4` jumps.
- `lang` attribute on `<html>`?
- Contrast pairs you used satisfy WCAG AA (4.5:1 / 3:1)?
- Tone is professional? Portfolio-grade copy avoids emotional adjectives ("amazing", "perfect", "completely").

## When to hand back instead of finishing

- Brand or design decisions you cannot make alone → `handoff(approve_gate)`.
- Large content deletion or replacement (any block of user copy) → `handoff(review_only)`.
- A framework limitation makes the goal infeasible → `handoff(replan_pass)` with the constraint named.

## Tools you'll typically reach for

Required: a modern browser (Chrome/Firefox/Edge), a W3C HTML validator (online or CLI), DOM/CSS inspector. Optional: Lighthouse CLI, axe-core, Playwright/Cypress for e2e, Figma for design references, ImageOptim/squoosh for asset compression.

## Recovery patterns

- **validator_fail** → fix only the offending tag; don't restructure the whole page.
- **a11y_fail** → add `alt`, raise contrast, add `aria-label` where appropriate.
- **responsive_broken** → check viewport meta first, then add the missing `@media` query.
- **lighthouse_low** → optimize images, apply `defer`/`async`, drop unnecessary external dependencies.
- If a framework-specific approach keeps fighting back, fall back to vanilla HTML/CSS and revisit later.

# Web Domain — Builder Guide

You implement responsive web work. Output goes to actual files. The functional verifier fails the cycle on the hard rules below.

## Critical hard rules

- **`<meta name="viewport" content="width=device-width, initial-scale=1">`** — every HTML page.
- **`<html lang="...">`** — set per the project's primary locale (default `ko`).
- **Semantic HTML.** `header / nav / main / article / section / footer` over a sea of `<div>`s.
- **Every `<img>` carries `alt`.** Decorative images get `alt=""`. Never omit the attribute.
- **Heading hierarchy.** Exactly one `<h1>` per page; h2/h3/... in sequence, no level skipping.
- **Responsive CSS.** A `max-width` container + at least one `@media` (or container-query) breakpoint. Hard-coded `px`-only widths is a fail.
- **Color contrast WCAG AA** — 4.5:1 body, 3:1 large text.
- **External `<script>`** uses `defer` or `async`; inline scripts kept minimal.

## Core Web Vitals discipline

When build/deploy is in scope, write toward LCP / CLS / INP, not just Lighthouse score:

- Hero/above-fold image: dimensions set + preloaded if it's the LCP element.
- Reserve space for images / embeds / ads to avoid CLS spikes.
- Defer non-critical JS; avoid main-thread blocking on interaction (INP).

## Project assets (binding when present)

- Design system / brand tokens (colors, typography, spacing primitives).
- Component conventions (naming, file structure, prop patterns).
- Content guidelines (voice, tone).
- Coding rules / style guide.

## Greenfield scaffold

Required: `project-root.md`, `README.md`, `index.html` (or `src/index.{html,tsx,vue,svelte}`), `.gitignore` (framework-appropriate), `memory/handoff.md`, `memory/coding-rules.md`.

Optional: `src/components/`, `src/styles/`, `src/pages/`, `public/`, `screenshots/`, `tests/`.

## Asset rules

- Images ship responsive (`srcset` or mobile + desktop pair). Optimized formats (WebP / AVIF / size-tuned PNG-JPG). Never check in 4K originals.
- Brand tokens as CSS custom properties (`--color-primary`, `--font-display`). Never hard-code hex across the codebase.

## When to hand back

- Brand or design decisions you cannot make alone → `handoff(approve_gate)`.
- Large content deletion or replacement → `handoff(review_only)`.
- Framework limitation makes the goal infeasible → `handoff(replan_pass)` with the constraint named.

## Recovery patterns

- **validator_fail** → fix the offending tag only.
- **a11y_fail** → add `alt`, raise contrast, add `aria-label` where appropriate.
- **responsive_broken** → check viewport meta first, then add the missing breakpoint.
- **lighthouse_low / cwv_low** → optimize images, apply `defer`/`async`, drop unnecessary external dependencies.

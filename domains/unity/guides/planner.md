# Unity Domain — Planner Guide

You are planning work for a **Unity project** — game, simulation, AR/VR experience, training app, digital twin, editor tooling, or any other Unity-based application. Plans must respect Unity's MonoBehaviour lifecycle, the chosen render pipeline, the build pipeline, and the target platforms. The project's specific application domain (game / simulation / industrial / training / etc.) and any external-system integration (PLC, MES, network services) are project assets, not domain assumptions.

## What this domain expects from you

Unity projects fail in characteristic ways: silent build breaks, missing script GUIDs, render-pipeline mismatches, platform-specific runtime exceptions, asset/meta desync. The bar is therefore: build always green, runtime exceptions zero, every target platform builds, and the project's target FPS and platform constraints are met. Plans should reflect those failure modes and the project's specific application context (which the project itself defines).

## Reading the intake

Identify or assume these inputs (state assumptions in `body`):

- **unity_version** — LTS preferred (e.g. `2022.3 LTS`). Mismatch with `ProjectSettings/ProjectVersion.txt` is itself a finding.
- **render_pipeline** — `URP` (general purpose, WebGL-friendly), `HDRP` (high-fidelity, no WebGL), or `BIRP` (legacy).
- **target_platforms** — at least one of `windows_standalone` / `mac_standalone` / `linux_standalone` / `webgl` / `android` / `ios` / `console`. Each platform has its own constraint pack (WebGL bans threads + `System.IO.File`; mobile has thermal/battery considerations; console has certification requirements).
- **application_kind** (project-defined) — game / simulation / training / digital twin / AR/VR / editor tool / etc. The project owns this label and its implications.
- **external_integration** (project-defined) — does the project connect to external systems (network services, hardware, PLCs, sensors)? Approval gates around such integrations are project-defined; respect them when present.
- **legacy_project_path** — when retrofit. Don't overwrite Unity version / render pipeline silently.

Auto-detect signals: `ProjectSettings/ProjectVersion.txt`, `Packages/manifest.json`, `Assets/` layout, existing `*.sln/*.csproj`, prior project documents.

## Project assets (binding when present)

If the project carries any of these, they are binding and override this guide's defaults:

- **Project's application-domain context** — what the project is actually for (game genre, simulation purpose, training scenario, etc.).
- **Project's external-integration policies** — read-only vs read/write boundaries for any external system, approval gates, safety rules.
- **Project's target performance budget** — FPS target, draw call budget, memory budget, build size cap.
- **Project's coding/asset conventions** — folder structure, naming, asset import settings.
- **Project's CI/build script entry points** — how to invoke a headless build for verification.

## Splitting the goal into tasks

Work in **feature-sized units**. One feature = one folder under `Assets/Features/{FeatureName}/` (or the project's pinned convention) with its scripts, scene fragment, prefabs, settings ScriptableObjects, and any localization keys.

- A **build breaker** (compile error, missing script, missing reference) jumps the queue regardless of feature priority — green build is the prerequisite for everything else.
- **Platform-specific work is its own task.** Don't bundle "make it work on WebGL" into a generic feature task — WebGL constraints (no threads, no `System.IO.File`, asset size) shape the implementation choices.
- **External-system integration tasks** separate the read path from the write path when both are in scope. The project may require approval gates on the write path; respect them.
- **Editor tooling tasks** (custom inspectors, importers, build scripts) are a distinct task type — they touch `UnityEditor.*` which must never leak into runtime code.

Task titles name the feature area + intent: `Inventory: drag-drop UI 추가`, not `인벤토리 작업`.

## Priority order

1. **Build breakers** (compile errors, missing script GUIDs, missing references) — green build is mandatory before any feature work.
2. **Runtime exceptions** on the target platforms.
3. **Core feature work** along the master objective.
4. **WebGL build size** when WebGL is a target — hits the cap → priority bump, reach for Addressables.
5. **Localization key gaps** (soft, but track).
6. **Polish** (UI micro-interactions, post-processing tweaks, lighting refinement).

## Acceptance you should encode in tasks

A Unity task is "done" when these hold:

- Project compiles for Editor + every target platform listed in intake.
- Console Errors / Exceptions = 0 in Editor and PlayMode for the touched scenes.
- Missing Script / Missing Reference = 0.
- Target FPS met at the declared resolution (project-defined budget).
- Build size within the agreed cap (project-defined; especially binding on WebGL and mobile).
- Localization keys present in every required locale (no empty strings).

## When to replan

- **Unity version upgrade becomes necessary** — approval gate, not a builder decision.
- **Render pipeline change** (URP ↔ HDRP) is on the table — propagates to every shader, material, and post-process.
- **A package required by the goal is not WebGL-compatible** and WebGL is in scope.
- **Target platform added or removed.**
- **External-integration spec changes** (network protocol, hardware interface, third-party service version).

## References to consult before planning

- `ProjectSettings/ProjectVersion.txt` (binding for Unity version).
- `Packages/manifest.json` (package versions; lockfile commits matter).
- Existing `Assets/_Core/` utilities — duplicate work is the most common waste.
- Existing similar features in the project — copy structural conventions, don't reinvent.
- Unity Manual + Scripting API for the version pinned in `ProjectVersion.txt`. Cite version when quoting.
- The project's own application-domain documentation (when present) for context-specific constraints.

## Things to keep your hands off of

- `ProjectSettings/ProjectVersion.txt` — version changes are an approval gate.
- `Packages/manifest.json` core packages — additions/removals/version bumps need explicit user buy-in.
- `Library/`, `Temp/`, `obj/` — must stay in `.gitignore`.
- `*.meta` files — they pair with assets. Losing or renaming a meta breaks every reference using its GUID.
- Existing scenes / prefabs on irreversible structural changes — branch into a new file rather than mutating in place.
- **External-system write paths** when the project gates them — defer to the project's approval rules rather than executing.

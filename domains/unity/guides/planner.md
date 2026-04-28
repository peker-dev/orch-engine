# Unity Domain — Planner Guide

You plan work for a Unity project — game, simulation, AR/VR, training, digital twin, editor tooling, or any other Unity-based application. Plans must respect Unity's failure modes (silent build breaks, missing GUIDs, render-pipeline mismatches) and the chosen target platforms.

## Critical Unity rules

- **Green build is the prerequisite for everything else.** A build breaker (compile error, missing script GUID, missing reference) jumps the queue regardless of feature priority.
- **Platform-specific work is its own task.** "Make it work on WebGL" is not a sub-step of a feature task — WebGL constraints (no threads, no `System.IO.File`, asset size, no heavy reflection) shape the implementation choices.
- **Unity version + render pipeline + core packages are an approval gate.** Never plan a silent upgrade.
- **Asset import settings determine build size.** Texture compression, audio compression, mesh import — plan changes to these explicitly when build size is a target.
- **Editor tooling tasks are distinct.** Anything touching `UnityEditor.*` must never leak into runtime; plan editor-only work into its own folder/assembly.

## Project assets (binding when present)

- The project's application-domain context (game genre / simulation purpose / training scenario / etc.).
- The project's coding/asset conventions (folder structure, naming, import settings).
- The project's CI/build script entry points (how to invoke a headless build).
- The project's external-integration policies (read-only vs read/write boundaries, approval gates, safety rules).
- The project's performance budget (FPS target, draw calls, memory, build size cap).

## Reading the intake

State assumptions in `body` for: unity_version (LTS preferred), render_pipeline (URP / HDRP / BIRP), target_platforms, application_kind, external_integration (when applicable).

Auto-detect from `ProjectSettings/ProjectVersion.txt`, `Packages/manifest.json`, `Assets/` layout.

## Priority

1. Build breakers (compile, missing GUID, missing reference).
2. Runtime exceptions on target platforms.
3. Core feature work along the master objective.
4. Build size when WebGL/mobile is a target and the cap is hit.
5. Localization key gaps.
6. Polish (UI micro, post-processing, lighting refinement).

## When to replan

- Unity version upgrade looks needed → approval gate.
- Render pipeline change on the table → propagates to every shader/material/post-process.
- A required package is not WebGL-compatible while WebGL is in scope.
- Target platform added or removed.

## Hands off

- `ProjectSettings/ProjectVersion.txt` — Unity version is an approval gate.
- `Packages/manifest.json` core packages — additions/removals/version bumps need explicit user buy-in.
- `Library/`, `Temp/`, `obj/` — must stay gitignored.
- `*.meta` files — losing or renaming a meta breaks every reference using its GUID.
- External-system write paths the project gates.

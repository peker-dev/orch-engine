# Unity Domain — Planner Guide

You are planning work for a **Unity industrial client** project — digital twin, factory simulator, training sim, PLC integration POC, AR industrial app, or editor tooling. The targets are typically Windows Standalone and/or WebGL, often both, and the domain is industrial automation (battery / shipbuilding / bio / pharma / general manufacturing). Plans must respect Unity's MonoBehaviour lifecycle, the chosen render pipeline, the build pipeline, and the safety rules around real industrial equipment.

## What this domain expects from you

This is not a hobby Unity project. The client typically connects to real factory data (PLC / MES / OPC-UA), runs on actual operator hardware, and may even drive industrial visualizations that inform decisions on the floor. The bar is therefore: build always green, runtime exceptions zero, target platforms all build, and **anything that could write to real PLC equipment is an explicit approval gate**, not a casual dev step.

## Reading the intake

Before splitting the goal, identify or assume these inputs (state assumptions in `body`):

- **unity_version** — LTS preferred (e.g. `2022.3 LTS`). Mismatch with `ProjectSettings/ProjectVersion.txt` is itself a finding.
- **render_pipeline** — `URP` (default for industrial — WebGL-friendly + balanced perf), `HDRP` (high-fidelity, no WebGL), or `BIRP` (legacy).
- **target_platforms** — at least one of `windows_standalone` / `webgl` / `android_ar`. WebGL has a different rules pack: no `System.IO.File`, no `Thread`, careful with reflection.
- **industry_domain** — battery / shipbuilding / bio / pharma / general. The persona of the operator and the safety implications change dramatically across these.
- **plc_protocol** (optional) — FOCAS / Modbus / Kepware / OPC-UA / none. If real PLC writes are in scope, plan an approval gate.
- **legacy_project_path** — when retrofit. Don't overwrite Unity version / render pipeline silently.

Auto-detect signals: `ProjectSettings/ProjectVersion.txt`, `Packages/manifest.json`, `Assets/` layout, existing `*.sln/*.csproj`, prior `CLAUDE.md`.

## Splitting the goal into tasks

Work in **feature-sized units**. One feature = one folder under `Assets/Features/{FeatureName}/` with its scripts, scene fragment, prefabs, settings ScriptableObjects, and (if applicable) localization keys.

- A build breaker (compile error, missing script, missing reference) jumps the queue regardless of feature priority — green build is the prerequisite for everything else.
- Platform-specific work is its own task. Don't bundle "make it work on WebGL" into a generic feature task — WebGL constraints (no threads, no `System.IO.File`, asset size) shape the implementation choices.
- PLC / MES integration tasks separate the read path from the write path. The write path always needs an explicit approval gate task.
- Editor tooling tasks (custom inspectors, importers, build scripts) are a distinct task type — they touch `UnityEditor.*` which must never leak into runtime code.

Task titles name the feature area + intent: `PLC_Reader: Modbus 연결 로직 추가`, not `PLC 작업`.

## Priority order

1. Build breakers (compile errors, missing script GUIDs, missing references) — green build is mandatory before any feature work.
2. Runtime exceptions on the target platforms.
3. Core feature work (digital twin loop, simulation core, PLC read path, scene composition).
4. WebGL build size — if it hits the size gate, treat it as a hard priority and reach for Addressables.
5. Localization key gaps (soft, but track).
6. Polish (UI micro-interactions, post-processing tweaks, lighting refinement).

## Acceptance you should encode in tasks

A Unity task is "done" when these hold:

- Project compiles for Editor + every target platform listed in intake.
- Console Errors / Exceptions = 0 in Editor and in PlayMode for the touched scenes.
- Missing Script / Missing Reference = 0.
- Target FPS met at the declared resolution (often 60fps on PC, varies on WebGL).
- WebGL compressed build size within the agreed cap (default 200MB; project-specific can be tighter).
- Localization keys present in every required locale (no empty strings).

## When to replan

- Unity version upgrade becomes necessary (e.g. a needed package requires 2022.3+). This is an approval gate, not a builder decision.
- Render pipeline change (URP ↔ HDRP) is on the table — propagates to every shader, material, and post-process.
- A package required by the goal is not WebGL-compatible and WebGL is in scope.
- PLC protocol spec changes mid-project.
- A target platform is added or removed.

## References to consult before planning

- `ProjectSettings/ProjectVersion.txt` (binding for Unity version).
- `Packages/manifest.json` (package versions; lockfile commits matter).
- Existing `Assets/_Core/` utilities — duplicate work is the most common waste here.
- Existing similar features in `Assets/Features/` — copy structural conventions, don't reinvent.
- The project's prior similar features when they match the use case — structural consistency matters.
- Unity Manual + Scripting API for the version pinned in `ProjectVersion.txt`. Cite version when quoting.

## Things to keep your hands off of

- `ProjectSettings/ProjectVersion.txt` — Unity version changes are an approval gate, never an incidental edit.
- `Packages/manifest.json` core packages — additions/removals/version bumps need explicit user buy-in.
- `Library/`, `Temp/`, `obj/` — must stay in `.gitignore`. Never commit these.
- `*.meta` files — they pair with assets. Losing or renaming a meta breaks every reference using its GUID.
- Existing scenes / prefabs on irreversible structural changes — branch into a new file rather than mutating in place.
- Real PLC / industrial equipment write commands — these are an explicit approval gate every time.

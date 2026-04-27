# Unity Domain — Builder Guide

You are implementing work for a **Unity industrial client** project. The work goes into actual `Assets/`, `Packages/`, and `ProjectSettings/` files in the working directory. Output must compile, run without exceptions, and respect the safety rules around real industrial systems.

## Hard rules every change must obey

These are non-negotiable. The functional verifier will fail the cycle if any of these break:

- Project compiles cleanly (`.csproj` set, target platforms). No compile errors.
- Console Error / Exception count = 0 in Editor and in PlayMode for the scenes you touched.
- Missing Script (red GUID) and Missing Reference (null in serialized fields) = 0.
- Every asset has its `.meta` sibling. **Never rename, move, or delete an asset without keeping the meta in sync** — losing the meta breaks every reference using that GUID.
- WebGL builds (when WebGL is a target) avoid: `System.IO.File` writes, `Thread` / `Task.Run`, heavy reflection, `Resources.Load` on large assets. Use `Application.persistentDataPath` + `PlayerPrefs` + `UniTask` instead.
- Editor APIs (`UnityEditor.*`) never leak into runtime code paths. Wrap in `#if UNITY_EDITOR` or split into `Editor/` assemblies.
- Localized strings only via `LocalizedString` or the project's localization table. **No hardcoded user-facing Korean/English strings.**
- New localization keys are added to **every** locale the project ships — empty value is itself a finding.

## MonoBehaviour lifecycle discipline

Use the lifecycle methods explicitly and consistently:

- `Awake` — own initialization, references to your own components only.
- `OnEnable` — subscribe to events, register to managers.
- `Start` — resolve external dependencies (other components, services).
- `OnDisable` — unsubscribe (mirror `OnEnable`).
- `OnDestroy` — clean up native resources, dispose tokens, release Addressables handles.

Forgetting `OnDisable` to mirror `OnEnable` is the most common source of leaked event handlers in this domain.

## Architecture defaults

- **ScriptableObject** for data-and-logic separation: settings, lookup tables, event channels. Especially good for industrial config (PLC tag maps, process step definitions).
- **UniTask** over Unity Coroutines for async work — better cancellation, exception propagation, WebGL compatibility.
- **Addressables** for asset loading instead of `Resources.Load`. Resources/ is a one-way trip to giant builds.
- **Render pipeline branching** via `#if UNITY_PIPELINE_URP` / Shader Graph variants. Don't fork material assets per pipeline unless absolutely necessary.

## Scaffold expected at the project root

Required structure (greenfield bootstrap creates these):

- `project-root.md`, `memory/handoff.md`, `memory/coding-rules.md`
- `Assets/`, `Assets/Scripts/`, `Assets/Scenes/`
- `Packages/`, `ProjectSettings/`
- `.gitignore` — Unity official template (Library/, Temp/, obj/, Builds/, *.csproj, *.sln per template)

Optional but recommended structure: `Assets/_Core/` (shared utilities), `Assets/Features/{FeatureName}/`, `Assets/Settings/` (ScriptableObjects), `Assets/Localization/`, `Tests/` (EditMode/PlayMode), `Builds/` (gitignored).

Bootstrap order on greenfield:

1. Confirm Unity LTS version, write to `ProjectSettings/ProjectVersion.txt`.
2. Pick render pipeline. URP is the default for industrial work — WebGL-compatible + perf-friendly.
3. Add core packages: `TextMeshPro`, `InputSystem`, `UniTask`, `Addressables`, `Localization`.
4. Seed `Assets/` structure (`_Core / Features / Settings / Scenes / Prefabs / Art`).
5. Apply Unity's `.gitignore` template + write `README` + `CLAUDE.md`.

## Change scope discipline

- A new feature = a new folder `Assets/Features/{FeatureName}/`. Don't sprinkle new scripts across existing feature folders.
- Public API edits to existing scripts need an `[Obsolete]` marker or a breaking-change comment naming the migration path.
- `ProjectSettings/**` edits need a comment with the rationale and a note in the cycle handoff.
- Scene merges with prefab instance overrides — review each override before committing. Stale overrides accumulate silently.

## Asset rules

- Prefab Variants for variations — don't fork the base prefab.
- Materials/Shaders verified per render pipeline. URP and HDRP have different shader graph backends.
- Localization tables: every new key gets a value in every locale. Empty string is a finding.

## Self-check before declaring done

Before you return your utterance, walk through:

- Editor Console: zero errors, zero exceptions?
- Open the touched scenes — any Missing Script (red icons) or Missing Reference (null serialized fields)?
- Build Settings include every target platform listed in intake?
- Run `Unity -batchmode -nographics -executeMethod BuildScript.BuildAll -quit` (or the project's equivalent build entry) — does it succeed for each target?
- WebGL target: `grep` your changes for `System.IO.File`, `Thread`, `Task.Run` — any hits in runtime paths?
- Localization keys you touched — every locale has a non-empty value?
- Editor-only API in runtime code — any `using UnityEditor` outside `#if UNITY_EDITOR` blocks?

## When to hand back instead of finishing

- Render pipeline incompatibility surfaces → `handoff(review_only)` with the affected scenes/materials list.
- Real PLC verification environment is missing or unsafe → `handoff(approve_gate)` with a mock-implementation scope proposal.
- Unity version upgrade looks necessary → `handoff(approve_gate)` — never upgrade silently.
- A core package needs to be added or replaced → `handoff(approve_gate)`.

## Recovery patterns

- **compile_error**: fix the offending script only; don't rewrite surrounding code.
- **missing_asset_meta**: regenerate the meta or re-import the asset; don't delete-and-recreate.
- **unity_version_mismatch**: restore `ProjectVersion.txt` to the project's pinned version; only upgrade with explicit approval.
- **package_conflict**: defer to `manifest.json` lockfile; don't free-version packages.
- **platform_incompat**: branch the affected code with `#if UNITY_WEBGL` / `#if UNITY_STANDALONE`.
- **webgl_size_exceed**: split assets via Addressables, tighten compression in player settings, drop unused packages.
- **plc_comm_fail**: drop to a mock adapter pattern; never attempt blind retries against real equipment.

## Things you must never do without approval

- Send write commands to real PLC / industrial equipment. Read-only monitoring is the default; writes are always an approval gate.
- Commit `Library/`, `Temp/`, `obj/`, `Builds/`.
- Move or delete an asset without its `.meta`.
- Hardcode user-visible strings, bypassing localization.
- Call `UnityEditor.*` API at runtime.
- Use `Resources/` for assets that should live in Addressables.
- Push a build to a customer or store without explicit approval.

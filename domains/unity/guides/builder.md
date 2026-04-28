# Unity Domain — Builder Guide

You implement Unity work. Code must compile, run without exceptions, and respect platform constraints + the project's approval rules.

## Critical hard rules

- **Compile cleanly. Console errors / exceptions = 0** in Editor and PlayMode for touched scenes.
- **Missing Script (red GUID) and Missing Reference (null serialized field) = 0.**
- **`.meta` integrity.** Never rename, move, or delete an asset without keeping the meta in sync — losing the meta breaks every reference using that GUID.
- **WebGL constraints when WebGL is a target.** No `System.IO.File` writes, no `Thread` / `Task.Run`, no heavy reflection in runtime paths. Use `Application.persistentDataPath` + `PlayerPrefs` + `UniTask`.
- **No `UnityEditor.*` in runtime paths.** Wrap in `#if UNITY_EDITOR` or split into `Editor/` assemblies.
- **Localized strings only via the project's localization table** when localization is in scope. Hardcoded user-facing strings = finding.
- **GC allocations on the hot path are a finding** when a target FPS is declared. `LINQ`, string concat in `Update`, `foreach` over collections that allocate enumerators — flag and replace.

## Lifecycle discipline

`Awake` → own init / own components only. `OnEnable` → subscribe / register. `Start` → external dependencies. `OnDisable` → unsubscribe (mirror `OnEnable`). `OnDestroy` → release native resources, dispose tokens, release Addressables handles. Forgetting `OnDisable` to mirror `OnEnable` is the most common source of leaked event handlers.

## Architecture defaults

- **ScriptableObject** for data + logic separation (settings, lookup tables, event channels).
- **UniTask** over Coroutines (better cancellation, exception propagation, WebGL-friendly).
- **Addressables** over `Resources.Load` (Resources/ is a one-way trip to oversized builds).
- **Render pipeline branching** via `#if UNITY_PIPELINE_URP` / Shader Graph variants.

## Asset rules

- Prefab Variants for variations — don't fork the base prefab.
- Asset import settings (texture compression, audio compression, mesh import) tuned per platform; default-import is a finding when build size is a target.
- Localization tables: every new key gets a value in every locale.

## Project assets (binding when present)

- Coding / asset conventions document.
- CI / headless build entry (typically `Unity -batchmode -nographics -executeMethod ... -quit`).
- Application-domain rules and external-integration approval gates.

## When to hand back

- Render pipeline incompatibility surfaces → `handoff(review_only)`.
- External-integration verification environment missing → `handoff(approve_gate)` with mock-implementation scope.
- Unity version upgrade or core-package replacement looks needed → `handoff(approve_gate)`.

## Hands off without approval

- External-system commands the project gates.
- `Library/`, `Temp/`, `obj/`, `Builds/` commits.
- Asset moves/deletes without `.meta`.
- Hardcoded user-visible strings bypassing localization.
- `Resources/` for assets that should live in Addressables.
- Customer or store build push.

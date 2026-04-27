# Novel Domain — Planner Guide

You are planning work for a **현대 판타지 장편 웹소설** project. The output is episode drafts (`원고/{N권}/{NN}화_{제목}.md`), worldbuilding documents (`설정/*.md`), and persona-council meeting logs (`회의록/`). The author has confirmed style rules (`memory/writing-principles.md`) and a 6-persona review council that must speak before finalization. Plans must respect these conventions — this is not generic fiction writing.

## What this domain expects from you

Web-novel writing for the modern-fantasy market has very specific structural rules: **one sentence per line, paragraphs ≤ 3 lines, no emotion adjectives as primary descriptor, Show-don't-tell, Anti-Drama (no abstract phrasings), worldbuilding folded into protagonist POV/action**. The 6-persona council (강서진 / 윤재혁 / 소이현 / 한도윤 / 박준영 / 이도하) reviews from six fixed angles. Plans should reflect this — every task ends in either a draft revision, a setting confirmation meeting, or a persona review pass.

## Reading the intake

Identify or assume these inputs (state assumptions in `body`):

- **genre** — `현대 판타지` / `로맨스판타지` / `먼치킨` / `무협` / etc. Genre choice constrains pacing, 사이다 cadence, and reader expectations.
- **length_unit** — `화 단위` (most common) / `권 단위`. Determines how to scope each task.
- **viewpoint** — `1인칭` / `3인칭` / `혼합`. Mixing requires explicit per-scene declaration.
- **current_progress** — which episode is the latest confirmed; new work picks up from there.
- **target_platform** (optional) — `kakaopage` / `naverseries` / `munpia` etc. Affects length-per-episode conventions.

Auto-detect signals: existing `원고/{N권}/{NN}화_{제목}.md` files, `설정/*.md` worldbuilding docs, `회의록/{YYYY-MM-DD}_{주제}.md`, `memory/writing-principles.md`.

## Splitting the goal into tasks

- **One planning unit = one episode OR one setting confirmation meeting OR one revision pass.** Don't bundle episode draft + revision in a single task — they go through different verification rhythms.
- **Chapter arcs span multiple cycles.** Plan per-episode, not per-arc. The arc emerges from sequenced episodes; trying to plan the arc in one task makes the episode drafts thin.
- **Persona council meetings are their own task type** when a setting question or plot-direction question needs the 6-persona discussion before drafting can continue.
- **Setting confirmation tasks** (`설정/*.md` updates) are distinct from episode drafts — settings change only after a recorded `회의록` decision.

Task title states the episode number + intent: `05화 각성 초고`, `03화 리듬 재조정`, `회의록: 능력 체계 정합성 검토`.

## Priority order

1. **사용자 피드백 대기 중인 화의 개정이 최우선.** A pending revision blocks downstream episodes — clear it first.
2. **아웃라인 기반 다음 순차 화** — incremental progress on the confirmed beat map.
3. **설정 충돌 감지 시 회의록 개최가 집필보다 우선.** Drafting on a contested setting risks regressing it; resolve the setting question first.
4. **긴장축 (tension thread) 배치 공백이 아웃라인 기준 3화 이상 벌어지면** — re-prioritize to insert the missing thread before continuing.

## Acceptance you should encode in tasks

A task is "done" when:

- `writing-principles.md` 전 항목 통과 (문장 위계, 1인 1행, 3행 문단, 감정어 금지 block-list, 강조기호 규약, Anti-Drama).
- `memory/project-status.md` 에 해당 화 상태 갱신.
- 세계관 / 능력 체계 / 캐릭터 정합성이 `설정/*.md` 와 일치.
- For revisions: revision count incremented in episode header + change log inside the file.
- For setting changes: a recorded `회의록` decision cited inline.

## When to replan

- **사용자 피드백으로 플롯 방향 변경 지시** — the author's call wins, replan downstream episodes around it.
- **페르소나 회의에서 설정 변경 결정** — propagate the decision to the relevant `설정/*.md` and re-evaluate downstream episodes.
- **긴장축 배치 공백이 아웃라인 기준 3화 이상** — the arc's tension is leaking; insert a tension-thread episode.
- **사이다 / 고구마 배분 원칙 어긋남** — pacing rhythm has drifted; rebalance before continuing.

## References to consult before planning

- `memory/handoff.md` — what's in flight from the prior session.
- `memory/writing-principles.md` — the canonical style rules. Binding.
- `memory/project-status.md` — episode progress + confirmed changes.
- `설정/*.md` — world / abilities / characters / timelines. Binding for in-fiction facts.
- `회의록/{YYYY-MM-DD}_{주제}.md` — the most recent meetings that affect the current work.
- The immediately preceding confirmed episode — for rhythm continuity.

## Things to keep your hands off of

- **Confirmed `원고/*.md` episodes** — never overwrite without recorded user consent. Revisions go in via revision-count increment + change log.
- **`설정/*.md`** without a recorded `회의록` decision. Worldbuilding facts are the contract.
- **`회의록/*.md`** — append-only. Past meetings are not retroactively edited.
- **`memory/writing-principles.md` and `memory/workflow-rules.md`** — modifications to the binding rules are an explicit user-approval gate, not a planner decision.
- **Confirmed plot points** — never silently regress. A confirmed character relationship, a confirmed ability ceiling, a confirmed tension-beat location — these are fixed unless an explicit `회의록` decision changes them.

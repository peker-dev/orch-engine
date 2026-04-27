# Novel Domain — Builder Guide

You write the actual episode drafts and revisions for a **현대 판타지 장편 웹소설** project. Output goes to `원고/{N권}/{NN}화_{제목}.md` files. The author's style is codified in `memory/writing-principles.md` — your output must obey it line by line, not approximately.

## Hard rules every draft must obey

These are non-negotiable. The functional verifier will fail the cycle if any of these break:

- **One sentence per line.** Multiple sentences on one line is a hard fail.
- **Paragraphs ≤ 3 lines.** A 4-line paragraph is a hard fail, no exceptions for "this one needed it".
- **Sentence hierarchy:** mix 서사 (15+ chars, connective endings) with 임팩트 (3–10 chars, terminal punch). **One 임팩트 per 문단.** Two 임팩트 in the same 문단 is a finding.
- **No emotion adjectives as primary descriptor.** The block-list (비참하다 / 슬프다 / 놀랍다 / 좋았다 / etc., maintained in `writing-principles.md`) is grep-checked. Show through action, sensory detail, or object — not through adjectives.
- **No abstract phrasings.** "눈을 읽지 않았다" / "생각하는 것 같은 눈" — these are Anti-Drama violations. Use concrete genre vocabulary.
- **Emphasis markers used per convention only:** `[ ]` for system messages, `『 』` for status/skill names, `* *` for inner monologue. Misuse (e.g. using `[ ]` for inner thought) is a hard fail.
- **Worldbuilding folded into protagonist POV/action.** Never dump world info as a paragraph. A 5+ consecutive setting-description lines stretch is the heuristic for a worldbuilding dump — a finding.
- **Character / ability names** match `설정/*.md` exactly. Drift here is regression.

## Show-don't-tell, in practice

Telling: "그는 슬펐다."
Showing: "그가 손가락을 굽혔다 폈다. 한 번. 두 번. 세 번째에서 멈췄다."

Telling: "그녀는 놀랐다."
Showing: "그녀의 컵이 책상 모서리에 닿았다. 닿기만 했다. 떨어지진 않았다."

The Show-don't-tell rule is enforced by the emotion-adjective block-list. If you find yourself reaching for an emotion adjective, you've found the spot where you need to write the action / sensory detail / object instead.

## Episode header convention

Every episode file starts with:

```
# {NN}화 {제목}

- 화 번호: {NN}
- 제목: {제목}
- 확정 아웃라인 참조: {beat id}
- 개정 회차: {0|1|2|...}
- (revision >0 인 경우) 변경 로그: {간단한 요약}
```

If a revision is reflecting a setting change, link the relevant `회의록` filename inline.

## Change scope discipline

- **New episodes = new files** under `원고/{N권}/{NN}화_{제목}.md`. **Never overwrite a prior episode.**
- **Episode revision = revision count incremented** in the header + change log inside the file. Don't reset the revision history.
- **`설정/*.md` edits** only after a `회의록` decision exists; cite the meeting log filename inline.
- **`memory/*.md` rules** are append-only. Older rules remain for audit.

## Self-check before declaring done

Before you return your utterance, walk through:

- **Sentence-hierarchy pass**: ratio of 서사/임팩트 within target? (Read writing-principles for the current target ratio.)
- **Paragraph line counts**: grep for paragraphs > 3 lines → must be 0.
- **Banned emotion adjectives**: grep against the block-list → count must be 0.
- **Banned abstract phrasings** ("눈을 읽지 않았다", "생각하는 것 같은 눈" 류) → count must be 0.
- **Emphasis-marker usage**: scan `[ ]`, `『 』`, `* *` — each used per convention?
- **Worldbuilding dump heuristic**: any 5+ consecutive setting-description lines? Refold into action/POV.
- **Character/ability name cross-check** against `설정/*.md` — any drift?
- **Outline beat alignment** — does this episode hit the planned beat? If displaced, log why.

## Persona council (when invoked)

When the cycle includes a 6-persona meeting (typically before drafting a critical episode or revising a setting):

- All 6 names appear in the `회의록` log: 강서진 (서사), 윤재혁 (설정), 소이현 (캐릭터), 한도윤 (독자시점), 박준영 (편집장), 이도하 (상업성).
- Each persona's opinion is logged from their angle. Dissent is preserved verbatim.
- The author makes the final call. Decision recorded as `결정: <decision> (사유: <reason>)`.

## When to hand back instead of finishing

- **Plot direction needs to change** → `handoff(replan_pass)` with a persona council requested.
- **Setting conflict detected** (current draft contradicts `설정/*.md`) → `handoff(review_only)` citing the conflicting setting files.
- **사용자 feedback pending** on a prior episode that gates this one → `handoff(approve_gate)`.

## Recovery patterns

- **style_violation** — rewrite only the offending lines, not the full episode. The block-list is targeted; the fix is targeted.
- **world_conflict** — re-read the relevant `설정` + the `회의록` that confirmed it. Adjust the episode minimally to comply.
- **plot_regression** — diff against the outline. Restore the missed beat.
- **pacing_broken** — adjust the sentence-hierarchy ratio in the affected `문단` only. Don't rewrite the whole episode.

## Things you must never do

- **Use banned emotion adjectives** as primary descriptors. Even when they "fit", use the action/sensory replacement.
- **Write abstract Anti-Drama phrasings.** "눈을 읽지 않았다" 류는 금지.
- **Dump worldbuilding** as a paragraph. Fold it into POV / action.
- **Overwrite a prior episode** without a change log.
- **Ignore a confirmed `회의록` rejection reason.**
- **Modify `memory/writing-principles.md`** — that's an approval gate, not your call.
- **Upload to a publishing platform** (kakaopage / naverseries / munpia). Drafts and reviews only.

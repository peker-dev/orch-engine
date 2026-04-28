# Music & Music Video Domain — Builder Guide

You produce stage deliverables — lyrics, composition prompts, melody sketches, MV storyboards, recording notes. Vocalist feasibility, sound coherence, and rights clearance are non-negotiable.

## Critical hard rules

- **Each deliverable lives under its correct stage location** per the project's folder convention. Cross-stage placement is a hard fail.
- **Vocal feasibility is annotated** for any artifact affecting singing — 음역대 / 고음 지점 / 호흡 난이도 / 발음 난이도. Annotation must be specific (named pitch, named phrase), not "괜찮음".
- **Sound coherence with the declared genre.** BPM, key, instrumentation, mix density must trace to the genre — a 발라드 with EDM mix density is a finding even if individually well-produced.
- **Voice cloning only from consented sources.** Unconsented source = `result: fail`, not iterate.
- **AI-generated artifacts carry a metadata block** at the file top: tool + version + prompt-file version + output filename + key generation parameters.
- **Rights clearance for samples / references / training data.** When commercial release is in scope, a sample without a license, an AI tool with unclear training-data terms, a reference track copied past inspiration — these are hard findings, not stylistic notes.
- **The project's review process has been observed** when one is defined — all members logged opinions; dissent preserved verbatim.

## Project assets (binding when present)

- Concept / overview document — genre / theme / language / tools / vocal strategy.
- Persona / council definition.
- Stage folder convention.
- Decision-record / final-marker convention.

## Iteration discipline

- New deliverable = new file. Never overwrite a confirmed version.
- Iteration uses `v{N+1}` filenames, not edits.
- Release / final folder is write-only after earlier stages all confirmed.

## When to hand back

- Vocal feasibility uncertain → `handoff(review_only)` citing the high-risk phrase.
- Review/council deadlock with no decision → `handoff(approve_gate)`.
- AI tool can't deliver the intent → `handoff(replan_pass)` with the limitation named.

## Hands off

- Confirmed settings (genre / theme / language / tool / vocal_strategy) without an approval-gate handoff.
- The project's persona/council roster.
- Streaming / social platform uploads — distribution is the user's call.

# Music & Music Video Domain — Human-Perspective Verifier Guide

You read lyrics, listen (when audio exists), watch (when video exists) and ask: **does it work as a song / video, does it carry the intended emotion, can the actual vocalist sing it?**

## The three core checks

- **Emotion line consistency** — the 정서 confirmed at the concept stage is alive in this artifact. Lyrics, melody, MV scenes all trace back to the same emotional center.
- **Vocalist feasibility** — high notes, breath points, phrasing rhythm are within the actual vocalist's ceiling. Not aspirational, actual.
- **Stage coherence** — the deliverable connects naturally to the immediately preceding stage's confirmed output. Lyrics ignoring meeting decisions, melody fighting the lyrics, MV contradicting the song's tone — all coherence breaks.

If all three hold, review is positive. If any breaks, the cycle isn't done.

## MV-specific axis

When the artifact is the MV: **visual language consistency** across scenes — color palette, framing, motion grammar, lighting key. Scenes individually competent but visually disconnected = a finding.

## Reading angles

If the project defines its own roster, use it. Otherwise universal angles: **lyric** (emotion / metaphor / narrative progression), **melody** (contour vs lyrics, hook memorability), **production** (sound choices vs declared genre), **vocal** (singability for this vocalist), **audience** (first-listen impression, repeat-listen pull).

Name which angle (or persona) surfaced each finding.

## Quality rubric

- **A** — All three core checks hold + (when MV) visual language consistent.
- **B** — One axis needs minor tweak (반 음 낮추기, one verse line emotionally off, one MV scene disconnected).
- **C** — Two or more axes weak.
- **reject** — Confirmed theme abandoned, vocal infeasible at actual ceiling, cross-stage leakage, voice cloning from unconsented source.

## Approval rules

- C or below → `result: "needs_iteration"`.
- Vocal infeasible / theme abandoned → `result: "fail"`.
- Voice cloning from unconsented source → `result: "fail"` (consent violation, not quality).
- A grade with reviewer/angle consensus + final-decision marker → `result: "pass"`.

## Compare against the master objective, not just the active task

If the master objective is "한 곡 전체 + MV 5씬" and only chorus + 2 scenes are finalized, surface the coverage gap even when the finalized parts are A-grade.

## Common failure modes

- "All reviewers spoke" but most agreed in one line — discussion was thin.
- "괜찮음" vocal feasibility annotation with no named pitch.
- Verse 2 reuses verse 1's metaphor — narrative didn't progress.
- AI prompt v3 didn't add anything substantive over v2 — iteration was cosmetic.
- MV scene 4 contradicts the song's emotional arc at that timestamp.
- A vocal-difficulty concern logged but the final decision didn't address it.

## What you do not do

You read, listen, watch, report. Never propose changes to confirmed settings yourself. Never resolve reviewer dissent.

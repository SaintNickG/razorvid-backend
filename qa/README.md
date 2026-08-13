# RazorVid QA Kit

Use this kit to run repeatable first-session functionality checks against the multicam backend.

## Quick Start

1. Run environment and integration smoke checks:
   make qa-smoke
2. Create a session report from templates:
   make qa-init-session
3. Execute manual test cases from qa/first_session_checklist.md.
4. Record outcomes in qa/reports/<timestamp>/results.csv and notes.md.

## Files

- qa/first_session_checklist.md
  Structured test plan for 2-camera, 3-camera, mismatched lengths, and no-audio edge cases.
- qa/templates/results_template.csv
  Row-based pass/fail tracker for each test.
- qa/templates/session_notes_template.md
  Narrative notes, defects, and decision log.

## Exit Criteria (Alpha Functionality)

- All critical tests pass:
  - T01 2-camera happy path
  - T02 3-camera staggered start
  - T03 mismatched clip durations
  - T04 no-audio validation fail-fast
- No unhandled exceptions in backend logs.
- Failures are actionable (clear status + error reason).

# First QA Session Checklist

## Session Metadata

- Session date:
- Tester:
- App version/branch:
- Environment (local/aws):
- API base URL:

## Preflight

- [ ] API is running and /health returns status=ok.
- [ ] make qa-smoke passes.
- [ ] Test input files are prepared and labeled by camera.

## Test Cases

### T01: Two-camera happy path

Inputs:
- 2 clips, 20-60s, both with valid audio, same event

Steps:
1. Upload both clips.
2. Submit render with interval strategy.
3. Poll until terminal status.
4. Download output.

Expected:
- Status transitions to COMPLETE.
- Output plays with continuous audio/video.
- Angle switches occur around configured interval.

Pass/Fail:
- [ ] Pass
- [ ] Fail
Notes:

---

### T02: Three-camera staggered starts

Inputs:
- camA starts at t0
- camB starts about +1 to +2s
- camC starts about -1 to -3s equivalent relative content

Steps:
1. Upload all 3 clips.
2. Submit render.
3. Review beginning and transitions.

Expected:
- Pipeline computes offsets.
- No black gaps due to unavailable early/late segments.
- Timeline selects only currently valid angles.

Pass/Fail:
- [ ] Pass
- [ ] Fail
Notes:

---

### T03: Mismatched clip lengths

Inputs:
- camA about 45s
- camB about 30s
- camC about 18s

Steps:
1. Upload all clips.
2. Submit render.
3. Review end-of-timeline behavior.

Expected:
- Short clips drop out gracefully.
- Job reaches COMPLETE.
- No trim/concat crash near the end.

Pass/Fail:
- [ ] Pass
- [ ] Fail
Notes:

---

### T04: No-audio edge case

Inputs:
- 1 valid clip with audio
- 1 clip without an audio stream

Steps:
1. Upload both clips.
2. Attempt render.

Expected:
- Validation fails fast.
- Error explicitly states audio is required for sync.
- Job/request does not hang.

Pass/Fail:
- [ ] Pass
- [ ] Fail
Notes:

---

### T05: Noisy audio tolerance

Inputs:
- 2 clips with noisy/crowd-heavy audio

Steps:
1. Upload clips.
2. Submit render.
3. Evaluate sync and cut usability.

Expected:
- Job still reaches COMPLETE.
- Sync may degrade slightly but remains watchable.

Pass/Fail:
- [ ] Pass
- [ ] Fail
Notes:

## Final Decision

- [ ] Alpha functional gate passed
- [ ] Alpha functional gate failed

Reasons:
- 

Top follow-up actions:
1. 
2. 
3. 

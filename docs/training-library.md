# RazorVid Training Library

The training library is a curated collection of legally usable multicam media,
cut annotations, and derived signal metadata. It is not a general-purpose web
scrape. Every asset must be traceable to a source and license before it enters
the training or evaluation split.

## Dataset layers

1. **Raw media**: source video, audio, or image stored outside Git in versioned
   object storage.
2. **Manifest**: one JSONL row per asset. It records the source URL, SPDX
   license identifier, license URL, retrieval date, attribution, consent status,
   split, and storage URI.
3. **Annotations**: human-reviewed cut intervals with the selected angle,
   reason, confidence, and optional reference profile.
4. **Derived features**: motion, audio energy, beat locations, subject/frame
   quality, and camera-view signatures. These can be regenerated and should not
   replace the raw manifest or annotations.

## Acceptable sources

Use sources whose terms explicitly permit the intended use, such as:

- public-domain or government media with verified reuse terms;
- Creative Commons media whose specific license permits commercial model
  training, with attribution and share-alike obligations tracked;
- RazorVid customer footage where written consent covers model training;
- synthetic or internally generated multicam scenes.

Do not ingest a URL merely because it is publicly viewable. Exclude sources with
unknown terms, non-commercial-only restrictions when training a paid product,
personally identifying footage without consent, or licenses incompatible with
the intended model distribution.

## Manifest format

Use `training_library.TrainingExample` and validate with:

```bash
python scripts/validate_training_manifest.py path/to/manifest.jsonl
```

Each row must include:

- stable `example_id` and `media_uri`;
- `split`: `train`, `validation`, or `test`;
- `provenance.source_url`;
- `provenance.license_spdx` and `provenance.license_url`;
- ISO `provenance.retrieved_at`;
- consent/usage status and attribution where applicable;
- non-overlapping, time-bounded cut annotations for supervised examples.

## Collection order

Build the library in this order:

1. Collect source records and verify licenses manually.
2. Ingest media and record hashes, dimensions, duration, and audio presence.
3. Create human reference cuts for representative events: cheer, sport,
   concert, dance, wedding, and spoken word.
4. Add hard cases: lifted performers, occlusion, low light, crowd reaction,
   portrait/landscape mismatch, missing audio, and late camera starts.
5. Split by recording session, creator, and event rather than random frames so
   near-duplicate footage cannot leak between train and test.
6. Generate derived signals and compare AI cuts against human annotations.

The next model-facing milestone is a benchmark report containing cut timing
error, selected-angle accuracy, action-peak recall, and framing quality. A model
should not be promoted based only on visual similarity or raw render success.
# Input Matrices

> **Normative authority:** `SKILL.md` is canonical for verdict semantics, retry rules, hard invariants, and completion requirements. This reference only elaborates procedures/examples and must not override `SKILL.md`.

Use these as probe-generation patterns, not universal requirements. Contract first.

## Image/file upload matrix

### Contract-valid family

If the product claims these formats, sample multiple distinct files from each applicable class:

| Family | Suggested variants |
|---|---|
| JPEG/JPG | `.jpg`, `.jpeg`, different dimensions/quality, EXIF/no EXIF |
| PNG | RGB, RGBA/transparency, indexed where relevant |
| GIF | static and animated if animation is claimed |
| WebP | lossy/lossless; animated only if claimed |
| TIFF/TIF | `.tif`/`.tiff`, different compression when relevant |
| SVG | simple valid SVG; embedded-resource behavior only if contract defines it |
| EPS | valid simple EPS if explicitly claimed |

Do not mark unsupported formats as failures simply because they are common image formats.

### Filename/metadata variation

- spaces,
- Unicode filename,
- uppercase/mixed-case extension,
- multiple dots,
- no extension when supported,
- accurate MIME,
- missing MIME,
- MIME/extension mismatch.

### Invalid family

Representative non-images:

- TXT / Markdown,
- JSON / CSV,
- source code (`.py`, `.js`, `.ts`, `.sh`, etc.),
- PDF,
- DOCX,
- PPTX,
- ZIP or other archive.

Expected behavior is usually explicit rejection. If the feature returns success, verify whether it actually stores/processes the content and whether downstream rendering/consumption breaks.

### Structural invalidity

- zero-byte file,
- truncated image,
- random bytes with image extension,
- valid image renamed to non-image extension,
- non-image renamed to image extension.

Use benign files only; this matrix is for functionality falsification, not exploit payload delivery.

### Boundaries

Use documented thresholds where available:

- tiny but valid image,
- near maximum byte size,
- maximum byte size,
- just above maximum,
- minimum/maximum dimensions,
- unusual but valid aspect ratio.

Avoid giant-resource probes that could cause denial of service.

### Causal and round-trip checks

For two different valid files `A` and `B`:

1. upload A,
2. upload B,
3. record returned IDs/URLs,
4. retrieve/list/render both,
5. compare content identity or expected transformations,
6. ensure A and B did not collapse to one canned artifact unless deduplication is explicitly documented.

Useful evidence:

- input SHA-256,
- returned ID/URL,
- retrieved SHA-256 or perceptual/semantic transformation note,
- storage/object metadata,
- UI screenshot after reload.

## CRUD feature matrix

For create/update/delete workflows:

- create A -> read A,
- create B with different values -> read B,
- update A only -> verify B unchanged,
- delete A -> verify A absent and B present,
- repeat delete -> verify documented idempotency/error,
- restart -> verify persistence if claimed.

## Search/filter matrix

- two meaningfully different queries,
- empty query,
- filter toggles one at a time,
- combination of filters,
- no-result query,
- pagination/next page,
- sort direction,
- causal comparison: output should respond to query/filter changes.

Static identical outputs are a hardcoding signal.

## Export/download matrix

- export minimally populated data,
- export multiple distinct records,
- change source data then re-export,
- verify file exists/download completes,
- parse/open artifact where feasible,
- verify exported contents correspond to current source state rather than fixture data.
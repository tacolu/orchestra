# TGD-Core 0.1.0 — Hardening (non-engine)

Harden the Rust tachograph parser library for release. Focus on tooling, docs, tests, and lint — NOT on compliance engine logic.

## Context

- **Repo:** `workspace/tgd-core-0.1.0`
- **Language:** Rust 2024, PyO3 for Python bindings
- **Size:** ~17K lines, 17 modules, 135 existing tests, 19 examples
- **Domain:** Digital tachograph card/VU file parsing + EU 561/2006 driving time compliance

### CRITICAL: Do NOT touch compliance engines

The PC1, PC2, and EU561 engines (`pc1_engine.rs`, `pc2_engine.rs`, `eu561_engine.rs`) and the activity timeline (`activity_timeline.rs`) are **frozen**. They have known divergences from the Java GloboFleet reference implementation. A separate rewrite effort (`tgd-core-v2`) is underway on a different machine to address this from the raw parsing layer up (Sprints 50-53). Patching the existing engines — including the 3 TODOs in PC1 sub-paso 8 — is explicitly NOT the goal here. Those TODOs exist because the activity input itself diverges from Java; fixing the downstream logic without fixing the upstream data is wasted work.

**Do NOT modify:** `pc1_engine.rs`, `pc2_engine.rs`, `eu561_engine.rs`, `activity_timeline.rs`, `tolerances.rs`

**Safe to modify:** everything else (parsers, file handling, signatures, types, python bindings, build config, docs)

## Objectives

### O1 — CI pipeline

No CI exists. Create `.github/workflows/ci.yml`:
- `cargo test` on push/PR to main
- `cargo clippy -- -D warnings`
- `cargo fmt --check`
- Matrix: stable + nightly Rust
- Optional: `maturin build` for Python wheel (feature `python`)

**Acceptance criteria:**
- CI badge in README
- All checks green on current code

### O2 — README

No README exists. Write one covering:
- What it is (1 paragraph)
- Supported file formats (.tgd, .ddd, .dtg, .c1b, .v1b)
- Supported card types (driver, company, workshop, control)
- Supported generations (Gen1, Gen2, Gen2v2)
- The 3 compliance engines (PC2, PC1, EU561) with regulation references
- Note that compliance engines are in active development and may diverge from reference implementations
- Rust usage example (parse card → analyze)
- Python usage example (via PyO3/maturin)
- Building instructions (`cargo build`, `maturin develop`)
- Example commands (`cargo run --example parse_card -- file.tgd`)

**Acceptance criteria:**
- README.md exists at repo root
- Includes code examples that compile/run against current API

### O3 — Python binding tests

`src/python.rs` exposes 20+ functions but has zero unit tests. The tests should validate:
- `parse_card()` returns expected dict structure
- `parse_vu()` handles Gen1/Gen2/Gen2v2
- `detect_file_type()` distinguishes card vs VU
- `verify_signatures()` returns validation result
- Error handling: invalid bytes, truncated files, unknown formats

Use test card/VU data from existing examples or construct minimal binary fixtures.

**Acceptance criteria:**
- At least 10 new tests in a `tests/` directory or inline in `python.rs`
- Tests runnable with `cargo test --features python`

### O4 — Clippy clean

Run `cargo clippy -- -D warnings` and fix all lints. Current code was written during rapid Java port and likely has:
- Unnecessary clones
- `as` casts that could use `.into()`
- Unused imports
- Missing `#[must_use]` on pure functions

**Important:** fix lints in ALL files including engines, but do NOT change logic — only cosmetic/lint fixes (rename unused vars, remove dead imports, fix type casts). If a clippy lint requires a logic change in a frozen engine file, suppress it with `#[allow()]` and a comment explaining why.

**Acceptance criteria:**
- `cargo clippy -- -D warnings` exits 0
- No logic changes in engine files

### O5 — Parsing layer hardening

The raw parsing modules (`tgd_parser.rs`, `tgd_file.rs`, `tgd_card_data.rs`, `vu_file.rs`, `vu_records.rs`, `signature.rs`) are the stable foundation. Add edge-case tests:
- Truncated files (EOF mid-EF)
- Empty EFs (zero-length data)
- Unknown EF IDs (graceful skip)
- Corrupted cyclic buffer in EF 0x0504
- VU files with mixed Gen1/Gen2 blocks
- Signature validation with tampered data

**Acceptance criteria:**
- At least 12 new tests across parsing modules
- `cargo test` passes with all new + existing tests
- No panics on malformed input (return Result::Err)

## Technical constraints

- Pure Rust, no new external dependencies
- Do NOT change the public API in `lib.rs` or `python.rs` signatures
- Do NOT modify compliance engine logic (PC1, PC2, EU561, activity_timeline, tolerances)
- All EF parser output must remain byte-compatible with existing examples
- Tests must not require real tachograph card files — use constructed binary fixtures

## Task breakdown suggestion

| Code | Objective | Priority | Est. min | Model |
|------|-----------|----------|----------|-------|
| TGD-001 | GitHub Actions CI pipeline | O1 | 30 | medium |
| TGD-002 | README.md with examples | O2 | 45 | medium |
| TGD-003 | Python binding tests (10+ tests) | O3 | 60 | medium |
| TGD-004 | Clippy lint cleanup (cosmetic only) | O4 | 45 | small |
| TGD-005 | Parsing edge-case tests (12+ tests) | O5 | 60 | medium |

## Blocker dependencies

- All tasks are independent — can run in parallel
- TGD-004 (clippy) should ideally run after TGD-003 and TGD-005 (new code should also be lint-clean)

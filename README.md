# Blast Radius — test selection for sqlglot

Entry for the RWX **Blast Radius** track: given a patch to sqlglot at
`eb11aec4be8cb2122afe713a4816662c3f0fb2fa`, print which tests to run and how to
split them across up to 8 workers. A commit costs its busiest worker's recorded
seconds plus 2 s per worker; a missed failing test costs 300 s; our own CPU is
added.

## How it works

1. **Maps built offline from the pinned commit** (`maps/`). Every one of the 1620
   tests was run alone in a fresh process, under the labeling image and
   `requirements.lock`, with a `sys.monitoring` + audit-hook tracer
   (`tools/br_trace.py`). For each test we kept:
   * the lines it executed inside functions (import-time lines kept apart),
   * the sqlglot classes it instantiated (with their MRO),
   * the modules it had loaded and the files/directories it opened.

   `tools/build_maps.py` inverts this into bitmask maps
   (line → tests, class → tests, module → tests, file → tests,
   identifier → tests). `tools/dialect_components.py` records which tokenizer,
   parser and generator classes each dialect wires in, because those classes are
   often read through the dialect without being instantiated. No map is keyed by
   patch or by kind of edit.

2. **Static analysis per patch** (`br/patch.py`, `br/analyze.py`). We apply the
   diff in memory, parse old and new sources with `ast`/`tokenize`, and map every
   edited region to its scope:
   * comment-only change (identical token stream): only tests that read the file
     as data;
   * statement inside a function or a lambda: tests that executed it (for an
     insertion, the statements around it);
   * `def` header or decorator: tests that ran the function;
   * class body (attributes, dict entries): tests using the class, a subclass, or
     a dialect that wires it in;
   * other module-level edits: tests naming the assigned names, or every test
     that loads the module;
   * a file that no longer parses: every test that loads it;
   * fixtures and other data: tests that opened the file (or listed its
     directory, for adds and removals).

3. **Run or list.** For each commit, `br/select.py` prices these options and takes the
   cheapest:
   * **A**: run the candidates here and list only the ones that fail;
   * **B**: list every candidate without running anything;
   * **H**: run the candidates that are cheap to run and list the rest unrun;
   * **G**: run every candidate except the two giants (`test_execute_tpcds`,
     `test_max_nodes`) and list those only if another candidate fails.

   Per-test CPU costs measured in this runner are in `maps/cpu.json`. Running is billed
   as CPU. Listing is billed as the busiest worker after packing, and the optimizer
   tests are about 10x cheaper to list than to run. A commit whose only edits turn
   `is None` into `== None` (or the same against an enum member) is guarded by its cheap
   candidates only.

4. **In-container runner** (`br/runner.py`). The parent pre-imports pandas, duckdb and
   numpy, then forks one child per commit. The child imports sqlglot from a symlink
   overlay of `/opt/sqlglot` in `/tmp`, where only patched files are real, and runs the
   tests with plain `unittest`.
   * The spawn process pools in sqlglot's tests are replaced by an in-process pool with a
     pickle round trip (`br/syncpool.py`). BLAS and duckdb are held to one thread
     (duckdb 2.0 otherwise starts 256 I/O threads per connection).
   * Failures follow the labels: failures, errors, failing subtests and timeouts count.
     An import error fails the whole file; a `setUpClass` error fails the whole class; a
     test that never reports is counted as failed.
   * **Import probe.** When an edit runs at import time (module or class bodies, `def`
     headers, functions called while importing), the child first imports sqlglot, the
     changed modules and every test module, and fails whatever no longer imports.
   * **Symbolic check for `test_execute_tpcds`** (`br/tpcds.py`). The test costs 21 s to
     list and 34 s to run. Per query it parses, optimizes, plans, executes and compares
     with duckdb. When a patch only touches code the test runs while parsing, optimizing
     or transpiling, its outcome can change only if some query's optimized AST or duckdb
     SQL changes. Those are fingerprinted from the pinned commit at image build and
     recomputed under the patch (3 s): no difference means the test passes, a few
     differing queries are run for real, many means the test is listed.
   * The run never leaves a commit unanswered: fork failures, memory overruns and the
     wall-clock deadline all fall back to listing.

5. **Packing.** The listed tests are packed with longest-processing-time first, for every
   worker count from 1 to 8, minimising busiest worker + 2 s x workers.

## Validation

`tools/mutants.py` generates patches like the sample's (operator and constant flips,
class-attribute edits, fixture edits, comments, multi-file) plus other kinds (test-file
edits, dropped statements, changed defaults, deleted/renamed/added files, syntax errors,
removed imports, config edits). `tools/ground_truth.py` runs sqlglot's full suite with
pytest on each, as the labels were made. On the 30 sample commits and 539 generated
patches the selector misses no failing test. These patches are used only to check the
selector; nothing looks answers up by patch.

## Layout

| Path | What |
| --- | --- |
| `Dockerfile` | Labeling base image, `requirements.lock`, sqlglot at the pinned commit (GitHub archive), byte-compiled. |
| `main.py`, `br/` | The selector that runs in the scorer. |
| `maps/` | Precomputed maps (built from the pinned commit only). State and TPC-DS fingerprints are generated during the image build. |
| `tools/` | Offline tooling: tracers, map builder, measurement, mutant generation, ground truth, local evaluation. |
| `sample/`, `score.py`, `Makefile`, `SCORING.md` | The organisers' starter kit, for local scoring. |

Local check: `make sample` (or `tools/run_sample.sh` with rootless podman).

## Open-source code used

* [sqlglot](https://github.com/tobymao/sqlglot) (MIT): the system under test, downloaded during the build.
* Python standard library; the packages in `requirements.lock` (pytest, duckdb, pandas, numpy, …), which are installed only so sqlglot's own tests can run.
* The starter kit's `score.py`, `Makefile`, `SCORING.md` and `sample/` from the organisers.

Everything else was written for this event.

## License

MIT — see [LICENSE](LICENSE).

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

3. **Run or list.** For each commit, `br/select.py` prices three options and
   takes the cheapest:
   * **A**: run the candidates here and list only the ones that fail;
   * **B**: list every candidate without running anything;
   * **H**: run the cheap candidates and list the rest unrun.

   Per-test CPU costs measured in this runner are in `maps/cpu.json`.
   Running is billed as CPU. Listing is billed as the busiest worker after
   packing, and the optimizer tests are about 10× cheaper to list than to run.

4. **In-container runner** (`br/runner.py`). The parent pre-imports pandas,
   duckdb and numpy, then forks one child per commit. The child imports sqlglot
   from a symlink overlay of `/opt/sqlglot` in `/tmp`, where only patched files
   are real, and runs the tests with plain `unittest`.
   * The spawn process pools in sqlglot's tests are replaced by an in-process
     pool with a pickle round trip (`br/syncpool.py`). BLAS and duckdb are held
     to one thread.
   * Failures follow the labels: failures, errors, failing subtests and timeouts
     count. An import error fails the whole file; a `setUpClass` error fails the
     whole class.

5. **Packing.** The listed tests are packed with longest-processing-time first,
   for every worker count from 1 to 8, minimising busiest worker + 2 s × workers.

## Layout

| Path | What |
| --- | --- |
| `Dockerfile` | Labeling base image, `requirements.lock`, sqlglot at the pinned commit (GitHub archive), byte-compiled. |
| `main.py`, `br/` | The selector that runs in the scorer. |
| `maps/` | Precomputed maps (built from the pinned commit only). |
| `tools/` | Offline tooling: tracer, map builder, measurement, local evaluation. |
| `sample/`, `score.py`, `Makefile`, `SCORING.md` | The organisers' starter kit, for local scoring. |

Local check: `make sample` (or `tools/run_sample.sh` with rootless podman).

## Open-source code used

* [sqlglot](https://github.com/tobymao/sqlglot) (MIT): the system under test, downloaded during the build.
* Python standard library; the packages in `requirements.lock` (pytest, duckdb, pandas, numpy, …), which are installed only so sqlglot's own tests can run.
* The starter kit's `score.py`, `Makefile`, `SCORING.md` and `sample/` from the organisers.

Everything else was written for this event.

## License

MIT — see [LICENSE](LICENSE).

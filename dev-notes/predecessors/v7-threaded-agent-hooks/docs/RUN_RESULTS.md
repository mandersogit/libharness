# Run Results

## Agent hook pass

```text
$ python -m ruff check .
All checks passed!

$ python -m mypy
Success: no issues found in 11 source files

$ python -m pyright
0 errors, 0 warnings, 0 informations

$ python -m pytest -q
.........s.........                                                      [100%]
18 passed, 1 skipped in 17.08s

$ python -m compileall -q src tests
# passed
```

The skipped test is the optional real-Pi integration test gated by `PI_CLI`.

New hook tests cover:

- `__init_subclass__` rejection when both hook colors are defined for one event;
- `__init_subclass__` rejection for unsupported event suffixes;
- async hooks running on the runtime loop thread;
- sync hooks running on the dedicated hook executor rather than the loop thread;
- clearing an inherited async hook and replacing it with a sync hook;
- strict-mode `UnhandledEventError`;
- additive dispatch alongside lower-level event subscribers;
- `set_model` using the `modelId` RPC wire shape.

## Thread-owned synthesized package

```text
$ python -m pytest -q
..s........                                                              [100%]
10 passed, 1 skipped in 5.86s
```

## Earlier synthesized package

```text
python -m pytest -q
6 passed, 1 skipped in 1.38s

PI_CLI=/mnt/data/pi_pkg/node_modules/@earendil-works/pi-coding-agent/dist/cli.js python -m pytest -q
7 passed in 2.55s
```

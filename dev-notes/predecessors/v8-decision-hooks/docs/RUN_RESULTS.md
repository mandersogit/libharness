# Run Results

## v8 decision-hook pass

```text
$ python -m ruff check .
All checks passed!

$ python -m mypy
Success: no issues found in 11 source files

$ python -m pyright
0 errors, 0 warnings, 0 informations

$ python -m pytest -q
...........................s.........                                    [100%]
36 passed, 1 skipped in 8.03s

$ python -m compileall -q src tests examples
# passed
```

I also ran a local line-length scan over `src/**/*.py` and `tests/**/*.py`; no Python source/test line
exceeded 100 columns after fixes.

New v8 tests cover:

- sync and async decision hook dispatch;
- tool-call blocking shape, session-before cancellation shape, and provider-payload replacement shape;
- channel separation between bridge decision events and `PiRpcClient.on_event()`;
- decision hook exception logging and bridge failure;
- timeout configuration in the manifest;
- cancellation when the bridge closes mid-decision;
- bridge drop mid-decision without deadlock;
- concurrent decision-event serialization through the one-worker hook executor;
- same-event observation followed by decision;
- fire-and-forget `notify_event` dispatch;
- strict mode across all 18 notification events;
- notification hook exception isolation;
- Pi source taxonomy regression when the Pi source tree is available;
- `AgentEvent.payload` shallow immutability.

The skipped test remains the optional real-Pi integration test gated by `PI_CLI`.

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

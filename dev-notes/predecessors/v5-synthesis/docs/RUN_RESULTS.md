# Run Results

## Synthesized package

```text
$ cd /mnt/data/pi_synthesis/pi-python-harness-synthesis
$ python -m pytest -q
..s....                                                                  [100%]
6 passed, 1 skipped in 1.38s
```

With real Pi installed from the published package:

```text
$ PI_CLI=/mnt/data/pi_pkg/node_modules/@earendil-works/pi-coding-agent/dist/cli.js python -m pytest -q
.......                                                                  [100%]
7 passed in 2.55s
```

## Generated attempts

| Attempt | Result |
|---|---|
| A | Plain pytest failed import collection; `PYTHONPATH=src python -m pytest -q` passed 5 tests. |
| B | Plain pytest failed import collection; `PYTHONPATH=src python -m pytest -q` passed 7 tests. |
| C | Plain pytest passed 3 tests and skipped 2 optional real-Pi tests. |
| D | Plain pytest passed 3 tests and skipped 2 optional real-Pi tests. |

## Pi runtime check

The published `@earendil-works/pi-coding-agent@0.74.0` package was installed and its CLI returned version `0.74.0`. A `get_state` command in RPC mode returned a successful response with a session ID and `isStreaming: false`.

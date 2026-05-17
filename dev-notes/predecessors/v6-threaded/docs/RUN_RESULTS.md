# Run Results

## Thread-owned synthesized package

```text
$ cd /mnt/data/pi_synth_work/pi-python-harness-synthesis
$ python -m pytest -q
..s........                                                              [100%]
10 passed, 1 skipped in 5.86s
```

New tests cover:

- the dedicated asyncio loop thread is not `MainThread`;
- one test-mode harness can live on `MainThread` with `threaded=False`;
- two normal harnesses have distinct owner threads and a shared loop thread;
- multiple registries and bridge servers execute tools through the shared thread pool.

## Earlier synthesized package

Before the threaded reshaping, the async-first synthesized package had these results:

```text
python -m pytest -q
6 passed, 1 skipped in 1.38s

PI_CLI=/mnt/data/pi_pkg/node_modules/@earendil-works/pi-coding-agent/dist/cli.js python -m pytest -q
7 passed in 2.55s
```

The current threaded package retains the optional real-Pi integration test, but this rerun did not reinstall the Pi npm package in the sandbox.

## Generated attempts

| Attempt | Result |
|---|---|
| A | Plain pytest failed import collection; `PYTHONPATH=src python -m pytest -q` passed 5 tests. |
| B | Plain pytest failed import collection; `PYTHONPATH=src python -m pytest -q` passed 7 tests. |
| C | Plain pytest passed 3 tests and skipped 2 optional real-Pi tests. |
| D | Plain pytest passed 3 tests and skipped 2 optional real-Pi tests. |

# threads-rewrite bisect-baseline

Machine-readable per-commit log of pytest collection state across the
threads-rewrite feature branch. Format:

```text
# commit-sha<TAB>test-count<TAB>sha256(sorted pytest nodeids)
```

The phase-4 Makefile target `threads-rewrite-phase4-guard` (added in phase 4)
will compare current `HEAD` against the last row in this file. Pre-commit hook
(also phase 4) enforces append-on-commit.

Initial row is the pre-rewrite baseline: current `main` HEAD before any
threads-rewrite phase commit. Phase commits append their own row.

```text
257247a0530a2037a19646e5dee7d95f36f7db89	14	a329639e9917f3627e3d6d963e22228c3e4b50de27a5715fcfe30d2e057737de
```

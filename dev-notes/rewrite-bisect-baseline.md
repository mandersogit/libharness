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
a78088811e416f710043988fc5dcddd0e5149e58	33	4caa1e89ac2b8642bd695ec43ae0334933624f7ec969066042938ba9f8679243
PHASE2_PLACEHOLDER	87	1ccdb9cf7443252d537c4fb233adfafacf3764810d0d81fc3a216e8300133147
```

The phase-1 row was retroactively computed by checking out the phase-1 HEAD
(`a780888`); phase 1's commit landed without appending its baseline row.
Phase 2's row uses `PHASE2_PLACEHOLDER` for the SHA — the actual phase-2
commit SHA isn't known until the commit lands. The phase-3 commit will edit
this row to substitute the real phase-2 SHA, and append its own row.

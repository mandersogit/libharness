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
30c45608fff8a2dfd4f619cd6c4640d73255259a	87	1ccdb9cf7443252d537c4fb233adfafacf3764810d0d81fc3a216e8300133147
PHASE3_PLACEHOLDER	129	a360f476d72991934c264a175d4062a364b100377f511947366885f100060cd5
```

Phase-1 row was retroactively computed by checking out the phase-1 HEAD.
Phase-2 row's SHA was substituted in the phase-3 commit (now `30c45608...`).
Phase-3 row uses `PHASE3_PLACEHOLDER` until the phase-3 commit lands;
phase-4's commit will substitute the real phase-3 SHA and append its own
row.

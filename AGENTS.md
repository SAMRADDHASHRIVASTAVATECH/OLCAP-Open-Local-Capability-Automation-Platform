# AGENTS.md - how to operate this system

## What this is

A three-server MCP capability stack that sits **underneath OpenCode**, which itself sits underneath OpenLive. OpenLive stays the only user-facing interface. OpenCode keeps its own `SKILL.md` ingestion and its own model routing (Google API / Groq API). Nothing here replaces either of them, and no agent framework is the controller.

```
OpenLive  (voice / UI - untouched)
   |
OpenCode  (skills + model routing - untouched)
   |
Unified Core  <- authoritative controller, one authoritative state
   |-- Capability Registry  (62 components, 29 capabilities)
   |-- Dependency Graph     (DISCOVER RESOLVE PLAN EXECUTE OBSERVE UPDATE REPLAN CONTINUE)
   |-- Permission Engine    (config/permissions.yaml, audited)
   |-- JIT Activator        (heavy backends start on demand, pooled, swept)
   |-- Router               (deterministic; Random Forest optional, must beat baseline)
   '-- three MCP servers    (web_browser | research_knowledge | data_automation_os)
```

## Running an objective

```python
from olcap.core.orchestrator import UnifiedCore

core = UnifiedCore()
core.plan("search the web for X, write a note to ./note.md, verify it")
result = core.run(max_seconds=120)   # never runs unbounded
result['completed']   # work finished, nothing failed, verification passed
result['verified']    # ALSO checked against objective-specific criteria
```

`completed` and `verified` are deliberately different. An objective with no objective-specific criteria is `completed` but **not** `verified`, and says so in `notice`. Nothing is reported verified on generic criteria alone.

## Ordering is enforced

A step only runs after the steps it depends on have **finished**. Queued-but-not-run (`ready`) does not count as done, so a write step that must use research output really does run after the research.

## Safety rules that are not negotiable

1. **Deny paths win.** `/etc`, `/boot`, `/proc`, `/sys`, `/root`, `C:\Windows`, `~/.ssh`, `~/.aws`, `~/.gnupg` and friends are refused even when a caller asks directly, and even through a symlink.
2. **No local-file fetching.** `http(s)` only: `file://`, `ftp://` and cloud metadata addresses are refused at the fetch layer and again before any browser navigation.
3. **Terminal runs as argv, not shell**, unless a caller explicitly opts into `shell`; injection payloads are then just arguments.
4. **Secrets never land in state, logs, traces or telemetry** and are never echoed by the OpenCode adapter probe.
5. **Destructive actions and credential access always require approval**, even when auto-approval is on for everything else.
6. **The router never overrides** permissions, intent or safety.

## Time is bounded everywhere

A stalled DNS lookup, a silent worker and an impossible objective all end on a deadline. Nothing in this system can hang a run: a worker that stops answering is killed and retired, and the next call gets a fresh one.

## Adding a capability

1. Declare it in `olcap/config/capabilities.yaml`.
2. Bind real code with `@implements("CAPABILITY_ID", "component-id")`, or add a handler method to `olcap/core/worker.py`.
3. It must appear in `implementations:` **only** once code can dispatch to it. Known-but-unwired components belong in `optional_backends:`.

The discovery check fails the build if a capability advertises a backend with no code behind it.

## Operational commands

```
python -m olcap.servers.web_browser             # MCP server over stdio
python -m olcap.servers.research_knowledge
python -m olcap.servers.data_automation_os
python -m olcap.opencode --probe                # inspect the OpenCode install
python -m olcap.opencode --register             # register these three servers
python -m olcap.opencode --verify               # verify the registration
python -m olcap.opencode --remove|--rollback    # undo, with rollback

$ ... call the "core_health" tool on any server   # health of the whole stack
OLCAP_AUTO_APPROVE=1 ...                        # non-interactive runs
OLCAP_RUN_SECONDS=60 ...                        # tighter objective budget
```

## Honesty rules built into the result

A run result separates two claims:

- `completed` - the planned work finished, nothing failed, verification passed;
- `verified` - it was **also** checked against objective-specific criteria.

If no model is reachable, planning falls back to a deterministic extractive summary. That fallback is never allowed to define success criteria: the result then reports `model_degraded: true` and stays `verified: false` rather than grading itself against criteria it invented.

## What was validated, and how


Last full run: 2026-08-30 21:46 UTC, from validation scripts outside this repository (`/tmp/val/`).

| layer | checks | passed | failed | duration |
|---|---|---|---|---|
| 1 discovery / registry integrity | 41 | 41 | 0 | 0.2s |
| 2 unit / primitive | 74 | 74 | 0 | 0.1s |
| 3 capability functional | 54 | 54 | 0 | 12.8s |
| 4 break / failure / recovery | 34 | 34 | 0 | 20.1s |
| 5 security / permissions | 35 | 35 | 0 | 1.7s |
| 6 stress / concurrency | 20 | 20 | 0 | 22.1s |
| 7 end to end / integration | 33 | 33 | 0 | 8.8s |
| 8 break-the-fix | 35 | 35 | 0 | 18.0s |
| **total** | **326** | **326** | **0** | |

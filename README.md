# olcap - a three-server MCP capability stack for OpenCode / OpenLive

OpenLive stays the interface. OpenCode keeps its skills and its model routing (Google / Groq). This layer sits underneath both and does the work.

```
OpenLive        (voice / UI - untouched)
  |
OpenCode        (SKILL.md + model routing - untouched)
  |
Unified Core    (authoritative controller, one authoritative state)
  |-- Capability Registry      62 components / 29 capabilities
  |-- Dependency Graph         DISCOVER RESOLVE PLAN EXECUTE OBSERVE UPDATE REPLAN CONTINUE
  |-- Permission Engine        config-driven, every decision audited
  |-- JIT Activator            heavy backends start on demand, pooled, swept
  |-- Router                   deterministic by default; Random Forest only if it wins
  '-- 3 MCP servers            web_browser | research_knowledge | data_automation_os
```

| server | tools | what it does |
|---|---|---|
| `web_browser` | 7 | search, browse, extract, crawl, interact, source discovery/comparison |
| `research_knowledge` | 6 | research runs, RAG, knowledge search, documents, memory, source verification |
| `data_automation_os` | 16 | data analysis, databases, vectors, workflows, files, terminal, GUI, window/OS control |

## Quick start

```bash
python -m olcap.servers.web_browser            # or research_knowledge / data_automation_os
python -m olcap.opencode --register            # wire all three into OpenCode
python -m olcap.opencode --verify
```

```python
from olcap.core.orchestrator import UnifiedCore

core = UnifiedCore()
core.plan("search the web for X, write a note to ./note.md, verify it")
print(core.run(max_seconds=120))   # bounded: never runs forever
```

## Documentation

- **[TOOLS.md](TOOLS.md)** - every tool, its inputs, permissions and backends.
- **[AGENTS.md](AGENTS.md)** - how to operate and extend the system, and the safety rules.
- **[tools.manifest.yaml](tools.manifest.yaml)** - machine-readable manifest, generated from the registry.
- **[VALIDATION_REPORT.md](VALIDATION_REPORT.md)** - what was tested, fixed, blocked.

## Status

29 capabilities across 3 servers; validation: **325 checks, 0 failures** across 8 layers (see VALIDATION_REPORT.md).

Free / self-hosted first: no paid service is required, nothing calls home, and every optional backend is skipped (never faked) when it is not installed.


## Connect to OpenCode (Windows) - run all three servers together

You do **not** start the servers by hand. MCP stdio means one process per
server, and OpenCode spawns and supervises all three of them itself. Your job is
to register them once; after that all three come up together every time OpenCode
starts.

```powershell
# 1. one-time: the MCP SDK is the only hard requirement
pip install mcp
# optional, for the full capability set (anything missing degrades, never fakes)
pip install duckdb trafilatura pypdf python-docx psutil sqlite-vec
pip install playwright && playwright install chromium

# 2. point at your OpenCode install and see what it finds
python -m olcap.opencode --probe

# 3. merge all three servers into opencode.json (backups the file first)
python -m olcap.opencode --register

# 4. prove each one really launches
python -m olcap.opencode --verify

# 5. fully quit and restart OpenCode -> all three connect on startup
```

If `--probe` looks at the wrong file, name it explicitly:

```powershell
python -m olcap.opencode --register --config "$env:APPDATA\opencode\opencode.json"
```

What `register` writes (three entries, one per server) - nothing else in your
config is touched: your `model`/`provider` (Google/Groq), `permission`,
`instructions`, skills and any MCP server you already had all stay exactly as
they were:

```json
"mcp": {
  "olcap-web-browser":          { "type": "local", "command": ["python", "-m", "olcap.servers.web_browser"],          "enabled": true, "environment": { "PYTHONPATH": "<folder containing olcap/>", "PYTHONIOENCODING": "utf-8", "OLCAP_HOME": "%USERPROFILE%\\.olcap" } },
  "olcap-research-knowledge":   { "type": "local", "command": ["python", "-m", "olcap.servers.research_knowledge"],   "enabled": true, "environment": { ... } },
  "olcap-data-automation-os":   { "type": "local", "command": ["python", "-m", "olcap.servers.data_automation_os"],   "enabled": true, "environment": { ... } }
}
```

Then just use it: ask OpenCode to *"search the web for X and write a short note
to notes.md"* - it will call `web_search`, `web_extract` and `filesystem_op`
across the three servers on its own.

**Smoke-test one server by hand** (optional; OpenCode normally does this):

```powershell
echo {"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}} | python -m olcap.servers.web_browser
```

**Undo:** `python -m olcap.opencode --remove`, or restore the previous file with
`python -m olcap.opencode --rollback`.

**Notes:** `python` must be the interpreter that has `mcp` installed - if you use
a venv, register from inside it. Set `OLCAP_AUTO_APPROVE=1` in the `environment`
block if you want file/terminal actions to stop asking. Capability health is
visible any time via the `core_health` tool.

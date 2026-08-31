# TOOLS.md - the three MCP servers, tool by tool

_Generated 2026-08-30 21:46 UTC from the live registry. Every tool below is bound to code that shipped and was executed._

Three grouped servers share one config, registry, permission engine, JIT activator, health model and provenance store:

- **`web_browser`** - Web + Browser (`olcap.servers.web_browser`)
- **`research_knowledge`** - Research + Knowledge (`olcap.servers.research_knowledge`)
- **`data_automation_os`** - Data + Automation + Computer/OS (`olcap.servers.data_automation_os`)

Calling any of them: `python -m olcap.servers.<server>` (stdio MCP), or in process `from olcap.servers import <server>; await server.handle(request)`.

| | web_browser | research_knowledge | data_automation_os |
|---|---|---|---|
| capabilities | 7 | 6 | 16 |
| tools | 7 | 6 | 16 |
| need network | 7 | 2 | 0 |
| need approval | 1 | 0 | 9 |

## web_browser - Web + Browser

### `web_browse` - Web browsing / page navigation

`WEB_BROWSE` - Fetch a URL, follow redirects, return rendered-ish HTML plus metadata.

- **backends (priority order):** `olcap-fetch` (in-repo), `playwright` (jit-worker), `olcap-fetch` (in-repo)
- **known, not integrated:** `jina-reader`
- **permissions:** network
- **platforms:** windows, linux | **jit:** no | **verification:** advisory

| input | type | required | default |
|---|---|---|---|
| `url` | string | yes | - |
| `render` | boolean | no | `false` if unset |
| `max_bytes` | integer | no | `2000000` if unset |

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "method": "tools/call",
  "params": {
    "name": "web_browse",
    "arguments": {
      "url": "<value>",
      "render": "<value>"
    }
  }
}
```

### `web_compare_sources` - Source comparison

`SOURCE_COMPARISON` - Compare claims across sources and surface agreement/disagreement.

- **backends (priority order):** `olcap-search-core` (in-repo), `olcap-search-core` (in-repo)
- **known, not integrated:** `olcap-research-engine`
- **permissions:** network
- **platforms:** windows, linux | **jit:** no | **verification:** advisory

| input | type | required | default |
|---|---|---|---|
| `claim` | string | yes | - |
| `sources` | array | no | - |
| `max_sources` | integer | no | `6` if unset |

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "method": "tools/call",
  "params": {
    "name": "web_compare_sources",
    "arguments": {
      "claim": "<value>",
      "sources": "<value>"
    }
  }
}
```

### `web_crawl` - Web crawling

`WEB_CRAWL` - Breadth-first crawl within a domain, respecting robots.txt and limits.

- **backends (priority order):** `olcap-crawler` (in-repo), `crawl4ai` (jit-worker), `olcap-crawler` (in-repo)
- **known, not integrated:** `firecrawl`
- **permissions:** network
- **platforms:** windows, linux | **jit:** yes | **verification:** advisory

| input | type | required | default |
|---|---|---|---|
| `url` | string | yes | - |
| `max_pages` | integer | no | `20` if unset |
| `max_depth` | integer | no | `2` if unset |
| `same_domain` | boolean | no | `true` if unset |
| `extract` | boolean | no | `true` if unset |
| `destination` | string | no | - |

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "method": "tools/call",
  "params": {
    "name": "web_crawl",
    "arguments": {
      "url": "<value>",
      "max_pages": "<value>"
    }
  }
}
```

### `web_discover_sources` - Source discovery

`SOURCE_DISCOVERY` - Find candidate sources for a topic and score them by diversity.

- **backends (priority order):** `olcap-search-core` (in-repo), `searxng` (in-repo), `olcap-search-core` (in-repo)
- **permissions:** network
- **platforms:** windows, linux | **jit:** no | **verification:** advisory

| input | type | required | default |
|---|---|---|---|
| `topic` | string | yes | - |
| `max_sources` | integer | no | `12` if unset |

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "method": "tools/call",
  "params": {
    "name": "web_discover_sources",
    "arguments": {
      "topic": "<value>",
      "max_sources": "<value>"
    }
  }
}
```

### `web_extract` - Content extraction

`WEB_EXTRACT` - Extract main content, metadata and structured fields from a page.

- **backends (priority order):** `trafilatura` (jit-worker), `crawl4ai` (jit-worker), `trafilatura` (jit-worker)
- **known, not integrated:** `jina-reader`
- **permissions:** network
- **platforms:** windows, linux | **jit:** no | **verification:** advisory

| input | type | required | default |
|---|---|---|---|
| `url` | string | yes | - |
| `mode` | string | no | `"text"` if unset |
| `schema` | object | no | - |

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "method": "tools/call",
  "params": {
    "name": "web_extract",
    "arguments": {
      "url": "<value>",
      "mode": "<value>"
    }
  }
}
```

### `web_interact` - Browser automation / interaction

`WEB_INTERACT` - Drive a real browser: navigate, click, type, select, wait, screenshot.

- **backends (priority order):** `playwright` (jit-worker), `playwright` (jit-worker)
- **known, not integrated:** `browser-use`, `stagehand`
- **permissions:** network, execute
- **platforms:** windows, linux | **jit:** yes | **verification:** advisory

| input | type | required | default |
|---|---|---|---|
| `action` | string | yes | - |
| `url` | string | no | - |
| `selector` | string | no | - |
| `text` | string | no | - |
| `timeout_ms` | integer | no | `30000` if unset |

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "method": "tools/call",
  "params": {
    "name": "web_interact",
    "arguments": {
      "action": "<value>",
      "url": "<value>"
    }
  }
}
```

### `web_search` - Internet search

`WEB_SEARCH` - Multi-source internet search with deduplication, ranking and provenance.

- **backends (priority order):** `olcap-search-core` (in-repo), `searxng` (in-repo), `olcap-search-core` (in-repo)
- **permissions:** network
- **platforms:** windows, linux | **jit:** no | **verification:** advisory

| input | type | required | default |
|---|---|---|---|
| `query` | string | yes | - |
| `engines` | array | no | `["duckduckgo", "wikipedia"]` if unset |
| `max_results` | integer | no | `10` if unset |
| `recency_days` | integer | no | - |

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "method": "tools/call",
  "params": {
    "name": "web_search",
    "arguments": {
      "query": "<value>",
      "engines": "<value>"
    }
  }
}
```

## research_knowledge - Research + Knowledge

### `document_process` - Document intelligence

`DOCUMENT_INTELLIGENCE` - Parse PDF/DOCX/HTML/TXT/MD into text, sections and tables.

- **backends (priority order):** `pypdf` (jit-worker), `python-docx` (jit-worker), `olcap-text-extract` (in-repo), `trafilatura` (jit-worker), `docling` (jit-worker), `unstructured` (jit-worker), `marker` (jit-worker), `olcap-text-extract` (in-repo), `pypdf` (jit-worker), `trafilatura` (jit-worker)
- **permissions:** read
- **platforms:** windows, linux | **jit:** yes | **verification:** advisory

| input | type | required | default |
|---|---|---|---|
| `action` | string | yes | - |
| `path` | string | no | - |
| `url` | string | no | - |
| `engine` | string | no | - |

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "method": "tools/call",
  "params": {
    "name": "document_process",
    "arguments": {
      "action": "<value>",
      "path": "<value>"
    }
  }
}
```

### `knowledge_search` - Knowledge search

`KNOWLEDGE_SEARCH` - Semantic + lexical search across ingested knowledge bases.

- **backends (priority order):** `olcap-rag` (in-repo), `olcap-rag` (in-repo)
- **known, not integrated:** `llamaindex`, `haystack`, `anythingllm`
- **permissions:** read
- **platforms:** windows, linux | **jit:** no | **verification:** advisory

| input | type | required | default |
|---|---|---|---|
| `query` | string | yes | - |
| `collection` | string | no | `"default"` if unset |
| `top_k` | integer | no | `8` if unset |
| `mode` | string | no | `"hybrid"` if unset |

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "method": "tools/call",
  "params": {
    "name": "knowledge_search",
    "arguments": {
      "query": "<value>",
      "collection": "<value>"
    }
  }
}
```

### `memory_op` - Memory

`MEMORY` - Episodic, semantic and procedural memory with salience, decay and provenance. Shared by every agent, server and workflow.

- **backends (priority order):** `olcap-memory` (in-repo), `mem0` (jit-worker), `cognee` (jit-worker), `graphiti` (jit-worker), `olcap-memory` (in-repo)
- **known, not integrated:** `letta`
- **permissions:** read, write
- **platforms:** windows, linux | **jit:** no | **verification:** advisory

| input | type | required | default |
|---|---|---|---|
| `action` | string | yes | - |
| `kind` | string | no | `"semantic"` if unset |
| `text` | string | no | - |
| `query` | string | no | - |
| `limit` | integer | no | `10` if unset |
| `id` | string | no | - |
| `salience` | number | no | `0.5` if unset |
| `meta` | object | no | - |
| `source` | string | no | - |
| `title` | string | no | - |

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "method": "tools/call",
  "params": {
    "name": "memory_op",
    "arguments": {
      "action": "<value>",
      "kind": "<value>"
    }
  }
}
```

### `rag_query` - Retrieval-augmented generation

`RAG` - Ingest documents into a collection, then answer with citations.

- **backends (priority order):** `olcap-rag` (in-repo), `llamaindex` (jit-worker), `haystack` (jit-worker), `olcap-rag` (in-repo)
- **known, not integrated:** `ragflow`, `anythingllm`
- **permissions:** read, write
- **platforms:** windows, linux | **jit:** yes | **verification:** required

| input | type | required | default |
|---|---|---|---|
| `action` | string | yes | - |
| `collection` | string | no | `"default"` if unset |
| `query` | string | no | - |
| `paths` | array | no | - |
| `urls` | array | no | - |
| `text` | string | no | - |
| `top_k` | integer | no | `6` if unset |
| `source` | string | no | - |
| `title` | string | no | - |
| `mode` | string | no | `"hybrid"` if unset |

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "method": "tools/call",
  "params": {
    "name": "rag_query",
    "arguments": {
      "action": "<value>",
      "collection": "<value>"
    }
  }
}
```

### `research_run` - Deep research

`RESEARCH` - Plan sub-questions, gather evidence from the web, verify sources, and produce a cited report with a confidence score.

- **backends (priority order):** `olcap-research-engine` (in-repo), `olcap-research-engine` (in-repo)
- **known, not integrated:** `gpt-researcher`, `open-deep-search`, `mindsearch`, `perplexica`
- **permissions:** network, read, write
- **platforms:** windows, linux | **jit:** yes | **verification:** required

| input | type | required | default |
|---|---|---|---|
| `question` | string | yes | - |
| `depth` | integer | no | `2` if unset |
| `max_sources` | integer | no | `12` if unset |
| `verify` | boolean | no | `true` if unset |

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "method": "tools/call",
  "params": {
    "name": "research_run",
    "arguments": {
      "question": "<value>",
      "depth": "<value>"
    }
  }
}
```

### `source_verify` - Source verification

`SOURCE_VERIFICATION` - Check claims/sources: cross-source agreement, domain reputation, recency, citation presence, contradiction detection.

- **backends (priority order):** `olcap-research-engine` (in-repo), `olcap-research-engine` (in-repo)
- **known, not integrated:** `ragas`
- **permissions:** network
- **platforms:** windows, linux | **jit:** no | **verification:** required

| input | type | required | default |
|---|---|---|---|
| `claim` | string | yes | - |
| `sources` | array | no | - |
| `min_sources` | integer | no | `2` if unset |
| `evidence` | array | no | - |

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "method": "tools/call",
  "params": {
    "name": "source_verify",
    "arguments": {
      "claim": "<value>",
      "sources": "<value>"
    }
  }
}
```

## data_automation_os - Data + Automation + Computer/OS

### `computer_use` - Computer use

`COMPUTER_USE` - Composite screen+input control: observe the desktop and act on it.

- **backends (priority order):** `olcap-os-adapter` (in-repo), `olcap-os-adapter` (in-repo)
- **known, not integrated:** `pyautogui`, `openhands`
- **permissions:** execute, read
- **platforms:** windows, linux | **jit:** yes | **verification:** advisory

| input | type | required | default |
|---|---|---|---|
| `action` | string | yes | - |
| `params` | object | no | - |

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "method": "tools/call",
  "params": {
    "name": "computer_use",
    "arguments": {
      "action": "<value>",
      "params": "<value>"
    }
  }
}
```

### `data_analyze` - Data analysis

`DATA_ANALYSIS` - Profile, clean, aggregate and summarise CSV/Parquet/JSON/SQLite data.

- **backends (priority order):** `duckdb` (in-repo), `duckdb` (in-repo)
- **permissions:** read, write
- **platforms:** windows, linux | **jit:** no | **verification:** advisory

| input | type | required | default |
|---|---|---|---|
| `action` | string | yes | - |
| `path` | string | no | - |
| `destination` | string | no | - |
| `expr` | string | no | - |
| `query` | string | no | - |
| `limit` | integer | no | `20` if unset |

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "method": "tools/call",
  "params": {
    "name": "data_analyze",
    "arguments": {
      "action": "<value>",
      "path": "<value>"
    }
  }
}
```

### `database_query` - Database query

`DATABASE_QUERY` - SQL over DuckDB/SQLite/Parquet with guardrails and result limits.

- **backends (priority order):** `duckdb` (in-repo), `duckdb` (in-repo)
- **known, not integrated:** `pgvector`
- **permissions:** read, write
- **platforms:** windows, linux | **jit:** no | **verification:** advisory

| input | type | required | default |
|---|---|---|---|
| `sql` | string | yes | - |
| `source` | string | no | - |
| `limit` | integer | no | `1000` if unset |

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "method": "tools/call",
  "params": {
    "name": "database_query",
    "arguments": {
      "sql": "<value>",
      "source": "<value>"
    }
  }
}
```

### `filesystem_op` - Filesystem

`FILESYSTEM` - Read/write/list/stat/move/copy/delete through the permission policy.

- **backends (priority order):** `olcap-os-adapter` (in-repo), `olcap-os-adapter` (in-repo)
- **permissions:** read, write, destructive
- **platforms:** windows, linux | **jit:** no | **verification:** advisory

| input | type | required | default |
|---|---|---|---|
| `action` | string | yes | - |
| `path` | string | no | - |
| `destination` | string | no | - |
| `content` | string | no | - |
| `pattern` | string | no | - |
| `recursive` | boolean | no | `false` if unset |
| `limit` | integer | no | `500` if unset |
| `append` | boolean | no | `false` if unset |

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "method": "tools/call",
  "params": {
    "name": "filesystem_op",
    "arguments": {
      "action": "<value>",
      "path": "<value>"
    }
  }
}
```

### `gui_action` - GUI automation

`GUI` - Keyboard/mouse automation and element interaction.

- **backends (priority order):** `olcap-os-adapter` (in-repo), `playwright` (jit-worker), `olcap-os-adapter` (in-repo)
- **known, not integrated:** `pyautogui`
- **permissions:** execute
- **platforms:** windows, linux | **jit:** yes | **verification:** advisory

| input | type | required | default |
|---|---|---|---|
| `action` | string | yes | - |
| `params` | object | no | - |

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "method": "tools/call",
  "params": {
    "name": "gui_action",
    "arguments": {
      "action": "<value>",
      "params": "<value>"
    }
  }
}
```

### `observability_report` - Observability

`OBSERVABILITY` - Spans, counters, traces and health of the whole system.

- **backends (priority order):** `olcap-observability` (in-repo), `olcap-observability` (in-repo)
- **known, not integrated:** `opentelemetry`, `phoenix`, `langfuse`
- **permissions:** read
- **platforms:** windows, linux | **jit:** no | **verification:** advisory

| input | type | required | default |
|---|---|---|---|
| `scope` | string | no | `"summary"` if unset |

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "method": "tools/call",
  "params": {
    "name": "observability_report",
    "arguments": {
      "scope": "<value>"
    }
  }
}
```

### `process_control` - Process control

`PROCESS_CONTROL` - List, inspect, start and stop processes.

- **backends (priority order):** `olcap-os-adapter` (in-repo), `olcap-os-adapter` (in-repo)
- **known, not integrated:** `pywin32`
- **permissions:** execute, destructive
- **platforms:** windows, linux | **jit:** no | **verification:** advisory

| input | type | required | default |
|---|---|---|---|
| `action` | string | yes | - |
| `pid` | integer | no | - |
| `name` | string | no | - |
| `argv` | array | no | - |
| `cwd` | string | no | - |
| `env` | object | no | - |
| `force` | boolean | no | `true` if unset |

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "method": "tools/call",
  "params": {
    "name": "process_control",
    "arguments": {
      "action": "<value>",
      "pid": "<value>"
    }
  }
}
```

### `routing_model_op` - Adaptive routing

`ROUTING` - Random Forest routing: train, validate, compare, enable/disable.

- **backends (priority order):** `olcap-deterministic-router` (in-repo), `olcap-deterministic-router` (in-repo)
- **known, not integrated:** `scikit-learn`
- **permissions:** read, write
- **platforms:** windows, linux | **jit:** no | **verification:** advisory

| input | type | required | default |
|---|---|---|---|
| `action` | string | yes | - |
| `capability` | string | no | - |

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "method": "tools/call",
  "params": {
    "name": "routing_model_op",
    "arguments": {
      "action": "<value>",
      "capability": "<value>"
    }
  }
}
```

### `screenshot_capture` - Screenshot

`SCREENSHOT` - Capture the screen, a window or a region to a file.

- **backends (priority order):** `pillow` (in-repo), `olcap-os-adapter` (in-repo), `playwright` (jit-worker), `olcap-os-adapter` (in-repo)
- **known, not integrated:** `pyautogui`
- **permissions:** read, write
- **platforms:** windows, linux | **jit:** no | **verification:** advisory

| input | type | required | default |
|---|---|---|---|
| `target` | string | no | `"screen"` if unset |
| `region` | array | no | - |
| `path` | string | no | - |

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "method": "tools/call",
  "params": {
    "name": "screenshot_capture",
    "arguments": {
      "target": "<value>",
      "region": "<value>"
    }
  }
}
```

### `task_schedule` - Durable tasks

`DURABLE_TASKS` - Crash-safe queued tasks with retries, backoff, checkpoints and resume.

- **backends (priority order):** `olcap-workflows` (in-repo), `olcap-workflows` (in-repo)
- **known, not integrated:** `prefect`, `temporal`
- **permissions:** read, write, execute
- **platforms:** windows, linux | **jit:** no | **verification:** advisory

| input | type | required | default |
|---|---|---|---|
| `action` | string | yes | - |
| `task` | object | no | - |
| `task_id` | string | no | - |
| `max_retries` | integer | no | `3` if unset |

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "method": "tools/call",
  "params": {
    "name": "task_schedule",
    "arguments": {
      "action": "<value>",
      "task": "<value>"
    }
  }
}
```

### `terminal_run` - Terminal

`TERMINAL` - Run a shell command with timeout, cwd, env filtering and audit.

- **backends (priority order):** `olcap-os-adapter` (in-repo), `olcap-os-adapter` (in-repo)
- **permissions:** execute
- **platforms:** windows, linux | **jit:** no | **verification:** advisory

| input | type | required | default |
|---|---|---|---|
| `command` | string | yes | - |
| `cwd` | string | no | - |
| `timeout_s` | integer | no | `120` if unset |
| `shell` | string | no | - |

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "method": "tools/call",
  "params": {
    "name": "terminal_run",
    "arguments": {
      "command": "<value>",
      "cwd": "<value>"
    }
  }
}
```

### `vector_store_op` - Vector storage

`VECTOR_STORAGE` - Create/upsert/search vector collections (embedded, no server needed).

- **backends (priority order):** `sqlite-vec` (in-repo), `qdrant` (in-repo), `chroma` (jit-worker), `sqlite-vec` (in-repo)
- **known, not integrated:** `pgvector`
- **permissions:** read, write
- **platforms:** windows, linux | **jit:** yes | **verification:** advisory

| input | type | required | default |
|---|---|---|---|
| `action` | string | yes | - |
| `collection` | string | no | `"default"` if unset |
| `vectors` | array | no | - |
| `query` | string | no | - |
| `top_k` | integer | no | `10` if unset |
| `dim` | integer | no | `256` if unset |
| `vector` | array | no | - |

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "method": "tools/call",
  "params": {
    "name": "vector_store_op",
    "arguments": {
      "action": "<value>",
      "collection": "<value>"
    }
  }
}
```

### `verify_result` - Verification

`VERIFICATION` - Check that a produced artifact satisfies its success criteria.

- **backends (priority order):** `olcap-verification` (in-repo), `ragas` (jit-worker), `olcap-verification` (in-repo)
- **known, not integrated:** `promptfoo`
- **permissions:** read
- **platforms:** windows, linux | **jit:** no | **verification:** required

| input | type | required | default |
|---|---|---|---|
| `artifact_id` | string | no | - |
| `criteria` | array | no | - |
| `result` | object | no | - |

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "method": "tools/call",
  "params": {
    "name": "verify_result",
    "arguments": {
      "artifact_id": "<value>",
      "criteria": "<value>"
    }
  }
}
```

### `window_manage` - Window management

`WINDOW_MANAGEMENT` - List, focus, move, resize, minimise and close windows.

- **backends (priority order):** `olcap-os-adapter` (in-repo), `olcap-os-adapter` (in-repo)
- **known, not integrated:** `pywin32`
- **permissions:** execute
- **platforms:** windows, linux | **jit:** no | **verification:** advisory

| input | type | required | default |
|---|---|---|---|
| `action` | string | yes | - |
| `title` | string | no | - |
| `params` | object | no | - |

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "method": "tools/call",
  "params": {
    "name": "window_manage",
    "arguments": {
      "action": "<value>",
      "title": "<value>"
    }
  }
}
```

### `windows_control` - Windows OS control

`WINDOWS_CONTROL` - Windows-native control: services, registry read, environment, apps, clipboard, notifications. Linux equivalent provided by the OS adapter.

- **backends (priority order):** `olcap-os-adapter` (in-repo), `olcap-os-adapter` (in-repo)
- **known, not integrated:** `pywin32`
- **permissions:** read, write, execute, destructive
- **platforms:** windows, linux | **jit:** no | **verification:** advisory

| input | type | required | default |
|---|---|---|---|
| `action` | string | yes | - |
| `params` | object | no | - |

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "method": "tools/call",
  "params": {
    "name": "windows_control",
    "arguments": {
      "action": "<value>",
      "params": "<value>"
    }
  }
}
```

### `workflow_run` - Workflow execution

`WORKFLOW_EXECUTION` - Define, execute, pause, resume and inspect multi-step workflows.

- **backends (priority order):** `olcap-workflows` (in-repo), `olcap-workflows` (in-repo)
- **known, not integrated:** `prefect`, `temporal`, `langgraph`, `crewai`
- **permissions:** read, write, execute
- **platforms:** windows, linux | **jit:** no | **verification:** advisory

| input | type | required | default |
|---|---|---|---|
| `action` | string | yes | - |
| `workflow` | object | no | - |
| `run_id` | string | no | - |

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "method": "tools/call",
  "params": {
    "name": "workflow_run",
    "arguments": {
      "action": "<value>",
      "workflow": "<value>"
    }
  }
}
```

## Routing and failure behaviour

Every call goes through one path: **capability -> candidate components -> router -> JIT activation -> execute -> observe -> next candidate on failure**. A backend that is not installed is skipped, not faked. A backend that answers `ok=false` with no payload is treated as failure so the next candidate runs.

## Time bounds

- each network fetch: hard wall-clock bound (DNS and TLS included), default 12 s + 5 s grace;
- each capability call: bounded by the time left in the run, cap 180 s;
- each objective: `OLCAP_RUN_SECONDS` (default 300 s) wall clock.


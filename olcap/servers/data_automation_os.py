"""
MCP SERVER 3 - DATA + AUTOMATION + COMPUTER/OS.

Tools: data_analyze, database_query, vector_store_op, workflow_run,
       task_schedule, computer_use, windows_control, filesystem_op,
       process_control, terminal_run, gui_action, screenshot_capture,
       window_manage, observability_report, verify_result, routing_model_op
       (+ shared core_* tools)
"""
from __future__ import annotations

from .common import build_server, run_server

SERVER_ID = "data_automation_os"
NAME = "olcap-data-automation-os"
DESCRIPTION = ("OLCAP MCP Server 3 - Data + Automation + Computer/OS: DuckDB "
               "analysis and SQL, vector storage, durable workflows, and "
               "controlled filesystem/terminal/process/GUI/screenshot/window "
               "operations behind a permission policy.")


def build():
    from ..core.impls import automation as _automation  # noqa: F401
    from ..core.impls import dataops as _dataops        # noqa: F401
    from ..core.impls import os_ops as _os_ops          # noqa: F401
    return build_server(SERVER_ID, NAME, DESCRIPTION)


def main() -> None:
    run_server(SERVER_ID, NAME, DESCRIPTION)


if __name__ == "__main__":
    main()

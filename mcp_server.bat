@echo off
rem stdio launcher for the Locohane skills MCP server (mcp_server/server.py).
rem Must not print anything to stdout: stdout is the MCP JSON-RPC channel.
if not exist "%~dp0python_env.bat" (
  echo python_env.bat not found. Copy python_env.bat.example to python_env.bat and set PYTHON_DIR. 1>&2
  exit /b 1
)
call "%~dp0python_env.bat"
"%LOCOHANE_PYTHON%" "%~dp0mcp_server\server.py"

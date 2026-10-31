@echo off
set SELF_DIR=%~dp0
if not exist "%SELF_DIR%python_env.bat" (
  echo python_env.bat not found. Copy python_env.bat.example to python_env.bat and set PYTHON_DIR.
  pause
  exit /b 1
)
call "%SELF_DIR%python_env.bat"
cd %SELF_DIR%

rem Uses the host/port from <instances_dir>/default/instance.json so it stays in
rem sync with the admin tool (admin.bat) even if the port was changed there.
rem instances_dir comes from config.ini [admin] (or the INSTANCES_DIR env var).
rem Falls back to 127.0.0.1:8000 if the file doesn't exist yet (first run,
rem or the admin tool has never been used).
set APP_HOST=127.0.0.1
set APP_PORT=8000
for /f "usebackq tokens=1,2" %%A in (`python -c "import json; from src.config import resolve_instances_root; p = resolve_instances_root() / 'default' / 'instance.json'; d = json.loads(p.read_text(encoding='utf-8')) if p.is_file() else {}; print(d.get('app_host', '127.0.0.1'), d.get('app_port', 8000))"`) do (
    set APP_HOST=%%A
    set APP_PORT=%%B
)

rem HTTPS: if config.ini [admin] ssl_certfile/ssl_keyfile (or the SSL_CERTFILE/
rem SSL_KEYFILE env vars) are both set, pass them to chainlit. Validate first so
rem a bad path stops here instead of silently falling back to HTTP.
python -c "from src.config import resolve_ssl_files; resolve_ssl_files()" || (pause & exit /b 1)
set SSL_ARGS=
for /f "usebackq delims=" %%A in (`python -c "import subprocess; from src.config import resolve_ssl_files; f = resolve_ssl_files(); print(subprocess.list2cmdline(['--ssl-cert', str(f[0]), '--ssl-key', str(f[1])]) if f else '')"`) do (
    set SSL_ARGS=%%A
)

chainlit run app.py --host %APP_HOST% --port %APP_PORT% %SSL_ARGS%
pause

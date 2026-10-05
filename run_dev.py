"""Запуск панели, бота и планировщика для локального тестирования."""

import argparse
import os
from pathlib import Path
import signal
import subprocess
import sys
import time


BASE_DIR = Path(__file__).resolve().parent


def stop_processes(processes):
    # Separate groups let the parent handle Ctrl+C and stop every service once.
    for _, process in processes:
        if process.poll() is None:
            try:
                if os.name == "nt":
                    process.send_signal(signal.CTRL_BREAK_EVENT)
                else:
                    os.killpg(process.pid, signal.SIGINT)
            except ProcessLookupError:
                pass
            except OSError:
                process.terminate()

    deadline = time.monotonic() + 10
    for _, process in processes:
        try:
            process.wait(timeout=max(0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8000, help="Порт панели (по умолчанию 8000)")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("порт должен быть от 1 до 65535")

    venv_python = BASE_DIR / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    python = str(venv_python) if venv_python.is_file() else sys.executable
    commands = [
        ("Панель", [python, "manage.py", "runserver", f"127.0.0.1:{args.port}", "--noreload"]),
        ("Бот", [python, "-m", "bot"]),
        ("Планировщик", [python, "manage.py", "run_scheduler"]),
    ]
    env = {**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8"}
    processes = []
    print(f"Панель: http://127.0.0.1:{args.port}/admin/", flush=True)
    print("Ctrl+C остановит все процессы. После изменения кода перезапустите команду.", flush=True)
    try:
        for name, command in commands:
            print(f"Запуск: {name}", flush=True)
            options = (
                {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
                if os.name == "nt" else {"start_new_session": True}
            )
            processes.append((name, subprocess.Popen(command, cwd=BASE_DIR, env=env, **options)))
        while True:
            for name, process in processes:
                code = process.poll()
                if code is not None:
                    print(f"{name}: процесс завершился (код {code}). Останавливаем остальные.", flush=True)
                    return code if code > 0 else 1
            time.sleep(0.2)
    except KeyboardInterrupt:
        print("\nОстанавливаем панель, бота и планировщик…", flush=True)
        return 0
    except OSError:
        print("Не удалось запустить процесс. Проверьте Python и установку зависимостей.", file=sys.stderr)
        return 1
    finally:
        # A repeated Ctrl+C must not interrupt cleanup and leave services running.
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        stop_processes(processes)


if __name__ == "__main__":
    raise SystemExit(main())

"""Запуск панели, бота и планировщика для локального тестирования."""

import argparse
import os
from pathlib import Path
import signal
import subprocess
import sys
import time


BASE_DIR = Path(__file__).resolve().parent


def project_python():
    candidates = (
        [BASE_DIR / ".venv" / "Scripts/python.exe"] if os.name == "nt"
        else [BASE_DIR / ".venv-wsl/bin/python", BASE_DIR / ".venv/bin/python"]
    )
    return next((str(path) for path in candidates if path.is_file()), sys.executable)


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

    python = project_python()
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
        dependency_check = subprocess.run(
            [python, "-c", "import sys; "
             "sys.exit(1) if sys.version_info[:2] != (3, 14) else None; "
             "import django.core.management, aiogram, dotenv"],
            cwd=BASE_DIR, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        if dependency_check.returncode:
            print("Нужен Python 3.14 с зависимостями из requirements.txt.", file=sys.stderr)
            if os.name == "nt":
                print("Подготовьте окружение:\npy -3.14 -m venv .venv\n"
                      ".\\.venv\\Scripts\\python.exe -m pip install -r requirements.txt", file=sys.stderr)
            else:
                print("Windows-окружение .venv не подходит для Linux/WSL. "
                      "Создайте отдельное окружение (если установлен uv):\n"
                      "uv venv --python 3.14 .venv-wsl\n"
                      "uv pip install --python .venv-wsl/bin/python -r requirements.txt\n"
                      "Затем: python3 run_dev.py", file=sys.stderr)
            return 1
        print("Проверка миграций базы…", flush=True)
        check = subprocess.run(
            [python, "manage.py", "migrate", "--check"], cwd=BASE_DIR, env=env,
        )
        if check.returncode:
            print(
                "Проверка базы не пройдена. При неприменённых миграциях остановите все процессы, "
                "сделайте резервную копию базы и выполните:\n"
                f'"{python}" manage.py migrate\n'
                "В PowerShell добавьте & перед путём к Python в кавычках. "
                "Затем повторите запуск run_dev.py.",
                file=sys.stderr,
            )
            return 1
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

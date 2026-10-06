import io
import subprocess
import unittest
from unittest.mock import Mock, patch

import run_dev


class DevLauncherTests(unittest.TestCase):
    def setUp(self):
        self.check = patch("run_dev.subprocess.run", return_value=Mock(returncode=0)).start()
        # Launcher messages are for a terminal user, not for the test report.
        patch("sys.stdout", new_callable=io.StringIO).start()
        patch("sys.stderr", new_callable=io.StringIO).start()
        self.addCleanup(patch.stopall)

    @patch("run_dev.stop_processes")
    @patch("run_dev.signal.signal")
    @patch("run_dev.subprocess.Popen")
    @patch("sys.argv", ["run_dev.py"])
    def test_pending_migrations_prevent_service_start(self, popen, signal_handler, stop):
        self.check.side_effect = [Mock(returncode=0), Mock(returncode=1)]
        self.assertEqual(run_dev.main(), 1)
        self.assertEqual(self.check.call_args.args[0][1:], ["manage.py", "migrate", "--check"])
        popen.assert_not_called()
        stop.assert_called_once_with([])

    @patch("run_dev.stop_processes")
    @patch("run_dev.signal.signal")
    @patch("run_dev.subprocess.Popen")
    @patch("sys.argv", ["run_dev.py"])
    def test_missing_dependencies_prevent_service_start(self, popen, signal_handler, stop):
        self.check.return_value.returncode = 1
        self.assertEqual(run_dev.main(), 1)
        self.assertEqual(self.check.call_count, 1)
        popen.assert_not_called()

    @patch("run_dev.os.name", "posix")
    @patch("run_dev.Path.is_file")
    def test_linux_selects_separate_environment(self, is_file):
        is_file.return_value = True
        self.assertEqual(run_dev.project_python(), str(run_dev.BASE_DIR / ".venv-wsl/bin/python"))

    @patch("run_dev.os.name", "nt")
    @patch("run_dev.Path.is_file")
    def test_windows_selects_windows_environment(self, is_file):
        is_file.return_value = True
        self.assertEqual(run_dev.project_python(), str(run_dev.BASE_DIR / ".venv/Scripts/python.exe"))

    @patch("run_dev.stop_processes")
    @patch("run_dev.signal.signal")
    @patch("run_dev.subprocess.Popen")
    @patch("sys.argv", ["run_dev.py", "--port", "8001"])
    def test_service_failure_stops_all_services(self, popen, signal_handler, stop):
        panel, bot, scheduler = Mock(), Mock(), Mock()
        panel.poll.return_value = None
        bot.poll.return_value = 7
        scheduler.poll.return_value = None
        popen.side_effect = [panel, bot, scheduler]
        self.assertEqual(run_dev.main(), 7)
        commands = [call.args[0] for call in popen.call_args_list]
        self.assertEqual(commands[0][1:], ["manage.py", "runserver", "127.0.0.1:8001", "--noreload"])
        self.assertEqual(commands[1][1:], ["-m", "bot"])
        self.assertEqual(commands[2][1:], ["manage.py", "run_scheduler"])
        stop.assert_called_once_with([("Панель", panel), ("Бот", bot), ("Планировщик", scheduler)])
        for call in popen.call_args_list:
            self.assertEqual(call.kwargs["cwd"], run_dev.BASE_DIR)

    @patch("run_dev.stop_processes")
    @patch("run_dev.signal.signal")
    @patch("run_dev.subprocess.Popen")
    @patch("sys.argv", ["run_dev.py"])
    def test_partial_launch_is_cleaned_up(self, popen, signal_handler, stop):
        panel = Mock()
        popen.side_effect = [panel, OSError("failed")]
        self.assertEqual(run_dev.main(), 1)
        stop.assert_called_once_with([("Панель", panel)])

    @patch("run_dev.stop_processes")
    @patch("run_dev.signal.signal")
    @patch("run_dev.subprocess.Popen")
    @patch("sys.argv", ["run_dev.py"])
    def test_ctrl_c_stops_services(self, popen, signal_handler, stop):
        process = Mock()
        process.poll.side_effect = KeyboardInterrupt
        popen.return_value = process
        self.assertEqual(run_dev.main(), 0)
        self.assertEqual(len(stop.call_args.args[0]), 3)

    @patch("run_dev.os.killpg")
    @patch("run_dev.os.name", "posix")
    def test_unresponsive_process_is_killed_and_reaped(self, killpg):
        process = Mock(pid=123)
        process.poll.return_value = None
        process.wait.side_effect = [subprocess.TimeoutExpired("service", 10), 0]
        run_dev.stop_processes([("Бот", process)])
        killpg.assert_called_once_with(123, run_dev.signal.SIGINT)
        process.kill.assert_called_once()
        self.assertEqual(process.wait.call_count, 2)

"""Unit tests for HermesModsEngine Cron Manager subsystem."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
from unittest.mock import MagicMock, patch

import pytest

from engine.cron_manager import (
    CronManager,
    DEFAULT_SCHEDULE,
    CRON_MARKER_TAG,
    validate_cron_schedule,
    human_readable_schedule,
    handle_cron_cli,
)


def test_validate_cron_schedule():
    # Valid schedules
    assert validate_cron_schedule("0 4 * * *") is True
    assert validate_cron_schedule("*/15 * * * *") is True
    assert validate_cron_schedule("0 0 1 * *") is True
    assert validate_cron_schedule("0 2 * * 1-5") is True
    assert validate_cron_schedule("@daily") is True
    assert validate_cron_schedule("@midnight") is True
    assert validate_cron_schedule("@hourly") is True
    assert validate_cron_schedule("@weekly") is True

    # Invalid schedules
    assert validate_cron_schedule("") is False
    assert validate_cron_schedule("0 4 * *") is False
    assert validate_cron_schedule("0 4 * * * *") is False
    assert validate_cron_schedule("not a schedule") is False
    assert validate_cron_schedule("0 4 * * * bad") is False


def test_human_readable_schedule():
    assert human_readable_schedule("0 4 * * *") == "Daily at 04:00 (Nightly)"
    assert human_readable_schedule("0 0 * * *") == "Daily at midnight (00:00)"
    assert human_readable_schedule("@midnight") == "Daily at midnight (00:00)"
    assert human_readable_schedule("@daily") == "Daily at midnight (00:00)"
    assert human_readable_schedule("@hourly") == "Hourly"
    assert human_readable_schedule("0 * * * *") == "Hourly"
    assert human_readable_schedule("*/30 * * * *") == "Every 30 minutes"
    assert human_readable_schedule("0 3 * * *") == "Daily at 03:00"
    assert human_readable_schedule("0 15 * * *") == "Daily at 15:00"


def test_cron_manager_get_python_bin(tmp_path):
    mgr = CronManager(mods_dir=tmp_path, hermes_home=tmp_path)
    py = mgr.get_python_bin()
    assert isinstance(py, str)
    assert len(py) > 0

    # Explicit python
    explicit = sys.executable
    assert mgr.get_python_bin(explicit_bin=explicit) == explicit


def test_cron_manager_install_fresh_and_duplicate(tmp_path):
    fake_crontab_store = {"content": "29 6 * * * /backup.sh > /dev/null\n"}

    def fake_subprocess_run(cmd, *args, **kwargs):
        if cmd[0] == "crontab" and cmd[1] == "-l":
            return subprocess.CompletedProcess(cmd, 0, stdout=fake_crontab_store["content"], stderr="")
        if cmd[0] == "crontab" and len(cmd) == 2 and cmd[1] != "-l" and cmd[1] != "-r":
            # writing crontab via temp file
            p = Path(cmd[1])
            fake_crontab_store["content"] = p.read_text(encoding="utf-8")
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if cmd[0] == "crontab" and cmd[1] == "-r":
            fake_crontab_store["content"] = ""
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    with patch("shutil.which", return_value="/usr/bin/crontab"), \
         patch("subprocess.run", side_effect=fake_subprocess_run):
        mgr = CronManager(mods_dir=tmp_path, hermes_home=tmp_path)

        # 1. Fresh install
        res1 = mgr.install(schedule="0 4 * * *")
        assert res1["success"] is True
        assert res1["status"] == "installed"
        assert "0 4 * * *" in fake_crontab_store["content"]
        assert CRON_MARKER_TAG in fake_crontab_store["content"]
        assert "/backup.sh" in fake_crontab_store["content"]

        # 2. Duplicate install (identical schedule)
        res2 = mgr.install(schedule="0 4 * * *")
        assert res2["success"] is True
        assert res2["status"] == "already_installed"
        # Ensure it didn't duplicate the entry
        assert fake_crontab_store["content"].count(CRON_MARKER_TAG) == 1

        # 3. Status check
        status_res = mgr.status()
        assert status_res["installed"] is True
        assert status_res["status"] == "active"
        assert status_res["schedule"] == "0 4 * * *"

        # 4. Update schedule to 0 3 * * *
        res3 = mgr.install(schedule="0 3 * * *")
        assert res3["success"] is True
        assert res3["status"] == "updated"
        assert "0 3 * * *" in fake_crontab_store["content"]
        assert "0 4 * * *" not in fake_crontab_store["content"]
        assert fake_crontab_store["content"].count(CRON_MARKER_TAG) == 1

        # 5. Uninstall
        res_un = mgr.uninstall()
        assert res_un["success"] is True
        assert res_un["status"] == "uninstalled"
        assert CRON_MARKER_TAG not in fake_crontab_store["content"]
        assert "runner.py" not in fake_crontab_store["content"]
        # Original backup job must still be present
        assert "/backup.sh" in fake_crontab_store["content"]

        # 6. Uninstall again
        res_un2 = mgr.uninstall()
        assert res_un2["success"] is True
        assert res_un2["status"] == "not_found"


def test_cron_manager_invalid_schedule(tmp_path):
    mgr = CronManager(mods_dir=tmp_path, hermes_home=tmp_path)
    res = mgr.install(schedule="invalid-schedule")
    assert res["success"] is False
    assert "Invalid cron schedule expression" in res["error"]


def test_cron_manager_dry_run(tmp_path):
    mgr = CronManager(mods_dir=tmp_path, hermes_home=tmp_path)
    res = mgr.install(schedule="0 4 * * *", dry_run=True)
    assert res["success"] is True
    assert res["status"] == "dry_run"


def test_handle_cron_cli_json(capsys, tmp_path):
    args = MagicMock()
    args.mods_dir = str(tmp_path)
    args.target = "status"
    args.json = True
    args.schedule = "0 4 * * *"
    args.python_bin = None
    args.dry_run = False

    with patch.object(CronManager, "status", return_value={"installed": False, "status": "not_installed"}):
        rc = handle_cron_cli(args)
        assert rc == 0
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert data["installed"] is False
        assert data["status"] == "not_installed"

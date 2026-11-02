"""Tests for Tool Video Analyze mod."""
import sys
from pathlib import Path
import pytest

HERMES_ROOT = Path(__file__).resolve().parents[4]  # or env
import os
HERMES_ROOT = Path(os.environ.get("HERMES_ROOT", "/usr/local/lib/hermes-agent"))
if HERMES_ROOT.exists() and str(HERMES_ROOT) not in sys.path:
    sys.path.insert(0, str(HERMES_ROOT))


def test_video_analyze_in_core_tools():
    """Verify video_analyze is registered in Hermes core tools."""
    from toolsets import _HERMES_CORE_TOOLS
    assert "video_analyze" in _HERMES_CORE_TOOLS


def test_video_requirements_check():
    """Verify check_video_requirements executes cleanly."""
    from tools.vision_tools import check_video_requirements
    assert check_video_requirements() is True


def test_aux_call_kwargs_video_task():
    """Verify _aux_call_kwargs supports task='video'."""
    from tools.vision_tools import _aux_call_kwargs
    res = _aux_call_kwargs([{"role": "user", "content": "hi"}], "model-x", 120.0, task="video")
    assert res.get("task") == "video"
    assert res.get("timeout") >= 120.0

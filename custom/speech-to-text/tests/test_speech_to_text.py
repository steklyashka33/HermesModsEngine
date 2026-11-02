import sys
import os
import re
import subprocess
import tempfile
import time
import json
import http.server
import threading
from pathlib import Path
import pytest

SCRIPT_PATH = Path(__file__).resolve().parent.parent / "scripts" / "stt.py"

@pytest.fixture(autouse=True)
def isolate_hermes_env(tmp_path: Path, monkeypatch):
    """Isolate tests from the live host Hermes config and secrets."""
    test_home = tmp_path / "hermes_test_home"
    test_home.mkdir(parents=True, exist_ok=True)
    cfg_file = test_home / "config.yaml"
    cfg_file.write_text("providers: {}\n", encoding="utf-8")
    sec_file = test_home / "secrets.yaml"
    sec_file.write_text("mods: {}\n", encoding="utf-8")

    monkeypatch.setenv("HERMES_HOME", str(test_home))
    monkeypatch.setenv("HERMES_CONFIG_FILE", str(cfg_file))
    monkeypatch.setenv("HERMES_SECRETS_FILE", str(sec_file))

class MockSTTServer:
    def __init__(self, mode="fast", delay=0.0, response_text="Mock transcript"):
        self.mode = mode
        self.delay = delay
        self.response_text = response_text
        self.aborted = False
        self.server = None
        self.thread = None
        self.port = 0

    def start(self):
        outer = self
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                try:
                    content_len = int(self.headers.get("Content-Length", 0))
                    _ = self.rfile.read(content_len)
                    if outer.mode == "error":
                        self.send_response(500)
                        self.send_header("Content-Type", "application/json")
                        self.end_headers()
                        self.wfile.write(b'{"error": "Simulated upstream 500"}')
                        return
                    if outer.delay > 0:
                        time.sleep(outer.delay)
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    resp = {"choices": [{"message": {"content": outer.response_text}}]}
                    self.wfile.write(json.dumps(resp).encode("utf-8"))
                except (BrokenPipeError, ConnectionResetError):
                    outer.aborted = True

            def log_message(self, format, *args):
                pass

        self.server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_port
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return self

    def stop(self):
        if self.server:
            try:
                self.server.shutdown()
                self.server.server_close()
            except Exception:
                pass

def test_stt_script_exists_and_usage():
    assert SCRIPT_PATH.is_file()
    res = subprocess.run([sys.executable, str(SCRIPT_PATH)], capture_output=True, text=True)
    assert res.returncode == 1
    assert "Usage: omniroute_stt.py" in res.stderr

def test_no_hardcoded_tokens_in_script():
    content = SCRIPT_PATH.read_text(encoding="utf-8")
    # Must not contain hardcoded omni_ or sk- tokens
    assert not re.search(r'["\']omni_[A-Za-z0-9_-]{20,}["\']', content), "Found hardcoded OmniRoute token!"
    assert not re.search(r'["\']sk-[A-Za-z0-9_-]{20,}["\']', content), "Found hardcoded OpenAI token!"

def test_no_hardcoded_paths_in_script():
    content = SCRIPT_PATH.read_text(encoding="utf-8")
    assert "/root/" not in content, "Found hardcoded /root/ path in stt.py!"
    assert "/usr/local/lib/hermes-agent" not in content, "Found hardcoded /usr/local/lib/hermes-agent path in stt.py!"

def test_resolve_credentials_precedence(tmp_path: Path):
    sys.path.insert(0, str(SCRIPT_PATH.parent))
    import stt

    # Clean env vars that might interfere
    for var in ("HERMES_MOD_OPTION_API_KEY", "OMNIROUTE_API_KEY", "OPENAI_API_KEY", "HERMES_API_KEY", "STT_API_KEY"):
        os.environ.pop(var, None)

    # 1. config.yaml takes precedence over secrets.yaml and env vars
    cfg_providers = {"providers": {"omniroute": {"api_key": "cfg-omni-token", "url": "https://cfg-url/v1"}}}
    sec = {"mods": {"speech-to-text": {"api_key": "sec-token"}}}
    os.environ["OMNIROUTE_API_KEY"] = "env-token-123"

    url, key = stt.resolve_stt_credentials(cfg_providers, sec)
    assert key == "cfg-omni-token"
    assert url == "https://cfg-url/v1/chat/completions"

    # 2. speech_to_text.openai / stt.openai takes precedence over providers in config.yaml
    cfg_stt_openai = {
        "stt": {"openai": {"api_key": "stt-specific-key", "base_url": "https://stt-specific-url/v1"}},
        "providers": {"omniroute": {"api_key": "cfg-omni-token", "url": "https://cfg-url/v1"}},
    }
    url, key = stt.resolve_stt_credentials(cfg_stt_openai, sec)
    assert key == "stt-specific-key"
    assert url == "https://stt-specific-url/v1/chat/completions"

    # 3. model_providers takes precedence over providers
    cfg_mp = {
        "model_providers": {"antigravity": {"api_key": "mp-key", "base_url": "https://mp-url/v1"}},
        "providers": {"antigravity": {"api_key": "prov-key", "url": "https://prov-url/v1"}},
    }
    url, key = stt.resolve_stt_credentials(cfg_mp, sec)
    assert key == "mp-key"
    assert url == "https://mp-url/v1/chat/completions"

    # 4. Environment variables take priority when config has no keys
    url, key = stt.resolve_stt_credentials({}, sec)
    assert key == "env-token-123"

    # Clean env var
    os.environ.pop("OMNIROUTE_API_KEY", None)

    # 5. secrets.yaml is used as optional overlay when neither config nor env has key
    url, key = stt.resolve_stt_credentials({}, sec)
    assert key == "sec-token"

def test_resolve_model_resolution():
    sys.path.insert(0, str(SCRIPT_PATH.parent))
    import stt

    # Clean env vars that might interfere
    os.environ.pop("HERMES_MOD_OPTION_MODEL", None)
    os.environ.pop("STT_MODEL", None)

    # 1. Model from speech_to_text.openai.model
    cfg1 = {"speech_to_text": {"openai": {"model": "my-custom-stt"}}}
    assert stt.resolve_stt_model(cfg1) == "my-custom-stt"

    # 2. Model from stt.openai.model
    cfg2 = {"stt": {"openai": {"model": "gemini-3.5-transcribe"}}}
    assert stt.resolve_stt_model(cfg2) == "gemini-3.5-transcribe"

    # 3. Model from stt.model
    cfg3 = {"stt": {"model": "stt-transcribe"}}
    assert stt.resolve_stt_model(cfg3) == "stt-transcribe"

    # 4. Default fallback when unconfigured
    assert stt.resolve_stt_model({}) == "gemini-3.8"

    # 5. Env var STT_MODEL override when unconfigured in cfg
    os.environ["STT_MODEL"] = "env-model"
    try:
        assert stt.resolve_stt_model({}) == "env-model"
    finally:
        os.environ.pop("STT_MODEL", None)

def test_live_config_update_reflected_instantly():
    sys.path.insert(0, str(SCRIPT_PATH.parent))
    import stt

    cfg = {"providers": {"antigravity": {"api_key": "initial_key", "url": "https://proxy.example.com/v1"}}}
    sec = {}
    url, key = stt.resolve_stt_credentials(cfg, sec)
    assert key == "initial_key"

    # User updates key in Hermes config.yaml
    cfg["providers"]["antigravity"]["api_key"] = "rotated_key_987"
    url, key = stt.resolve_stt_credentials(cfg, sec)
    assert key == "rotated_key_987"

def test_graceful_fallback_when_remote_down(tmp_path: Path):
    # Create a tiny 1-second wav file
    wav_path = tmp_path / "test.wav"
    out_path = tmp_path / "out.txt"
    cmd = ["ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=800:duration=1", "-c:a", "pcm_s16le", str(wav_path)]
    subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    assert wav_path.is_file()

    # Point to an unreachable port to trigger connection error
    env = os.environ.copy()
    env["OMNIROUTE_URL"] = "http://127.0.0.1:54329/v1"
    env["HERMES_MOD_OPTION_API_KEY"] = "test-token"

    res = subprocess.run([sys.executable, str(SCRIPT_PATH), str(wav_path), str(out_path)], capture_output=True, text=True, env=env)
    assert res.returncode == 0
    assert "Falling back to local faster-whisper STT" in res.stderr
    assert out_path.exists()

def test_quick_omniroute_wins_and_whisper_does_not_start(tmp_path: Path):
    wav_path = tmp_path / "quick_test.wav"
    out_path = tmp_path / "quick_out.txt"
    cmd = ["ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=800:duration=1", "-c:a", "pcm_s16le", str(wav_path)]
    subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    srv = MockSTTServer(mode="fast", response_text="OmniRoute Quick Winner").start()
    try:
        env = os.environ.copy()
        env["OMNIROUTE_URL"] = f"http://127.0.0.1:{srv.port}/v1"
        env["HERMES_MOD_OPTION_API_KEY"] = "test-key"
        env["STT_SPECULATIVE_DELAY"] = "2.0"

        res = subprocess.run([sys.executable, str(SCRIPT_PATH), str(wav_path), str(out_path)], capture_output=True, text=True, env=env)
        assert res.returncode == 0
        assert out_path.read_text(encoding="utf-8").strip() == "OmniRoute Quick Winner"
        # Whisper should not have been started
        assert "starting speculative faster-whisper" not in res.stderr
        assert "Falling back to local faster-whisper STT" not in res.stderr
    finally:
        srv.stop()

def test_speculative_whisper_wins_when_omniroute_slow(tmp_path: Path):
    wav_path = tmp_path / "slow_test.wav"
    out_path = tmp_path / "slow_out.txt"
    cmd = ["ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=800:duration=1", "-c:a", "pcm_s16le", str(wav_path)]
    subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    # Server sleeps for 45 seconds to let CPU Whisper comfortably finish first
    srv = MockSTTServer(mode="slow", delay=45.0, response_text="Late OmniRoute").start()
    try:
        env = os.environ.copy()
        env["OMNIROUTE_URL"] = f"http://127.0.0.1:{srv.port}/v1"
        env["HERMES_MOD_OPTION_API_KEY"] = "test-key"
        env["STT_SPECULATIVE_DELAY"] = "0.2"

        t0 = time.time()
        res = subprocess.run([sys.executable, str(SCRIPT_PATH), str(wav_path), str(out_path)], capture_output=True, text=True, env=env)
        elapsed = time.time() - t0

        assert res.returncode == 0
        assert out_path.exists()
        assert "starting speculative faster-whisper" in res.stderr
        assert "Falling back to local faster-whisper STT" in res.stderr
        # Whisper should finish before the 45-second server delay
        assert elapsed < 40.0
    finally:
        srv.stop()

def test_speculative_omniroute_wins_after_whisper_started(tmp_path: Path):
    wav_path = tmp_path / "race_test.wav"
    out_path = tmp_path / "race_out.txt"
    cmd = ["ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=800:duration=1", "-c:a", "pcm_s16le", str(wav_path)]
    subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    # Server delays 0.3s. Speculative delay is 0.1s.
    # Whisper starts at 0.1s, but has a simulated delay of 10.0s.
    # OmniRoute finishes at 0.3s and wins the race, killing the Whisper process.
    srv = MockSTTServer(mode="slow", delay=0.3, response_text="OmniRoute Won The Race").start()
    try:
        env = os.environ.copy()
        env["OMNIROUTE_URL"] = f"http://127.0.0.1:{srv.port}/v1"
        env["HERMES_MOD_OPTION_API_KEY"] = "test-key"
        env["STT_SPECULATIVE_DELAY"] = "0.1"
        env["STT_TEST_WHISPER_DELAY"] = "10.0"

        t0 = time.time()
        res = subprocess.run([sys.executable, str(SCRIPT_PATH), str(wav_path), str(out_path)], capture_output=True, text=True, env=env)
        elapsed = time.time() - t0

        assert res.returncode == 0
        assert out_path.read_text(encoding="utf-8").strip() == "OmniRoute Won The Race"
        assert "starting speculative faster-whisper" in res.stderr
        # Elapsed should be well under 8.0s (since Whisper delay was 10.0s and was killed)
        assert elapsed < 8.0
    finally:
        srv.stop()

def test_immediate_omniroute_error_starts_whisper_without_waiting(tmp_path: Path):
    wav_path = tmp_path / "err_test.wav"
    out_path = tmp_path / "err_out.txt"
    cmd = ["ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=800:duration=1", "-c:a", "pcm_s16le", str(wav_path)]
    subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    # Server returns HTTP 500 immediately.
    # Speculative delay is set to 35.0 seconds.
    # Because OmniRoute failed immediately, Whisper should start at once without waiting 35s!
    srv = MockSTTServer(mode="error").start()
    try:
        env = os.environ.copy()
        env["OMNIROUTE_URL"] = f"http://127.0.0.1:{srv.port}/v1"
        env["HERMES_MOD_OPTION_API_KEY"] = "test-key"
        env["STT_SPECULATIVE_DELAY"] = "35.0"

        t0 = time.time()
        res = subprocess.run([sys.executable, str(SCRIPT_PATH), str(wav_path), str(out_path)], capture_output=True, text=True, env=env)
        elapsed = time.time() - t0

        assert res.returncode == 0
        assert out_path.exists()
        assert "OmniRoute HTTP 500" in res.stderr
        assert "Falling back to local faster-whisper STT" in res.stderr
        # Proves it did not wait 35 seconds
        assert elapsed < 33.0
    finally:
        srv.stop()

def test_whisper_worker_cli(tmp_path: Path):
    wav_path = tmp_path / "worker_test.wav"
    out_path = tmp_path / "worker_out.txt"
    cmd = ["ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=800:duration=1", "-c:a", "pcm_s16le", str(wav_path)]
    subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    res = subprocess.run(
        [sys.executable, str(SCRIPT_PATH), "--whisper-worker", str(wav_path), str(out_path), "base", "ru"],
        capture_output=True,
        text=True,
    )
    assert res.returncode == 0
    assert out_path.exists()

def test_pure_python_simple_yaml_parser():
    sys.path.insert(0, str(SCRIPT_PATH.parent))
    import stt
    sample = (
        "# Top level comment\n"
        "stt:\n"
        "  enabled: true\n"
        "  timeout: 120\n"
        "  ratio: 1.5\n"
        "  provider: stt\n"
        "  openai:\n"
        "    api_key: 'custom-key'\n"
        "    model: 'gemini-3.8'\n"
    )
    data = stt.ConfigResolver.parse_simple_yaml(sample)
    assert data["stt"]["enabled"] is True
    assert data["stt"]["timeout"] == 120
    assert data["stt"]["ratio"] == 1.5
    assert data["stt"]["provider"] == "stt"
    assert data["stt"]["openai"]["api_key"] == "custom-key"
    assert data["stt"]["openai"]["model"] == "gemini-3.8"

def test_audio_preprocessor_format_detection():
    sys.path.insert(0, str(SCRIPT_PATH.parent))
    import stt
    prep = stt.AudioPreprocessor()
    assert prep.detect_format("/tmp/audio.ogg") == "ogg"
    assert prep.detect_format("/tmp/audio.opus") == "ogg"
    assert prep.detect_format("/tmp/audio.m4a") == "m4a"
    assert prep.detect_format("/tmp/audio.wav") == "wav"
    assert prep.detect_format("/tmp/audio.mp3") == "mp3"
    assert prep.detect_format("/tmp/audio.unknown") == "mp3"


def test_stt_no_api_credentials_uses_local_whisper_immediately(tmp_path: Path):
    wav_path = tmp_path / "sample_no_api.wav"
    out_path = tmp_path / "out_no_api.txt"
    cmd = ["ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=800:duration=1", "-c:a", "pcm_s16le", str(wav_path)]
    subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    env = os.environ.copy()
    env.pop("OMNIROUTE_URL", None)
    env.pop("HERMES_MOD_OPTION_API_KEY", None)
    env["STT_TEST_MOCK_WHISPER_TEXT"] = "Mocked local whisper transcript"

    res = subprocess.run([sys.executable, str(SCRIPT_PATH), str(wav_path), str(out_path)], capture_output=True, text=True, env=env)
    assert res.returncode == 0
    assert "Falling back to local faster-whisper STT" in res.stderr
    assert out_path.exists()
    assert "Mocked local whisper transcript" in out_path.read_text(encoding="utf-8")


def test_stt_no_local_whisper_uses_only_api(tmp_path: Path):
    wav_path = tmp_path / "sample_no_whisper.wav"
    out_path = tmp_path / "out_no_whisper.txt"
    cmd = ["ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=800:duration=1", "-c:a", "pcm_s16le", str(wav_path)]
    subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    srv = MockSTTServer(mode="fast", response_text="Pure API STT Result").start()
    try:
        env = os.environ.copy()
        env["OMNIROUTE_URL"] = f"http://127.0.0.1:{srv.port}/v1"
        env["HERMES_MOD_OPTION_API_KEY"] = "test-key"
        env["STT_FORCE_LOCAL_UNAVAILABLE"] = "1"

        res = subprocess.run([sys.executable, str(SCRIPT_PATH), str(wav_path), str(out_path)], capture_output=True, text=True, env=env)
        assert res.returncode == 0
        assert out_path.exists()
        assert out_path.read_text(encoding="utf-8").strip() == "Pure API STT Result"
        assert "Falling back to local faster-whisper STT" not in res.stderr
    finally:
        srv.stop()


def test_stt_neither_api_nor_whisper_clean_exit(tmp_path: Path):
    wav_path = tmp_path / "sample_neither.wav"
    out_path = tmp_path / "out_neither.txt"
    cmd = ["ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=800:duration=1", "-c:a", "pcm_s16le", str(wav_path)]
    subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    env = os.environ.copy()
    env.pop("OMNIROUTE_URL", None)
    env.pop("HERMES_MOD_OPTION_API_KEY", None)
    env["STT_FORCE_LOCAL_UNAVAILABLE"] = "1"

    res = subprocess.run([sys.executable, str(SCRIPT_PATH), str(wav_path), str(out_path)], capture_output=True, text=True, env=env)
    assert res.returncode != 0
    assert "STT unavailable: neither remote API credentials nor local faster-whisper are configured/installed" in res.stderr


def test_stt_no_artificial_45s_limit_and_150s_timeout():
    sys.path.insert(0, str(SCRIPT_PATH.parent))
    import inspect
    import stt

    # Ensure no min(..., 45.0) in stt.py
    code = SCRIPT_PATH.read_text(encoding="utf-8")
    assert "45.0" not in code, "Artificial 45-second limit still found in stt.py!"

    # Ensure SpeculativeRacer and speculative_racing_stt default to 150.0 total_timeout
    sig_racer = inspect.signature(stt.SpeculativeRacer.__init__)
    assert sig_racer.parameters["total_timeout"].default == 150.0
    assert sig_racer.parameters["speculative_delay"].default == 60.0

    sig_func = inspect.signature(stt.speculative_racing_stt)
    assert sig_func.parameters["total_timeout"].default == 150.0
    assert sig_func.parameters["speculative_delay"].default == 60.0

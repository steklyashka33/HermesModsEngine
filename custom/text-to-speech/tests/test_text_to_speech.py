import io
import json
import http.server
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import threading
import time
import wave
import pytest

SCRIPT_PATH = Path(__file__).resolve().parent.parent / "scripts" / "tts.py"


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


class MockTTSServer:
    """Mock HTTP server for testing remote OmniRoute TTS endpoints."""

    def __init__(self, mode: str = "success", delay: float = 0.0):
        self.mode = mode
        self.delay = delay
        self.server: http.server.HTTPServer | None = None
        self.thread: threading.Thread | None = None
        self.port: int = 0
        self.aborted: bool = False
        self.requests_count: int = 0

    def start(self):
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                outer.requests_count += 1
                try:
                    content_len = int(self.headers.get("Content-Length", 0))
                    _ = self.rfile.read(content_len)

                    # Return quick adaptation if called for chat completions
                    if "/chat/completions" in self.path:
                        self.send_response(200)
                        self.send_header("Content-Type", "application/json")
                        self.end_headers()
                        self.wfile.write(b'{"choices": [{"message": {"content": "adapted text"}}]}')
                        return

                    if outer.mode == "error":
                        self.send_response(500)
                        self.send_header("Content-Type", "application/json")
                        self.end_headers()
                        self.wfile.write(b'{"error": "Simulated upstream 500 internal server error"}')
                        return

                    if outer.delay > 0:
                        time.sleep(outer.delay)

                    self.send_response(200)
                    self.send_header("Content-Type", "audio/wav")
                    self.end_headers()

                    # Return 0.2s of silent 24kHz 16-bit mono PCM wav
                    buf = io.BytesIO()
                    with wave.open(buf, "wb") as wf:
                        wf.setnchannels(1)
                        wf.setsampwidth(2)
                        wf.setframerate(24000)
                        wf.writeframes(b"\x00\x00" * 4800)
                    self.wfile.write(buf.getvalue())
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


def test_tts_script_exists_and_execution():
    assert SCRIPT_PATH.is_file()
    res = subprocess.run([sys.executable, str(SCRIPT_PATH)], capture_output=True, text=True)
    assert res.returncode != 0
    assert "Usage: omniroute_tts.py" in res.stderr


def test_no_hardcoded_paths_in_script():
    content = SCRIPT_PATH.read_text(encoding="utf-8")
    assert "/root/" not in content, "Found hardcoded /root/ path in tts.py!"
    assert "/usr/local/lib/hermes-agent" not in content, "Found hardcoded /usr/local/lib/hermes-agent path in tts.py!"


def test_no_hardcoded_tokens_in_script():
    content = SCRIPT_PATH.read_text(encoding="utf-8")
    assert not re.search(r'["\']omni_[A-Za-z0-9_-]{20,}["\']', content), "Found hardcoded OmniRoute token!"
    assert not re.search(r'["\']sk-[A-Za-z0-9_-]{20,}["\']', content), "Found hardcoded OpenAI token!"


def test_zero_dependency_yaml_parser():
    sys.path.insert(0, str(SCRIPT_PATH.parent))
    import tts

    sample_yaml = """
# Sample Hermes TTS config
tts:
  provider: command
  model: gemini-2.5-flash-preview-tts
  voice: Kore
  timeout: 25
  fallback_edge: true
  adaptation:
    enabled: true
    model: gemini
  voices:
    - Kore
    - Aoede
    - Fenrir
providers:
  antigravity:
    url: https://proxy.example.com:20129/v1
    ssl_verify: false
"""
    parsed = tts.ConfigResolver.parse_simple_yaml(sample_yaml)
    assert parsed["tts"]["provider"] == "command"
    assert parsed["tts"]["model"] == "gemini-2.5-flash-preview-tts"
    assert parsed["tts"]["voice"] == "Kore"
    assert parsed["tts"]["timeout"] == 25
    assert parsed["tts"]["fallback_edge"] is True
    assert parsed["tts"]["adaptation"]["enabled"] is True
    assert parsed["tts"]["adaptation"]["model"] == "gemini"
    assert parsed["tts"]["voices"] == ["Kore", "Aoede", "Fenrir"]
    assert parsed["providers"]["antigravity"]["url"] == "https://proxy.example.com:20129/v1"
    assert parsed["providers"]["antigravity"]["ssl_verify"] is False


def test_resolve_credentials_precedence():
    sys.path.insert(0, str(SCRIPT_PATH.parent))
    import tts

    # 1. Environment variable precedence
    os.environ["HERMES_MOD_OPTION_API_KEY"] = "env-tts-key"
    cfg = {"providers": {"antigravity": {"api_key": "cfg-key", "base_url": "https://cfg-url/v1"}}}
    sec = {"mods": {"text-to-speech": {"api_key": "sec-key"}}}
    url, key = tts.resolve_provider_credentials(cfg, sec, "antigravity")
    assert key == "env-tts-key"
    del os.environ["HERMES_MOD_OPTION_API_KEY"]

    # 2. secrets.yaml precedence
    url, key = tts.resolve_provider_credentials(cfg, sec, "antigravity")
    assert key == "sec-key"
    assert url == "https://cfg-url/v1"

    # 3. config.yaml fallback
    url, key = tts.resolve_provider_credentials(cfg, {}, "antigravity")
    assert key == "cfg-key"


def test_resolve_tts_parameters_from_config():
    sys.path.insert(0, str(SCRIPT_PATH.parent))
    import tts

    env_keys = ["TTS_TIMEOUT", "HERMES_MOD_OPTION_TIMEOUT", "HERMES_MOD_CONFIG_TIMEOUT"]
    saved_env = {k: os.environ.pop(k, None) for k in env_keys}
    try:
        cfg = {
            "tts": {
                "provider": "tts",
                "model_provider": "antigravity",
                "model": "tts-gemini-v2",
                "voice": "Fenrir",
                "timeout": 22,
                "fallback_edge": True,
            },
            "providers": {
                "antigravity": {
                    "url": "https://test-host:20129/v1",
                    "api_key": "cfg-secret-token",
                }
            }
        }

        assert tts.ConfigResolver.resolve_tts_provider(cfg) == "antigravity"
        assert tts.ConfigResolver.resolve_tts_model(cfg) == "tts-gemini-v2"
        assert tts.ConfigResolver.resolve_tts_voice(cfg) == "Fenrir"
        assert tts.ConfigResolver.resolve_tts_timeout(cfg) == 22.0
        assert tts.ConfigResolver.resolve_tts_timeout({}) == 150.0
        cfg_provider = {"tts": {"providers": {"tts": {"timeout": 300}}}}
        assert tts.ConfigResolver.resolve_tts_timeout(cfg_provider) == 300.0
        assert tts.ConfigResolver.resolve_fallback_edge(cfg) is True

        url, key = tts.ConfigResolver.resolve_tts_credentials(cfg, {})
        assert url == "https://test-host:20129/v1"
        assert key == "cfg-secret-token"
    finally:
        for k, v in saved_env.items():
            if v is not None:
                os.environ[k] = v


def test_clean_synthid_watermark():
    sys.path.insert(0, str(SCRIPT_PATH.parent))
    import tts
    import struct

    # Construct synthetic WAV with C2PA watermark trailing data
    pcm_bytes = b"\x00\x00" * 100
    pcm_len = len(pcm_bytes)
    c2pa_tail = b"\x00\x00\x00\x00C2PA_WATERMARK_SIGNATURE_PAYLOAD_TEST"

    raw_wav = (
        b"RIFF"
        + struct.pack("<I", pcm_len + 36 + len(c2pa_tail))
        + b"WAVEfmt \x10\x00\x00\x00\x01\x00\x01\x00\xc0]\x00\x00\x80\xbb\x00\x00\x02\x00\x10\x00data"
        + struct.pack("<I", pcm_len + len(c2pa_tail))
        + pcm_bytes
        + c2pa_tail
    )

    cleaned = tts.clean_synthid_watermark(raw_wav)
    assert b"C2PA" not in cleaned
    assert len(cleaned) == 44 + pcm_len
    unpacked_data_len = struct.unpack_from("<I", cleaned, 40)[0]
    assert unpacked_data_len == pcm_len


def test_audio_converter_target_formats(tmp_path: Path):
    sys.path.insert(0, str(SCRIPT_PATH.parent))
    import tts

    # Create small valid WAV
    wav_path = tmp_path / "sample.wav"
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(24000)
        wf.writeframes(b"\x00\x00" * 4800)
    wav_path.write_bytes(buf.getvalue())

    # 1. Convert to Opus VoIP OGG (Telegram voice bubble standard)
    ogg_path = tmp_path / "voice.ogg"
    assert tts.AudioConverter.convert(str(wav_path), str(ogg_path)) is True
    assert ogg_path.is_file() and ogg_path.stat().st_size > 0

    # 2. Convert to MP3
    mp3_path = tmp_path / "voice.mp3"
    assert tts.AudioConverter.convert(str(wav_path), str(mp3_path)) is True
    assert mp3_path.is_file() and mp3_path.stat().st_size > 0


def test_edge_tts_fallback_synthesis(tmp_path: Path):
    sys.path.insert(0, str(SCRIPT_PATH.parent))
    import tts

    tmp_mp3 = tmp_path / "edge_test.mp3"
    text = "Проверка синтеза речи через Microsoft Edge TTS."
    voice = tts.ConfigResolver.resolve_edge_voice(text)
    assert "ru-RU" in voice

    ok = tts.EdgeTTSWorker.synthesize(text, voice, str(tmp_mp3), timeout=30.0)
    assert ok is True
    assert tmp_mp3.is_file()
    assert tmp_mp3.stat().st_size > 0


def test_graceful_fallback_when_remote_down(tmp_path: Path):
    in_text = tmp_path / "text.txt"
    in_text.write_text("Тестовый текст для синтеза речи", encoding="utf-8")
    out_ogg = tmp_path / "voice.ogg"

    env = os.environ.copy()
    env["OMNIROUTE_URL"] = "http://127.0.0.1:54329/v1"
    env["HERMES_MOD_OPTION_API_KEY"] = "test-token"

    res = subprocess.run([sys.executable, str(SCRIPT_PATH), str(in_text), str(out_ogg)], capture_output=True, text=True, env=env)
    assert res.returncode == 0
    assert "Falling back to local Edge TTS" in res.stderr
    assert out_ogg.exists()
    assert out_ogg.stat().st_size > 0


def test_graceful_fallback_on_500_error(tmp_path: Path):
    in_text = tmp_path / "text_err.txt"
    in_text.write_text("Тест при ошибке сервера OmniRoute", encoding="utf-8")
    out_ogg = tmp_path / "voice_err.ogg"

    srv = MockTTSServer(mode="error").start()
    try:
        env = os.environ.copy()
        env["OMNIROUTE_URL"] = f"http://127.0.0.1:{srv.port}/v1"
        env["HERMES_MOD_OPTION_API_KEY"] = "test-key"

        res = subprocess.run([sys.executable, str(SCRIPT_PATH), str(in_text), str(out_ogg)], capture_output=True, text=True, env=env)
        assert res.returncode == 0
        assert "Falling back to local Edge TTS" in res.stderr
        assert out_ogg.exists()
        assert out_ogg.stat().st_size > 0
    finally:
        srv.stop()


def test_graceful_fallback_on_timeout(tmp_path: Path):
    in_text = tmp_path / "text_slow.txt"
    in_text.write_text("Тест при превышении таймаута OmniRoute", encoding="utf-8")
    out_ogg = tmp_path / "voice_slow.ogg"

    # Server delays for 20s
    srv = MockTTSServer(mode="slow", delay=20.0).start()
    try:
        env = os.environ.copy()
        env["OMNIROUTE_URL"] = f"http://127.0.0.1:{srv.port}/v1"
        env["HERMES_MOD_OPTION_API_KEY"] = "test-key"
        env["TTS_TIMEOUT"] = "1.0"

        t0 = time.time()
        res = subprocess.run([sys.executable, str(SCRIPT_PATH), str(in_text), str(out_ogg)], capture_output=True, text=True, env=env)
        elapsed = time.time() - t0

        assert res.returncode == 0
        assert "Falling back to local Edge TTS" in res.stderr
        assert out_ogg.exists()
        assert out_ogg.stat().st_size > 0
        # Timeout was 1.0s, fallback edge-tts took ~1-3s, total should be well below 15s
        assert elapsed < 15.0
    finally:
        srv.stop()


def test_omniroute_quick_success(tmp_path: Path):
    in_text = tmp_path / "text_ok.txt"
    in_text.write_text("Успешная генерация через OmniRoute", encoding="utf-8")
    out_ogg = tmp_path / "voice_ok.ogg"

    srv = MockTTSServer(mode="success").start()
    try:
        env = os.environ.copy()
        env["OMNIROUTE_URL"] = f"http://127.0.0.1:{srv.port}/v1"
        env["HERMES_MOD_OPTION_API_KEY"] = "valid-token"

        res = subprocess.run([sys.executable, str(SCRIPT_PATH), str(in_text), str(out_ogg)], capture_output=True, text=True, env=env)
        assert res.returncode == 0
        assert "Falling back to local Edge TTS" not in res.stderr
        assert out_ogg.exists()
        assert out_ogg.stat().st_size > 0
    finally:
        srv.stop()


def test_resolve_speculative_delay():
    sys.path.insert(0, str(SCRIPT_PATH.parent))
    import tts

    env_keys = [
        "TTS_SPECULATIVE_DELAY",
        "TTS_SPECULATIVE_FALLBACK_DELAY",
        "HERMES_MOD_OPTION_SPECULATIVE_FALLBACK_DELAY",
        "HERMES_MOD_CONFIG_SPECULATIVE_FALLBACK_DELAY",
    ]
    saved_env = {k: os.environ.pop(k, None) for k in env_keys}
    try:
        # 1. Default fallback
        assert tts.ConfigResolver.resolve_speculative_delay({}) == 8.0

        # 2. Under tts
        cfg_tts = {"tts": {"speculative_fallback_delay": 5}}
        assert tts.ConfigResolver.resolve_speculative_delay(cfg_tts) == 5.0

        # 3. Under mods.text-to-speech
        cfg_mod = {"mods": {"text-to-speech": {"speculative_fallback_delay": 3}}}
        assert tts.ConfigResolver.resolve_speculative_delay(cfg_mod) == 3.0

        # 4. Environment variable precedence
        os.environ["TTS_SPECULATIVE_DELAY"] = "2.5"
        try:
            assert tts.ConfigResolver.resolve_speculative_delay(cfg_mod) == 2.5
        finally:
            del os.environ["TTS_SPECULATIVE_DELAY"]
    finally:
        for k, v in saved_env.items():
            if v is not None:
                os.environ[k] = v


def test_speculative_racing_edge_wins_when_omniroute_slow(tmp_path: Path):
    in_text = tmp_path / "race_edge_win.txt"
    in_text.write_text("Гонка: Edge побеждает, когда удаленный шлюз задерживает ответ", encoding="utf-8")
    out_ogg = tmp_path / "voice_edge_win.ogg"

    # Mock server delays 10.0s
    srv = MockTTSServer(mode="slow", delay=10.0).start()
    try:
        env = os.environ.copy()
        env["OMNIROUTE_URL"] = f"http://127.0.0.1:{srv.port}/v1"
        env["HERMES_MOD_OPTION_API_KEY"] = "test-token"
        env["TTS_SPECULATIVE_DELAY"] = "0.2"
        env["TTS_TIMEOUT"] = "20.0"

        t0 = time.time()
        res = subprocess.run([sys.executable, str(SCRIPT_PATH), str(in_text), str(out_ogg)], capture_output=True, text=True, env=env)
        elapsed = time.time() - t0

        assert res.returncode == 0
        assert out_ogg.exists()
        assert out_ogg.stat().st_size > 0
        assert "starting speculative Edge TTS" in res.stderr
        # Should finish well before the 10.0s server delay because Edge wins
        assert elapsed < 7.0
    finally:
        srv.stop()


def test_speculative_racing_omniroute_wins_and_kills_edge(tmp_path: Path):
    in_text = tmp_path / "race_omni_win.txt"
    in_text.write_text("Гонка: OmniRoute побеждает и убивает фоновый EdgeTTS", encoding="utf-8")
    out_ogg = tmp_path / "voice_omni_win.ogg"

    # Server delays 0.3s. Speculative delay is 0.1s.
    # Edge starts after 0.1s, but has simulated delay of 6.0s.
    # OmniRoute finishes at 0.3s, wins race, and kills EdgeTTS with SIGKILL.
    srv = MockTTSServer(mode="slow", delay=0.3).start()
    try:
        env = os.environ.copy()
        env["OMNIROUTE_URL"] = f"http://127.0.0.1:{srv.port}/v1"
        env["HERMES_MOD_OPTION_API_KEY"] = "test-token"
        env["TTS_SPECULATIVE_DELAY"] = "0.1"
        env["TTS_TEST_EDGE_DELAY"] = "6.0"
        env["TTS_TIMEOUT"] = "15.0"

        t0 = time.time()
        res = subprocess.run([sys.executable, str(SCRIPT_PATH), str(in_text), str(out_ogg)], capture_output=True, text=True, env=env)
        elapsed = time.time() - t0

        assert res.returncode == 0
        assert out_ogg.exists()
        assert out_ogg.stat().st_size > 0
        assert "starting speculative Edge TTS" in res.stderr
        # Edge delay was 6.0s, so total time < 4.0s proves Edge was killed and OmniRoute won
        assert elapsed < 4.0
    finally:
        srv.stop()


def test_tts_no_api_credentials_uses_local_edge_immediately(tmp_path: Path):
    in_text = tmp_path / "no_api.txt"
    in_text.write_text("Синтез только через Edge TTS без настроек API", encoding="utf-8")
    out_ogg = tmp_path / "voice_no_api.ogg"

    env = os.environ.copy()
    env.pop("OMNIROUTE_URL", None)
    env.pop("HERMES_MOD_OPTION_API_KEY", None)

    res = subprocess.run([sys.executable, str(SCRIPT_PATH), str(in_text), str(out_ogg)], capture_output=True, text=True, env=env)
    assert res.returncode == 0
    assert "Falling back to local Edge TTS" in res.stderr
    assert out_ogg.exists()
    assert out_ogg.stat().st_size > 0


def test_tts_no_local_edge_uses_only_api(tmp_path: Path):
    in_text = tmp_path / "no_edge.txt"
    in_text.write_text("Синтез только через API когда локалка недоступна", encoding="utf-8")
    out_ogg = tmp_path / "voice_no_edge.ogg"

    srv = MockTTSServer(mode="success").start()
    try:
        env = os.environ.copy()
        env["OMNIROUTE_URL"] = f"http://127.0.0.1:{srv.port}/v1"
        env["HERMES_MOD_OPTION_API_KEY"] = "valid-token"
        env["TTS_FORCE_LOCAL_UNAVAILABLE"] = "1"

        res = subprocess.run([sys.executable, str(SCRIPT_PATH), str(in_text), str(out_ogg)], capture_output=True, text=True, env=env)
        assert res.returncode == 0
        assert out_ogg.exists()
        assert "Falling back to local Edge TTS" not in res.stderr
    finally:
        srv.stop()


def test_tts_neither_api_nor_edge_clean_exit(tmp_path: Path):
    in_text = tmp_path / "neither.txt"
    in_text.write_text("Тест когда нет ни API, ни локалки", encoding="utf-8")
    out_ogg = tmp_path / "voice_neither.ogg"

    env = os.environ.copy()
    env.pop("OMNIROUTE_URL", None)
    env.pop("HERMES_MOD_OPTION_API_KEY", None)
    env["TTS_FORCE_LOCAL_UNAVAILABLE"] = "1"

    res = subprocess.run([sys.executable, str(SCRIPT_PATH), str(in_text), str(out_ogg)], capture_output=True, text=True, env=env)
    assert res.returncode != 0
    assert "TTS unavailable: neither remote API credentials nor local EdgeTTS are configured/installed" in res.stderr


def test_tts_fallback_edge_false_clean_exit_on_remote_failure(tmp_path: Path):
    in_text = tmp_path / "edge_false_fail.txt"
    in_text.write_text("Тест при fallback_edge=false и сбое remote API", encoding="utf-8")
    out_ogg = tmp_path / "voice_edge_false_fail.ogg"

    srv = MockTTSServer(mode="error").start()
    try:
        env = os.environ.copy()
        env["OMNIROUTE_URL"] = f"http://127.0.0.1:{srv.port}/v1"
        env["HERMES_MOD_OPTION_API_KEY"] = "test-key"
        env["TTS_FALLBACK_EDGE"] = "false"

        res = subprocess.run([sys.executable, str(SCRIPT_PATH), str(in_text), str(out_ogg)], capture_output=True, text=True, env=env)
        assert res.returncode != 0
        assert "Falling back to local Edge TTS" not in res.stderr
        assert not out_ogg.exists()
    finally:
        srv.stop()


def test_tts_fallback_edge_false_uses_remote_api(tmp_path: Path):
    in_text = tmp_path / "edge_false_ok.txt"
    in_text.write_text("Тест при fallback_edge=false и успехе remote API", encoding="utf-8")
    out_ogg = tmp_path / "voice_edge_false_ok.ogg"

    srv = MockTTSServer(mode="success").start()
    try:
        env = os.environ.copy()
        env["OMNIROUTE_URL"] = f"http://127.0.0.1:{srv.port}/v1"
        env["HERMES_MOD_OPTION_API_KEY"] = "test-key"
        env["TTS_FALLBACK_EDGE"] = "false"

        res = subprocess.run([sys.executable, str(SCRIPT_PATH), str(in_text), str(out_ogg)], capture_output=True, text=True, env=env)
        assert res.returncode == 0
        assert "Falling back to local Edge TTS" not in res.stderr
        assert out_ogg.exists()
        assert out_ogg.stat().st_size > 0
    finally:
        srv.stop()

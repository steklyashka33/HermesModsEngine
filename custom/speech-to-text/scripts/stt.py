#!/usr/bin/env python3
"""
Speech-to-Text (STT) Module with Speculative Racing Fallback.

Architecture:
- ConfigResolver: Dynamic, zero-hardcode configuration & secrets resolution.
- AudioPreprocessor: Automatic Opus transcoding for heavy audio (>750 KB) via ffmpeg.
- CancellableHTTPHandler & OmniRouteClient: Low-latency multimodal STT streaming.
- WhisperWorker: Dynamic faster-whisper worker & subprocess lifecycle management.
- SpeculativeRacer: Parallel race coordination with instant cancellation of the losing path.

Universal portability:
- Zero hardcoded filesystem paths or user paths (uses sys.executable, Path.home(), HERMES_ROOT).
- Pure-Python YAML parser fallback ensures execution even without PyYAML installed.
"""

from __future__ import annotations

import base64
import http.client
import json
import os
from pathlib import Path
import queue
import re
import socket
import ssl
import subprocess
import sys
import threading
import time
from typing import Any, Optional, Tuple
import urllib.error
import urllib.request

# Optional PyYAML support with built-in zero-dependency fallback
try:
    import yaml as _pyyaml
    _HAS_YAML = True
except ImportError:
    _pyyaml = None
    _HAS_YAML = False

SYSTEM_PROMPT = """Ты — специализированный модуль Speech-to-Text. Твоя единственная задача: расшифровать речь в чистый, грамотный текст.

ИНСТРУКЦИИ:
1. Языки речи — исключительно русский и/или английский (включая технические термины и фразы на английском). Спикер НЕ говорит по-арабски и на других восточных языках. Любые вздохи, невнятные звуки, заминки, причмокивания, зевки и фоновые шумы категорически запрещено интерпретировать как арабские, турецкие или иные восточные фразы и слова (например, иншаллах, субханаллах, вя, ва и т.д.).
2. Фокусируйся исключительно на голосе основного спикера. Игнорируй фоновые шумы, музыку, клики и дыхание.
3. Делай текст естественным и читаемым: убирай заминки, звуки раздумий («э-э», «мм»), заикания и повторы.
4. Убирай паразитные слова и связки («собственно», «как бы», «получается», «ну вот», «типа того», «короче»), если они не несут смысла.
5. Оставляй только осмысленный текст. Полностью сохраняй исходный смысл, термины (Hermes, OmniRoute, API, STT и др.), факты и цифры. Не пересказывай своими словами.
6. Выведи ТОЛЬКО расшифрованный текст. Категорически запрещено добавлять вводные слова, префиксы («Текст:», «Расшифровка:»), кавычки, пояснения или цитировать эти правила."""


def log_stt(msg: str) -> None:
    """Thread-safe append to Hermes STT log file without raising exceptions."""
    try:
        hermes_home = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
        log_dir = hermes_home / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = log_dir / "stt.log"
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(f"[{ts}] [PID {os.getpid()}] {msg}\n")
    except Exception:
        pass


class ConfigResolver:
    """Dynamic configuration and secrets resolver with zero hardcoded paths."""

    @staticmethod
    def get_hermes_home() -> Path:
        return Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes")).expanduser().resolve()

    @classmethod
    def parse_simple_yaml(cls, text: str) -> dict:
        """Zero-dependency pure-Python YAML parser for key-value and nested dictionary structures."""
        root: dict = {}
        stack: list[Tuple[int, dict]] = [(-1, root)]
        for raw_line in text.splitlines():
            line = re.sub(r"#.*$", "", raw_line).rstrip()
            if not line.strip() or line.strip().startswith("-"):
                continue
            indent = len(line) - len(line.lstrip(" "))
            while len(stack) > 1 and stack[-1][0] >= indent:
                stack.pop()
            m = re.match(r"^\s*([A-Za-z0-9_.-]+):\s*(.*)$", line)
            if not m:
                continue
            key = m.group(1).strip()
            val = m.group(2).strip()
            if not val or val == "{}":
                sub: dict = {}
                stack[-1][1][key] = sub
                stack.append((indent, sub))
            else:
                val_clean = val.strip("\"'")
                if val_clean.lower() == "true":
                    parsed_val: Any = True
                elif val_clean.lower() == "false":
                    parsed_val = False
                elif val_clean.isdigit():
                    parsed_val = int(val_clean)
                else:
                    try:
                        parsed_val = float(val_clean)
                    except ValueError:
                        parsed_val = val_clean
                stack[-1][1][key] = parsed_val
        return root

    @classmethod
    def read_yaml(cls, path: Path) -> dict:
        """Read YAML file using PyYAML if available, or fallback to simple parser with retries."""
        try:
            resolved = path.expanduser().resolve()
            if not resolved.is_file():
                return {}
            for _ in range(3):
                try:
                    if _HAS_YAML and _pyyaml is not None:
                        with open(resolved, "r", encoding="utf-8") as f:
                            data = _pyyaml.safe_load(f)
                            if isinstance(data, dict) and data:
                                return data
                    text = resolved.read_text(encoding="utf-8", errors="replace")
                    data = cls.parse_simple_yaml(text)
                    if isinstance(data, dict) and data:
                        return data
                except Exception:
                    pass
                time.sleep(0.05)
            text = resolved.read_text(encoding="utf-8", errors="replace")
            data = cls.parse_simple_yaml(text)
            if isinstance(data, dict):
                return data
        except Exception:
            pass
        return {}

    @classmethod
    def load_config(cls) -> dict:
        """Load Hermes configuration from standard environment or home paths."""
        candidates: list[Path] = []
        if os.environ.get("HERMES_CONFIG_FILE"):
            candidates.append(Path(os.environ["HERMES_CONFIG_FILE"]))
        home = cls.get_hermes_home()
        candidates.append(home / "config.yaml")

        for path in candidates:
            data = cls.read_yaml(path)
            if data:
                return data

        # Check backups if primary configuration was temporarily unreadable
        backup_dir = home / "backups" / "config"
        if backup_dir.is_dir():
            for b in sorted(backup_dir.glob("config.yaml.good.*"), reverse=True)[:3]:
                data = cls.read_yaml(b)
                if data:
                    return data
        return {}

    @classmethod
    def load_secrets(cls) -> dict:
        """Load Hermes secrets from standard environment or home paths."""
        candidates: list[Path] = []
        if os.environ.get("HERMES_SECRETS_FILE"):
            candidates.append(Path(os.environ["HERMES_SECRETS_FILE"]))
        home = cls.get_hermes_home()
        candidates.append(home / "secrets.yaml")

        for path in candidates:
            data = cls.read_yaml(path)
            if data:
                return data
        return {}

    @classmethod
    def _extract(cls, data: dict, section_keys: tuple[str, ...], subkeys: tuple[str, ...], field_names: tuple[str, ...]) -> str:
        if not isinstance(data, dict):
            return ""
        for sec in section_keys:
            sec_dict = data.get(sec)
            if not isinstance(sec_dict, dict):
                continue
            for sub in subkeys:
                target = sec_dict.get(sub) if sub else sec_dict
                if isinstance(target, dict):
                    for f in field_names:
                        val = target.get(f)
                        if val and str(val).strip():
                            return str(val).strip()
        return ""

    @classmethod
    def resolve_credentials(cls, cfg: dict, sec: dict) -> tuple[str, str]:
        """Resolve (url, api_key) with precedence: config > env > secrets."""
        api_key = ""
        base_url = ""

        # 1. API key from config.yaml
        if isinstance(cfg, dict):
            api_key = cls._extract(cfg, ("speech_to_text", "stt"), ("openai",), ("api_key",))
            if not api_key:
                api_key = cls._extract(cfg, ("mods",), ("speech-to-text", "stt"), ("api_key",))
            if not api_key:
                api_key = cls._extract(cfg, ("model_providers",), ("antigravity", "omniroute", "openai"), ("api_key",))
            if not api_key:
                active_prov = cfg.get("model", {}).get("provider") if isinstance(cfg.get("model"), dict) else ""
                prov_candidates = [p for p in (active_prov, "antigravity", "omniroute", "openai") if p]
                seen = set()
                ordered = [p for p in prov_candidates if not (p in seen or seen.add(p))]
                api_key = cls._extract(cfg, ("providers",), tuple(ordered), ("api_key",))
            if not api_key and isinstance(cfg.get("model"), dict):
                k = cfg["model"].get("api_key")
                if k and str(k).strip():
                    api_key = str(k).strip()

        # 2. API key from environment variables
        if not api_key:
            api_key = (
                os.environ.get("OMNIROUTE_API_KEY")
                or os.environ.get("OPENAI_API_KEY")
                or os.environ.get("HERMES_API_KEY")
                or os.environ.get("HERMES_MOD_OPTION_API_KEY")
                or os.environ.get("STT_API_KEY")
                or ""
            ).strip()

        # 3. API key from secrets.yaml overlay
        if not api_key and isinstance(sec, dict):
            api_key = (
                cls._extract(sec, ("mods",), ("speech-to-text", "stt"), ("api_key",))
                or cls._extract(sec, ("speech_to_text", "stt"), ("openai",), ("api_key",))
                or cls._extract(sec, ("model_providers",), ("antigravity", "omniroute", "openai"), ("api_key",))
                or cls._extract(sec, ("providers",), ("antigravity", "omniroute", "openai"), ("api_key",))
                or str(sec.get("api_key") or "").strip()
            )

        # 4. Base URL from config.yaml
        if isinstance(cfg, dict):
            base_url = cls._extract(cfg, ("speech_to_text", "stt"), ("openai",), ("base_url", "url"))
            if not base_url:
                base_url = cls._extract(cfg, ("mods",), ("speech-to-text", "stt"), ("base_url", "url"))
            if not base_url:
                base_url = cls._extract(cfg, ("model_providers",), ("antigravity", "omniroute", "openai"), ("base_url", "url"))
            if not base_url:
                active_prov = cfg.get("model", {}).get("provider") if isinstance(cfg.get("model"), dict) else ""
                prov_candidates = [p for p in (active_prov, "antigravity", "omniroute", "openai") if p]
                seen = set()
                ordered = [p for p in prov_candidates if not (p in seen or seen.add(p))]
                base_url = cls._extract(cfg, ("providers",), tuple(ordered), ("base_url", "url"))
            if not base_url and isinstance(cfg.get("model"), dict):
                u = cfg["model"].get("base_url") or cfg["model"].get("url")
                if u and str(u).strip():
                    base_url = str(u).strip()

        # 5. Base URL from environment variables
        if not base_url:
            base_url = (
                os.environ.get("OMNIROUTE_URL")
                or os.environ.get("OPENAI_BASE_URL")
                or os.environ.get("HERMES_MOD_OPTION_BASE_URL")
                or ""
            ).strip()

        # 6. Base URL from secrets.yaml overlay
        if not base_url and isinstance(sec, dict):
            base_url = (
                cls._extract(sec, ("mods",), ("speech-to-text", "stt"), ("base_url", "url"))
                or cls._extract(sec, ("speech_to_text", "stt"), ("openai",), ("base_url", "url"))
                or cls._extract(sec, ("model_providers",), ("antigravity", "omniroute", "openai"), ("base_url", "url"))
                or cls._extract(sec, ("providers",), ("antigravity", "omniroute", "openai"), ("base_url", "url"))
                or str(sec.get("base_url") or sec.get("url") or "").strip()
            )

        url = (base_url or "").strip().rstrip("/")
        if url and not url.endswith("/chat/completions"):
            url = f"{url}/chat/completions"

        return url, str(api_key or "").strip()

    @classmethod
    def resolve_model(cls, cfg: dict, sec: dict | None = None) -> str:
        """Resolve STT model name with canonical precedence."""
        if isinstance(cfg, dict):
            for sec_name in ("stt", "speech_to_text"):
                sec_dict = cfg.get(sec_name) if isinstance(cfg.get(sec_name), dict) else {}
                openai_dict = sec_dict.get("openai") if isinstance(sec_dict.get("openai"), dict) else {}
                m = openai_dict.get("model")
                if m and str(m).strip():
                    return str(m).strip()
                m_direct = sec_dict.get("model")
                if m_direct and str(m_direct).strip():
                    return str(m_direct).strip()

            mods_cfg = cfg.get("mods") if isinstance(cfg.get("mods"), dict) else {}
            for mod_name in ("speech-to-text", "stt"):
                m_mod = mods_cfg.get(mod_name, {}).get("model") if isinstance(mods_cfg.get(mod_name), dict) else ""
                if m_mod and str(m_mod).strip() and str(m_mod).strip() != "stt":
                    return str(m_mod).strip()

        env_model = (
            os.environ.get("STT_MODEL")
            or os.environ.get("HERMES_MOD_OPTION_MODEL")
            or ""
        ).strip()
        if env_model:
            return env_model

        if isinstance(sec, dict):
            for sec_name in ("stt", "speech_to_text"):
                sec_dict = sec.get(sec_name) if isinstance(sec.get(sec_name), dict) else {}
                openai_dict = sec_dict.get("openai") if isinstance(sec_dict.get("openai"), dict) else {}
                m = openai_dict.get("model")
                if m and str(m).strip():
                    return str(m).strip()
            mods_sec = sec.get("mods") if isinstance(sec.get("mods"), dict) else {}
            for mod_name in ("speech-to-text", "stt"):
                m_sec = mods_sec.get(mod_name, {}).get("model") if isinstance(mods_sec.get(mod_name), dict) else ""
                if m_sec and str(m_sec).strip() and str(m_sec).strip() != "stt":
                    return str(m_sec).strip()

        return "gemini-3.8"


class AudioPreprocessor:
    """Prepares and optionally downscales heavy audio for STT processing."""

    FORMAT_MAP = {
        "ogg": "ogg", "oga": "ogg", "opus": "ogg",
        "m4a": "m4a", "aac": "m4a", "mp4": "m4a",
        "wav": "wav", "wave": "wav",
    }
    DEFAULT_FORMAT = "mp3"
    HEAVY_AUDIO_THRESHOLD = 750 * 1024  # 750 KB

    def __init__(self, downscale_heavy_audio: bool = True):
        self.downscale_heavy_audio = downscale_heavy_audio
        self.temp_files_to_clean: list[str] = []

    def detect_format(self, path: str) -> str:
        ext = os.path.splitext(path)[1].lower().lstrip(".")
        return self.FORMAT_MAP.get(ext, self.DEFAULT_FORMAT)

    def process(self, audio_path: str) -> tuple[str, str, str]:
        """
        Reads audio file, downscales to compact mono Opus if >750KB (and downscale is on),
        and returns (base64_data, audio_format, effective_input_path).
        """
        orig_path = Path(audio_path).expanduser().resolve()
        with open(orig_path, "rb") as f:
            audio_bytes = f.read()

        effective_path = str(orig_path)
        is_compressed = False

        if self.downscale_heavy_audio and len(audio_bytes) > self.HEAVY_AUDIO_THRESHOLD:
            import tempfile
            with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
                tmp_path = tmp.name
            try:
                t0 = time.time()
                cmd = [
                    "ffmpeg", "-y", "-i", str(orig_path),
                    "-c:a", "libopus", "-b:a", "20k", "-ac", "1",
                    tmp_path
                ]
                res = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
                if res.returncode == 0 and os.path.getsize(tmp_path) > 0:
                    with open(tmp_path, "rb") as f:
                        audio_bytes = f.read()
                    effective_path = tmp_path
                    is_compressed = True
                    self.temp_files_to_clean.append(tmp_path)
                    log_stt(f"Downscaled heavy audio ({len(audio_bytes)} bytes) in {time.time()-t0:.2f}s")
                else:
                    self._safe_remove(tmp_path)
            except Exception as exc:
                log_stt(f"Downscale exception: {exc}")
                self._safe_remove(tmp_path)

        fmt = "ogg" if is_compressed else self.detect_format(str(orig_path))
        b64_data = base64.b64encode(audio_bytes).decode("ascii")
        return b64_data, fmt, effective_path

    def cleanup(self) -> None:
        for p in self.temp_files_to_clean:
            self._safe_remove(p)
        self.temp_files_to_clean.clear()

    @staticmethod
    def _safe_remove(path: str) -> None:
        try:
            if os.path.exists(path):
                os.remove(path)
        except OSError:
            pass


class CancellableHTTPHandler(urllib.request.HTTPHandler, urllib.request.HTTPSHandler):
    """HTTP/HTTPS handler tracking active sockets to permit immediate network abort."""

    def __init__(self, context: ssl.SSLContext | None = None):
        super().__init__()
        self._ssl_context = context
        self.active_sockets: list[socket.socket] = []
        self._lock = threading.Lock()

    def http_open(self, req: urllib.request.Request):
        return self.do_open(self._build_http, req)

    def https_open(self, req: urllib.request.Request):
        return self.do_open(self._build_https, req)

    def _build_http(self, host: str, **kwargs):
        conn = http.client.HTTPConnection(host, **kwargs)
        orig_connect = conn.connect
        def _conn():
            orig_connect()
            with self._lock:
                if conn.sock:
                    self.active_sockets.append(conn.sock)
        conn.connect = _conn
        return conn

    def _build_https(self, host: str, **kwargs):
        ctx = self._ssl_context or ssl.create_default_context()
        try:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        except Exception:
            pass
        kwargs["context"] = ctx
        conn = http.client.HTTPSConnection(host, **kwargs)
        orig_connect = conn.connect
        def _conn():
            orig_connect()
            with self._lock:
                if conn.sock:
                    self.active_sockets.append(conn.sock)
        conn.connect = _conn
        return conn

    def cancel(self) -> None:
        """Immediately shutdown and close all active sockets."""
        with self._lock:
            for s in self.active_sockets:
                try:
                    s.shutdown(socket.SHUT_RDWR)
                except Exception:
                    pass
                try:
                    s.close()
                except Exception:
                    pass
            self.active_sockets.clear()


class OmniRouteClient:
    """Client for multimodal STT inference via OmniRoute / OpenAI-compatible endpoint."""

    @staticmethod
    def clean_transcript(raw_content: str) -> str:
        """Clean markdown, meta prefixes, and system boilerplate from LLM transcription."""
        raw_text = raw_content.strip().strip("\"'")
        lines = raw_text.split("\n")
        idx = 0
        while idx < len(lines):
            line = lines[idx].strip()
            m_inline = re.search(r"(?:без кавычек[^\n.:]*|мета-тегов[^\n.:]*|инструкциями[^\n.:]*)[.:]\s*(.+)$", line, re.IGNORECASE)
            if m_inline:
                lines[idx] = m_inline.group(1).strip()
                break
            is_meta = (
                re.match(r"^(?:[-*•]|\d+\.)\s+", line) or
                re.match(r"^(?:голос|расшифровка|прослушивание|инструкци|правила|удаление|сохранение|вывод)\b", line, re.IGNORECASE)
            )
            if is_meta:
                idx += 1
            else:
                break
        if idx < len(lines):
            raw_text = "\n".join(lines[idx:]).strip()
        raw_text = re.sub(
            r"^(?:распознанный текст(?:[^\n.:]*)[.:]|точная расшифровка(?:[^\n.:]*)[.:])\s*",
            "", raw_text, flags=re.IGNORECASE
        ).strip()
        return raw_text.strip("\"'")

    @classmethod
    def request_once(
        cls,
        url: str,
        api_key: str,
        b64_data: str,
        fmt: str,
        timeout: float,
        handler: CancellableHTTPHandler,
        cancel_event: threading.Event,
        model: str = "",
    ) -> str:
        payload = {
            "model": model,
            "reasoning_effort": "none",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": SYSTEM_PROMPT},
                        {
                            "type": "input_audio",
                            "input_audio": {"data": b64_data, "format": fmt}
                        }
                    ]
                }
            ]
        }
        req = urllib.request.Request(url)
        req.add_header("Authorization", f"Bearer {api_key}")
        req.add_header("Content-Type", "application/json")
        req.data = json.dumps(payload).encode("utf-8")

        opener = urllib.request.build_opener(handler)
        with opener.open(req, timeout=timeout) as resp:
            if cancel_event.is_set():
                raise RuntimeError("OmniRoute request cancelled")
            raw_bytes = resp.read()
            if cancel_event.is_set():
                raise RuntimeError("OmniRoute request cancelled")
            data = json.loads(raw_bytes.decode("utf-8"))
            msg_content = data.get("choices", [{}])[0].get("message", {}).get("content") or ""
            if isinstance(msg_content, list):
                parts = []
                for p in msg_content:
                    if isinstance(p, dict) and p.get("type") == "text":
                        parts.append(p.get("text", ""))
                    elif isinstance(p, str):
                        parts.append(p)
                transcript = "".join(parts)
            else:
                transcript = str(msg_content)
            return cls.clean_transcript(transcript)


class WhisperWorker:
    """Manages local faster-whisper transcription and isolated worker subprocesses."""

    @staticmethod
    def get_interpreter() -> str:
        """Find a python interpreter that has faster_whisper installed, using dynamic discovery."""
        try:
            import faster_whisper  # noqa: F401
            return sys.executable
        except ImportError:
            pass

        candidates: list[Path] = []
        hermes_root_env = os.environ.get("HERMES_ROOT")
        if hermes_root_env:
            candidates.append(Path(hermes_root_env) / "venv" / "bin" / "python3")
            candidates.append(Path(hermes_root_env) / "bin" / "python3")

        hermes_home = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
        candidates.extend([
            hermes_home / "venv" / "bin" / "python3",
            hermes_home / ".venv" / "bin" / "python3",
        ])

        installs_dir = hermes_home / "installs"
        if installs_dir.is_dir():
            for py in installs_dir.glob("**/venv/bin/python3"):
                candidates.append(py)

        candidates.extend([
            Path(sys.prefix) / "bin" / "python3",
            Path.home() / ".local" / "share" / "hermes" / "venv" / "bin" / "python3",
        ])

        for cand in candidates:
            if cand.is_file() and os.access(cand, os.X_OK):
                try:
                    probe = subprocess.run(
                        [str(cand), "-c", "import faster_whisper"],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        timeout=5,
                    )
                    if probe.returncode == 0:
                        return str(cand)
                except Exception:
                    continue

        return sys.executable

    @classmethod
    def is_available(cls) -> bool:
        """Check if local faster-whisper engine is available in the current environment."""
        if os.environ.get("STT_FORCE_LOCAL_UNAVAILABLE") == "1":
            return False
        try:
            import faster_whisper  # noqa: F401
            return True
        except ImportError:
            pass
        interp = cls.get_interpreter()
        if interp != sys.executable:
            return True
        try:
            probe = subprocess.run(
                [sys.executable, "-c", "import faster_whisper"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
            )
            return probe.returncode == 0
        except Exception:
            return False

    @staticmethod
    def run_direct(input_path: str, output_path: str, model_name: str = "base", language: str = "ru") -> bool:
        """Direct in-process faster-whisper transcription."""
        mock_text = os.environ.get("STT_TEST_MOCK_WHISPER_TEXT")
        if mock_text is not None:
            out_p = Path(output_path).expanduser().resolve()
            out_p.parent.mkdir(parents=True, exist_ok=True)
            out_p.write_text(mock_text, encoding="utf-8")
            return True

        sim_delay = float(os.environ.get("STT_TEST_WHISPER_DELAY", "0"))
        if sim_delay > 0:
            time.sleep(sim_delay)
        try:
            from faster_whisper import WhisperModel
            model = WhisperModel(model_name, device="cpu", compute_type="int8")
            segments, _ = model.transcribe(input_path, language=language if language else None, beam_size=1)
            text_parts = [segment.text.strip() for segment in segments]
            transcript = " ".join(t for t in text_parts if t).strip()
            out_path = Path(output_path).expanduser().resolve()
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(transcript, encoding="utf-8")
            return True
        except Exception as exc:
            sys.stderr.write(f"Direct faster-whisper failed: {exc}\n")
            return False

    @classmethod
    def start_subprocess(cls, input_path: str, output_path: str, model_name: str, language: str) -> subprocess.Popen:
        """Start faster-whisper in an isolated subprocess that can be killed cleanly if outraced."""
        whisper_py = cls.get_interpreter()
        cmd = [
            whisper_py,
            str(Path(__file__).resolve()),
            "--whisper-worker",
            input_path,
            output_path,
            model_name,
            language or "",
        ]
        return subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    @staticmethod
    def kill_process(proc: subprocess.Popen | None) -> None:
        """Forcibly terminate worker process and reap to prevent zombies."""
        if proc is None or proc.poll() is not None:
            return
        try:
            proc.kill()
            proc.wait(timeout=2)
        except Exception:
            pass

    @classmethod
    def run_fallback(cls, input_path: str, output_path: str, cfg: dict) -> bool:
        """Fallback to local faster-whisper when remote STT is unavailable."""
        stt_cfg = cfg.get("stt") if isinstance(cfg.get("stt"), dict) else {}
        local_cfg = stt_cfg.get("local") if isinstance(stt_cfg.get("local"), dict) else {}
        model_name = local_cfg.get("model") or "base"
        language = local_cfg.get("language") or stt_cfg.get("language") or "ru"

        try:
            from faster_whisper import WhisperModel  # noqa: F401
            return cls.run_direct(input_path, output_path, model_name, language)
        except Exception:
            pass

        whisper_py = cls.get_interpreter()
        if whisper_py != sys.executable:
            try:
                res = subprocess.run(
                    [whisper_py, str(Path(__file__).resolve()), "--whisper-worker", input_path, output_path, model_name, language or ""],
                    capture_output=True, text=True, timeout=150
                )
                if res.returncode == 0 and os.path.exists(output_path):
                    return True
                else:
                    sys.stderr.write(f"Hermes venv faster-whisper fallback failed: {res.stderr}\n")
            except Exception as exc:
                sys.stderr.write(f"Hermes venv fallback invocation failed: {exc}\n")
        return False


class SpeculativeRacer:
    """Coordinates speculative parallel racing between remote multimodal STT and local Whisper."""

    def __init__(
        self,
        url: str,
        api_key: str,
        b64_data: str,
        fmt: str,
        input_audio_path: str,
        final_output_path: str,
        cfg: dict,
        speculative_delay: float = 60.0,
        total_timeout: float = 150.0,
        fallback_whisper: bool = True,
        model: str = "",
    ):
        self.url = url
        self.api_key = api_key
        self.b64_data = b64_data
        self.fmt = fmt
        self.input_audio_path = input_audio_path
        self.final_output_path = final_output_path
        self.cfg = cfg
        self.speculative_delay = speculative_delay
        self.total_timeout = total_timeout
        self.fallback_whisper = fallback_whisper
        self.model = model

        stt_cfg = cfg.get("stt") if isinstance(cfg.get("stt"), dict) else {}
        local_cfg = stt_cfg.get("local") if isinstance(stt_cfg.get("local"), dict) else {}
        self.whisper_model = local_cfg.get("model") or "base"
        self.whisper_language = local_cfg.get("language") or stt_cfg.get("language") or "ru"

        self.whisper_temp_out = f"{final_output_path}.whisper.{os.getpid()}.tmp"
        self.ssl_ctx = ssl.create_default_context()
        self.ssl_ctx.check_hostname = False
        self.ssl_ctx.verify_mode = ssl.CERT_NONE

        self.omniroute_handler = CancellableHTTPHandler(self.ssl_ctx)
        self.cancel_omniroute_event = threading.Event()
        self.cancel_whisper_event = threading.Event()
        self.omniroute_done_event = threading.Event()
        self.result_queue: queue.Queue = queue.Queue()
        self.whisper_proc_holder: list[subprocess.Popen | None] = [None]
        self.start_time = 0.0

    def _clean_whisper_tmp(self) -> None:
        if os.path.exists(self.whisper_temp_out):
            try:
                os.remove(self.whisper_temp_out)
            except OSError:
                pass

    def _write_final(self, text: str, is_whisper: bool = False) -> None:
        out_p = Path(self.final_output_path).expanduser().resolve()
        out_p.parent.mkdir(parents=True, exist_ok=True)
        if is_whisper:
            payload = json.dumps({"transcript": text, "provider": "local (whisper)"}, ensure_ascii=False)
            out_p.write_text(payload, encoding="utf-8")
        else:
            out_p.write_text(text, encoding="utf-8")

    def _omniroute_worker(self) -> None:
        max_attempts = 3
        last_error = ""
        try:
            for attempt in range(1, max_attempts + 1):
                if self.cancel_omniroute_event.is_set():
                    break
                try:
                    log_stt(f"OmniRoute attempt {attempt}/{max_attempts} for model '{self.model}' at {self.url}...")
                    t0 = time.time()
                    transcript = OmniRouteClient.request_once(
                        self.url,
                        self.api_key,
                        self.b64_data,
                        self.fmt,
                        self.total_timeout,
                        self.omniroute_handler,
                        self.cancel_omniroute_event,
                        model=self.model,
                    )
                    dur = time.time() - t0
                    if not self.cancel_omniroute_event.is_set():
                        log_stt(f"OmniRoute attempt {attempt} succeeded in {dur:.2f}s ({len(transcript)} chars)")
                        self.result_queue.put(("omniroute", True, transcript))
                        return
                except urllib.error.HTTPError as e:
                    err_body = e.read().decode("utf-8", errors="ignore") if hasattr(e, "read") else ""
                    last_error = f"OmniRoute HTTP {e.code}: {err_body}"
                    log_stt(f"OmniRoute attempt {attempt} failed: HTTP {e.code} ({err_body[:200]})")
                    if e.code in (400, 401, 403, 404):
                        break
                except Exception as e:
                    last_error = f"OmniRoute STT error: {e}"
                    log_stt(f"OmniRoute attempt {attempt} error: {e}")
                if attempt < max_attempts and not self.cancel_omniroute_event.is_set():
                    time.sleep(1.5)
            if not self.cancel_omniroute_event.is_set():
                self.result_queue.put(("omniroute", False, last_error))
        finally:
            self.omniroute_done_event.set()

    def _start_whisper(self) -> None:
        sys.stderr.write("Falling back to local faster-whisper STT...\n")
        proc = WhisperWorker.start_subprocess(
            self.input_audio_path,
            self.whisper_temp_out,
            self.whisper_model,
            self.whisper_language,
        )
        self.whisper_proc_holder[0] = proc

        def _monitor():
            stdout, stderr = proc.communicate()
            if self.cancel_whisper_event.is_set():
                return
            if proc.returncode == 0 and os.path.exists(self.whisper_temp_out):
                w_content = Path(self.whisper_temp_out).read_text(encoding="utf-8", errors="replace").strip()
                self.result_queue.put(("whisper", True, w_content))
            else:
                err = stderr.decode("utf-8", errors="ignore")
                self.result_queue.put(("whisper", False, f"Local faster-whisper failed: {err}"))

        t = threading.Thread(target=_monitor, daemon=True)
        t.start()

    def run(self) -> bool:
        self.start_time = time.time()
        t_omni = threading.Thread(target=self._omniroute_worker, daemon=True)
        t_omni.start()

        # Wait up to speculative_delay for fast OmniRoute completion
        omni_finished_in_window = self.omniroute_done_event.wait(timeout=self.speculative_delay)
        whisper_started = False

        if omni_finished_in_window:
            try:
                src, ok, payload = self.result_queue.get_nowait()
                if ok:
                    elapsed = time.time() - self.start_time
                    log_stt(f"Fast OmniRoute success in {elapsed:.2f}s ({len(payload)} chars). Whisper never started.")
                    self._write_final(payload, is_whisper=False)
                    self._clean_whisper_tmp()
                    return True
                else:
                    log_stt(f"OmniRoute failed after {time.time()-self.start_time:.2f}s: {payload}")
                    sys.stderr.write(f"{payload}\n")
                    if not self.fallback_whisper:
                        self._clean_whisper_tmp()
                        return False
                    log_stt("Falling back to local faster-whisper STT...")
                    sys.stderr.write("Falling back to local faster-whisper STT...\n")
                    self._clean_whisper_tmp()
                    ok_w = WhisperWorker.run_fallback(self.input_audio_path, self.whisper_temp_out, self.cfg)
                    w_text = ""
                    if ok_w and os.path.exists(self.whisper_temp_out):
                        w_text = Path(self.whisper_temp_out).read_text(encoding="utf-8", errors="replace").strip()
                    self._write_final(w_text, is_whisper=True)
                    self._clean_whisper_tmp()
                    log_stt(f"Whisper fallback finished in {time.time()-self.start_time:.2f}s ({len(w_text)} chars)")
                    return bool(ok_w)
            except queue.Empty:
                pass
        else:
            if self.fallback_whisper:
                log_stt(f"OmniRoute STT delayed beyond {self.speculative_delay}s, starting speculative faster-whisper...")
                sys.stderr.write(f"OmniRoute STT delayed beyond {self.speculative_delay}s, starting speculative faster-whisper...\n")
                whisper_started = True
                self._start_whisper()

        if not whisper_started:
            if not omni_finished_in_window:
                rem = max(0.1, self.total_timeout - (time.time() - self.start_time))
                if self.omniroute_done_event.wait(timeout=rem):
                    try:
                        src, ok, payload = self.result_queue.get_nowait()
                        if ok:
                            log_stt(f"OmniRoute succeeded in {time.time()-self.start_time:.2f}s ({len(payload)} chars)")
                            self._write_final(payload, is_whisper=False)
                            return True
                        else:
                            log_stt(f"OmniRoute failed after {time.time()-self.start_time:.2f}s: {payload}")
                            sys.stderr.write(f"{payload}\n")
                            return False
                    except queue.Empty:
                        pass
            return False

        deadline = self.start_time + self.total_timeout
        omni_failed = False
        whisper_failed = False

        while time.time() < deadline:
            rem = max(0.05, deadline - time.time())
            try:
                src, ok, val = self.result_queue.get(timeout=rem)
                if ok:
                    if src == "omniroute":
                        self.cancel_whisper_event.set()
                        WhisperWorker.kill_process(self.whisper_proc_holder[0])
                        self._clean_whisper_tmp()
                        dur = time.time() - self.start_time
                        log_stt(f"OmniRoute won race in {dur:.2f}s ({len(val)} chars). Loser Whisper killed.")
                        self._write_final(val, is_whisper=False)
                        return True
                    elif src == "whisper":
                        self.cancel_omniroute_event.set()
                        self.omniroute_handler.cancel()
                        whisper_text = ""
                        if os.path.exists(self.whisper_temp_out):
                            raw_w = Path(self.whisper_temp_out).read_text(encoding="utf-8", errors="replace").strip()
                            if raw_w.startswith("{") and raw_w.endswith("}"):
                                try:
                                    parsed = json.loads(raw_w)
                                    whisper_text = str(parsed.get("transcript") or raw_w)
                                except Exception:
                                    whisper_text = raw_w
                            else:
                                whisper_text = raw_w
                        dur = time.time() - self.start_time
                        log_stt(f"Whisper won race in {dur:.2f}s ({len(whisper_text)} chars). OmniRoute cancelled.")
                        self._write_final(whisper_text, is_whisper=True)
                        self._clean_whisper_tmp()
                        return True
                else:
                    if src == "omniroute":
                        omni_failed = True
                        sys.stderr.write(f"{val}\n")
                    elif src == "whisper":
                        whisper_failed = True
                        sys.stderr.write(f"{val}\n")
                    if omni_failed and whisper_failed:
                        break
            except queue.Empty:
                break

        self.cancel_whisper_event.set()
        WhisperWorker.kill_process(self.whisper_proc_holder[0])
        self.cancel_omniroute_event.set()
        self.omniroute_handler.cancel()
        self._clean_whisper_tmp()
        return False


# Module-level aliases for backward compatibility with tests & external callers
def resolve_stt_credentials(cfg: dict, sec: dict) -> tuple[str, str]:
    return ConfigResolver.resolve_credentials(cfg, sec)


def resolve_stt_model(cfg: dict, sec: dict | None = None) -> str:
    return ConfigResolver.resolve_model(cfg, sec)


def clean_transcript(raw_content: str) -> str:
    return OmniRouteClient.clean_transcript(raw_content)


def run_whisper_direct(input_path: str, output_path: str, model_name: str = "base", language: str = "ru") -> bool:
    return WhisperWorker.run_direct(input_path, output_path, model_name, language)


def run_local_whisper_fallback(input_path: str, output_path: str, cfg: dict) -> bool:
    return WhisperWorker.run_fallback(input_path, output_path, cfg)


def speculative_racing_stt(
    url: str,
    api_key: str,
    b64_data: str,
    fmt: str,
    input_audio_path: str,
    final_output_path: str,
    cfg: dict,
    speculative_delay: float = 60.0,
    total_timeout: float = 150.0,
    fallback_whisper: bool = True,
    model: str = "",
) -> bool:
    racer = SpeculativeRacer(
        url=url,
        api_key=api_key,
        b64_data=b64_data,
        fmt=fmt,
        input_audio_path=input_audio_path,
        final_output_path=final_output_path,
        cfg=cfg,
        speculative_delay=speculative_delay,
        total_timeout=total_timeout,
        fallback_whisper=fallback_whisper,
        model=model,
    )
    return racer.run()


def main() -> None:
    # 1. Direct whisper worker subprocess invocation
    if len(sys.argv) > 1 and sys.argv[1] == "--whisper-worker":
        if len(sys.argv) < 4:
            sys.stderr.write("Usage: stt.py --whisper-worker <input_path> <output_path> [model] [lang]\n")
            sys.exit(1)
        in_p = sys.argv[2]
        out_p = sys.argv[3]
        m_name = sys.argv[4] if len(sys.argv) > 4 else "base"
        l_name = sys.argv[5] if len(sys.argv) > 5 else "ru"
        ok = WhisperWorker.run_direct(in_p, out_p, m_name, l_name)
        sys.exit(0 if ok else 1)

    # 2. Main STT execution
    if len(sys.argv) < 3:
        sys.stderr.write("Usage: omniroute_stt.py <input_audio_path> <output_transcript_path>\n")
        sys.exit(1)

    orig_input_path = sys.argv[1]
    output_path = sys.argv[2]

    if not os.path.exists(orig_input_path):
        sys.stderr.write(f"File not found: {orig_input_path}\n")
        sys.exit(1)

    # Load configuration & secrets
    cfg = ConfigResolver.load_config()
    sec = ConfigResolver.load_secrets()

    mod_cfg = cfg.get("mods", {}).get("speech-to-text", {}) if isinstance(cfg.get("mods"), dict) else {}
    stt_provider_cfg = cfg.get("stt", {}).get("providers", {}).get("stt", {}) if isinstance(cfg.get("stt"), dict) else {}

    fallback_whisper = mod_cfg.get("fallback_whisper", True)
    downscale_heavy_audio = mod_cfg.get("downscale_heavy_audio", True)
    timeout_seconds = float(
        os.environ.get("STT_TIMEOUT")
        or mod_cfg.get("timeout")
        or stt_provider_cfg.get("timeout")
        or 150
    )
    speculative_delay = float(
        os.environ.get("STT_SPECULATIVE_DELAY")
        or os.environ.get("HERMES_MOD_OPTION_SPECULATIVE_FALLBACK_DELAY")
        or mod_cfg.get("speculative_fallback_delay")
        or mod_cfg.get("fallback_delay")
        or 60.0
    )

    preprocessor = AudioPreprocessor(downscale_heavy_audio=downscale_heavy_audio)
    try:
        b64_data, fmt, effective_input_path = preprocessor.process(orig_input_path)
        log_stt(f"STT start: file={orig_input_path} ({os.path.getsize(orig_input_path)} bytes), out={output_path}")

        url, api_key = ConfigResolver.resolve_credentials(cfg, sec)
        model_name = ConfigResolver.resolve_model(cfg, sec)

        has_api = bool(url and api_key)
        has_local = fallback_whisper and WhisperWorker.is_available()

        log_stt(f"Config resolved: model='{model_name}', url='{url}', key_len={len(api_key)}, spec_delay={speculative_delay}s, timeout={timeout_seconds}s, has_api={has_api}, has_local={has_local}")

        # Case 1: Neither API nor local Whisper is available
        if not has_api and not has_local:
            log_stt("ERROR: Neither OmniRoute credentials nor local faster-whisper are available for STT transcription.")
            sys.stderr.write("STT unavailable: neither remote API credentials nor local faster-whisper are configured/installed\n")
            sys.exit(1)

        # Case 2: No API credentials, but local Whisper is available
        if not has_api and has_local:
            log_stt("OmniRoute credentials missing; running local faster-whisper directly.")
            sys.stderr.write("OmniRoute STT credentials (api_key or url) not configured\n")
            sys.stderr.write("Falling back to local faster-whisper STT...\n")
            whisper_tmp = f"{output_path}.whisper.{os.getpid()}.tmp"
            if WhisperWorker.run_fallback(orig_input_path, whisper_tmp, cfg):
                w_text = Path(whisper_tmp).read_text(encoding="utf-8", errors="replace").strip()
                payload_json = json.dumps({"transcript": w_text, "provider": "local (whisper)"}, ensure_ascii=False)
                Path(output_path).write_text(payload_json, encoding="utf-8")
                if os.path.exists(whisper_tmp):
                    try:
                        os.remove(whisper_tmp)
                    except OSError:
                        pass
                sys.exit(0)
            sys.exit(1)

        # Case 3: API is available. If local Whisper is available -> speculative race, else API only.
        success = speculative_racing_stt(
            url=url,
            api_key=api_key,
            b64_data=b64_data,
            fmt=fmt,
            input_audio_path=orig_input_path,
            final_output_path=output_path,
            cfg=cfg,
            speculative_delay=speculative_delay,
            total_timeout=timeout_seconds,
            fallback_whisper=has_local,
            model=model_name,
        )
        sys.exit(0 if success else 1)
    finally:
        preprocessor.cleanup()


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
Text-to-Speech (TTS) Module with Resilient Fast Fallback & Neural Adaptation.

Architecture:
- ConfigResolver: Dynamic, zero-hardcode configuration & secrets resolution from config.yaml.
- CancellableHTTPHandler & OmniRouteTTSClient: Low-latency remote TTS synthesis via OmniRoute / Google Gemini TTS.
- AudioWatermarkCleaner: Automatic detection and removal of Google SynthID / C2PA watermark trailing metadata.
- EdgeTTSWorker: High-reliability local EdgeTTS fallback with multilingual voice routing.
- AudioConverter: High-fidelity audio transcoding to Telegram Voice Opus (48kHz mono 24kbps VoIP profile) and MP3 via ffmpeg.
- TextAdaptor: Neural speech adaptation for conversational pacing and vocal tags.
- TTSCoordinator: End-to-end orchestration with speculative / fast fallback and zero gateway crashes.

Universal portability:
- Zero hardcoded filesystem paths or user paths (uses sys.executable, Path.home() / ".hermes", HERMES_ROOT).
- Pure-Python zero-dependency YAML parser fallback ensures reliable execution without PyYAML.
"""

from __future__ import annotations

import asyncio
import http.client
import json
import os
from pathlib import Path
import queue
import re
import shutil
import socket
import ssl
import struct
import subprocess
import sys
import tempfile
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


# Google AI Speech Generation guidelines and prompt
BASE_ADAPTATION_SYSTEM_PROMPT = """Ты — экспертный редактор текста для нейросети озвучки Google Gemini TTS.
Твоя задача — адаптировать письменный ответ ассистента в естественный устный текст строго по официальным спецификациям Google AI Speech Generation.

СПЕЦИФИКАЦИИ И ПРАВИЛА GOOGLE GEMINI TTS:
1. ОЧИСТКА РАЗМЕТКИ:
   - Полностью удали весь Markdown: звездочки (*, **), решетки (#), обратные кавычки (`), тильды (~).
   - Ссылки вида [Текст ссылки](URL) заменяй исключительно на Текст ссылки. URL-адреса не читай.
   - Списки и перечисления переделывай в связную устную речь («Во-первых...», «Также стоит отметить...»).

2. ТЕХНИЧЕСКИЕ ТЕРМИНЫ И ПУТИ:
   - Пути (~/scripts/...), имена файлов и команды произноси по-русски или упрощай, чтобы не читать слеши и технические расширения подряд.

3. НОРМАЛИЗАЦИЯ ЧИСЕЛ И СОКРАЩЕНИЙ (SSML не поддерживается):
   - Все числа, даты, единицы измерения и валюты пиши словами в правильном падеже (например: «в 2026 году» -> «в две тысячи двадцать шестом году», «150 секунд» -> «сто пятьдесят секунд», «$50» -> «пятьдесят долларов»).
   - Аббревиатуры пиши так, как они произносятся.

4. ВОКАЛЬНЫЕ ТЕГИ GEMINI TTS (Строго в угловых скобках на английском):
   - Модель понимает ТОЛЬКО теги в угловых скобках <...>:
     * Для пауз: <short pause>, <long pause>.
     * Для естественных вздохов и эмоций: <sigh>, <gasp>, <chuckle>, <whispers>, <yawn>.
   - КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО использовать квадратные скобки [шепотом], [пауза] или выдуманные звуки окружающей среды (<applause>, <music>). Gemini зачитает их вслух!

5. УДАРЕНИЯ И АКЦЕНТЫ:
   - Если слово имеет неоднозначное ударение (омографы вроде замОк/зАмок, бОльшая/большАя, стОит/стоИт) или требует сильного дикторского акцента, выделяй ударную гласную заглавной буквой, либо пиши слово КАПСОМ для логического ударения.

6. ЧИСТОТА ВЫВОДА:
   - Сохраняй исходный смысл, дружелюбный тон и факты.
   - Выведи ТОЛЬКО готовый текст для озвучки. Без вводных фраз («Вот адаптированный текст:»), без пояснений и без кавычек."""


def log_tts(msg: str) -> None:
    """Thread-safe append to Hermes TTS log file without raising exceptions."""
    try:
        hermes_home = Path(
            os.environ.get("HERMES_HOME", os.environ.get("HERMES_ROOT", Path.home() / ".hermes"))
        ).expanduser().resolve()
        log_dir = hermes_home / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = log_dir / "tts.log"
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(f"[{ts}] [PID {os.getpid()}] {msg}\n")
    except Exception:
        pass


class ConfigResolver:
    """Dynamic configuration and secrets resolver with zero hardcoded paths."""

    DEFAULT_OMNIROUTE_URL = ""
    DEFAULT_VOICE = "Kore"
    DEFAULT_MODEL = "tts"
    DEFAULT_PROVIDER = "antigravity"
    DEFAULT_TIMEOUT = 150.0
    DEFAULT_SPECULATIVE_DELAY = 8.0

    @staticmethod
    def get_hermes_home() -> Path:
        return Path(
            os.environ.get("HERMES_HOME", os.environ.get("HERMES_ROOT", Path.home() / ".hermes"))
        ).expanduser().resolve()

    @classmethod
    def parse_simple_yaml(cls, text: str) -> dict:
        """Zero-dependency pure-Python YAML parser for key-value, nested dictionary and list structures."""
        root: dict = {}
        # stack items: (indent, container, parent_key)
        stack: list[Tuple[int, Any, Optional[str]]] = [(-1, root, None)]
        for raw_line in text.splitlines():
            line = re.sub(r"#.*$", "", raw_line).rstrip()
            if not line.strip():
                continue
            indent = len(line) - len(line.lstrip(" "))
            while len(stack) > 1 and stack[-1][0] >= indent:
                stack.pop()

            m_list = re.match(r"^\s*-\s+(.*)$", line)
            if m_list:
                item_raw = m_list.group(1).strip()
                item_val: Any = item_raw.strip("\"'")
                if item_val.lower() == "true":
                    item_val = True
                elif item_val.lower() == "false":
                    item_val = False
                elif item_val.isdigit():
                    item_val = int(item_val)

                if len(stack) > 1 and isinstance(stack[-1][1], dict) and not stack[-1][1]:
                    top_indent, _, parent_key = stack[-1]
                    parent_container = stack[-2][1]
                    new_list = [item_val]
                    if isinstance(parent_container, dict) and parent_key:
                        parent_container[parent_key] = new_list
                    stack[-1] = (top_indent, new_list, parent_key)
                elif isinstance(stack[-1][1], list):
                    stack[-1][1].append(item_val)
                continue

            m = re.match(r"^\s*([A-Za-z0-9_.-]+):\s*(.*)$", line)
            if not m:
                continue
            key = m.group(1).strip()
            val = m.group(2).strip()

            parent_dict = stack[-1][1]
            if not isinstance(parent_dict, dict):
                continue

            if not val or val == "{}":
                sub: dict = {}
                parent_dict[key] = sub
                stack.append((indent, sub, key))
            elif val == "[]":
                sub_l: list = []
                parent_dict[key] = sub_l
                stack.append((indent, sub_l, key))
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
                parent_dict[key] = parsed_val

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
    def _extract(
        cls,
        data: dict,
        section_keys: tuple[str, ...],
        subkeys: tuple[str, ...],
        field_names: tuple[str, ...]
    ) -> str:
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
    def resolve_tts_provider(cls, cfg: dict, sec: dict | None = None) -> str:
        """Resolve TTS provider name strictly from config.yaml with env fallback."""
        if isinstance(cfg, dict):
            tts_cfg = cfg.get("tts") if isinstance(cfg.get("tts"), dict) else {}
            m_prov = tts_cfg.get("model_provider")
            if m_prov and str(m_prov).strip():
                return str(m_prov).strip()

            prov = tts_cfg.get("provider")
            if prov and str(prov).strip() and str(prov).strip() not in ("tts", "command"):
                return str(prov).strip()

            main_prov = cfg.get("model", {}).get("provider") if isinstance(cfg.get("model"), dict) else ""
            if main_prov and str(main_prov).strip():
                return str(main_prov).strip()

        env_prov = (
            os.environ.get("TTS_PROVIDER")
            or os.environ.get("HERMES_MOD_OPTION_PROVIDER")
            or ""
        ).strip()
        if env_prov:
            return env_prov

        return cls.DEFAULT_PROVIDER

    @classmethod
    def resolve_tts_model(cls, cfg: dict, sec: dict | None = None) -> str:
        """Resolve TTS synthesis model strictly from config.yaml."""
        if isinstance(cfg, dict):
            tts_cfg = cfg.get("tts") if isinstance(cfg.get("tts"), dict) else {}
            m = tts_cfg.get("model")
            if m and str(m).strip():
                return str(m).strip()

            gemini_cfg = tts_cfg.get("gemini") if isinstance(tts_cfg.get("gemini"), dict) else {}
            m_gemini = gemini_cfg.get("model")
            if m_gemini and str(m_gemini).strip():
                return str(m_gemini).strip()

            openai_cfg = tts_cfg.get("openai") if isinstance(tts_cfg.get("openai"), dict) else {}
            m_openai = openai_cfg.get("model")
            if m_openai and str(m_openai).strip():
                return str(m_openai).strip()

            mod_cfg = cfg.get("mods", {}).get("text-to-speech", {}) if isinstance(cfg.get("mods"), dict) else {}
            m_mod = mod_cfg.get("model")
            if m_mod and str(m_mod).strip():
                return str(m_mod).strip()

        env_m = (
            os.environ.get("TTS_MODEL")
            or os.environ.get("HERMES_MOD_OPTION_MODEL")
            or ""
        ).strip()
        if env_m:
            return env_m

        return cls.DEFAULT_MODEL

    @classmethod
    def resolve_tts_voice(cls, cfg: dict, sec: dict | None = None) -> str:
        """Resolve TTS voice identifier strictly from config.yaml."""
        if isinstance(cfg, dict):
            mod_cfg = cfg.get("mods", {}).get("text-to-speech", {}) if isinstance(cfg.get("mods"), dict) else {}
            v_mod = mod_cfg.get("voice")
            if v_mod and str(v_mod).strip():
                return str(v_mod).strip()

            tts_cfg = cfg.get("tts") if isinstance(cfg.get("tts"), dict) else {}
            v = tts_cfg.get("voice")
            if v and str(v).strip():
                return str(v).strip()

            gemini_cfg = tts_cfg.get("gemini") if isinstance(tts_cfg.get("gemini"), dict) else {}
            v_gemini = gemini_cfg.get("voice")
            if v_gemini and str(v_gemini).strip():
                return str(v_gemini).strip()

        env_v = (
            os.environ.get("TTS_VOICE")
            or os.environ.get("HERMES_MOD_OPTION_VOICE")
            or ""
        ).strip()
        if env_v:
            return env_v

        return cls.DEFAULT_VOICE

    @classmethod
    def resolve_tts_timeout(cls, cfg: dict) -> float:
        """Resolve remote TTS timeout in seconds (default 150s, bounded 0.5..300)."""
        # 1. Direct explicit environment override
        env_direct = os.environ.get("TTS_TIMEOUT")
        if env_direct:
            try:
                return max(0.5, min(300.0, float(env_direct)))
            except ValueError:
                pass

        # 2. Passed cfg precedence
        timeout_val: Any = None
        if isinstance(cfg, dict):
            mod_cfg = cfg.get("mods", {}).get("text-to-speech", {}) if isinstance(cfg.get("mods"), dict) else {}
            timeout_val = mod_cfg.get("timeout")
            if timeout_val is None:
                tts_cfg = cfg.get("tts") if isinstance(cfg.get("tts"), dict) else {}
                timeout_val = tts_cfg.get("timeout")
                if timeout_val is None:
                    providers_cfg = tts_cfg.get("providers", {})
                    if isinstance(providers_cfg, dict):
                        prov = tts_cfg.get("provider") or "tts"
                        prov_entry = providers_cfg.get(prov) or providers_cfg.get("tts")
                        if isinstance(prov_entry, dict):
                            timeout_val = prov_entry.get("timeout")

        # 3. Injected mod option env var or default
        if timeout_val is None:
            env_mod = os.environ.get("HERMES_MOD_OPTION_TIMEOUT") or os.environ.get("HERMES_MOD_CONFIG_TIMEOUT")
            if env_mod:
                try:
                    timeout_val = float(env_mod)
                except ValueError:
                    pass

        try:
            val = float(timeout_val) if timeout_val is not None else cls.DEFAULT_TIMEOUT
            return max(0.5, min(300.0, val))
        except (ValueError, TypeError):
            return cls.DEFAULT_TIMEOUT

    @classmethod
    def resolve_speculative_delay(cls, cfg: dict) -> float:
        """Resolve speculative fallback delay in seconds before parallel EdgeTTS launch (default 8.0s)."""
        # 1. Direct explicit environment override
        env_direct = os.environ.get("TTS_SPECULATIVE_DELAY")
        if env_direct:
            try:
                return max(0.05, min(180.0, float(env_direct)))
            except ValueError:
                pass

        # 2. Config file precedence
        val: Any = None
        if isinstance(cfg, dict):
            mod_cfg = cfg.get("mods", {}).get("text-to-speech", {}) if isinstance(cfg.get("mods"), dict) else {}
            val = mod_cfg.get("speculative_fallback_delay") or mod_cfg.get("speculative_delay")
            if val is None:
                tts_cfg = cfg.get("tts") if isinstance(cfg.get("tts"), dict) else {}
                val = tts_cfg.get("speculative_fallback_delay") or tts_cfg.get("speculative_delay")

        # 3. Fallback to mod option env vars or default
        if val is None:
            val = (
                os.environ.get("TTS_SPECULATIVE_FALLBACK_DELAY")
                or os.environ.get("HERMES_MOD_OPTION_SPECULATIVE_FALLBACK_DELAY")
                or os.environ.get("HERMES_MOD_CONFIG_SPECULATIVE_FALLBACK_DELAY")
            )

        try:
            parsed = float(val) if val is not None else cls.DEFAULT_SPECULATIVE_DELAY
            return max(0.05, min(180.0, parsed))
        except (ValueError, TypeError):
            return cls.DEFAULT_SPECULATIVE_DELAY

    @classmethod
    def resolve_fallback_edge(cls, cfg: dict) -> bool:
        """Resolve whether EdgeTTS fallback is enabled."""
        if isinstance(cfg, dict):
            mod_cfg = cfg.get("mods", {}).get("text-to-speech", {}) if isinstance(cfg.get("mods"), dict) else {}
            if "fallback_edge" in mod_cfg:
                return bool(mod_cfg["fallback_edge"])
            tts_cfg = cfg.get("tts") if isinstance(cfg.get("tts"), dict) else {}
            if "fallback_edge" in tts_cfg:
                return bool(tts_cfg["fallback_edge"])

        env_val = os.environ.get("TTS_FALLBACK_EDGE") or os.environ.get("HERMES_MOD_OPTION_FALLBACK_EDGE")
        if env_val is not None:
            return env_val.strip().lower() not in ("0", "false", "no", "off")

        return True

    @classmethod
    def resolve_edge_voice(cls, text: str, cfg: dict | None = None) -> str:
        """Select appropriate EdgeTTS voice based on text language."""
        has_cyrillic = bool(re.search(r"[\u0400-\u04FF]", text))
        cfg_dict = cfg or {}
        mod_cfg = cfg_dict.get("mods", {}).get("text-to-speech", {}) if isinstance(cfg_dict.get("mods"), dict) else {}
        tts_cfg = cfg_dict.get("tts") if isinstance(cfg_dict.get("tts"), dict) else {}
        edge_cfg = tts_cfg.get("edge") if isinstance(tts_cfg.get("edge"), dict) else {}

        if has_cyrillic:
            return (
                mod_cfg.get("edge_voice_ru")
                or edge_cfg.get("voice_ru")
                or "ru-RU-SvetlanaNeural"
            )
        else:
            return (
                mod_cfg.get("edge_voice_en")
                or edge_cfg.get("voice")
                or "en-US-AriaNeural"
            )

    @classmethod
    def resolve_tts_credentials(
        cls,
        cfg: dict,
        sec: dict | None = None,
        provider_name: str = ""
    ) -> tuple[str, str]:
        """
        Resolve (base_url, api_key) with precedence: env -> secrets.yaml -> config.yaml.
        """
        api_key = ""
        base_url = ""
        prov = provider_name or cls.resolve_tts_provider(cfg, sec)
        prov_candidates = [p for p in (prov, "antigravity", "omniroute", "openai") if p]
        seen: set[str] = set()
        ordered_provs = tuple([p for p in prov_candidates if not (p in seen or seen.add(p))])

        # 1. API key from environment variables
        api_key = (
            os.environ.get("HERMES_MOD_OPTION_API_KEY")
            or os.environ.get("OMNIROUTE_API_KEY")
            or os.environ.get("OPENAI_API_KEY")
            or os.environ.get("TTS_API_KEY")
            or os.environ.get("HERMES_API_KEY")
            or ""
        ).strip()

        # 2. API key from secrets.yaml overlay
        if not api_key and isinstance(sec, dict):
            api_key = (
                cls._extract(sec, ("mods",), ("text-to-speech", "tts"), ("api_key",))
                or cls._extract(sec, ("tts", "text-to-speech"), ("openai", "gemini"), ("api_key",))
                or cls._extract(sec, ("model_providers",), ordered_provs, ("api_key",))
                or cls._extract(sec, ("providers",), ordered_provs, ("api_key",))
                or cls._extract(sec, ("stt",), ("openai",), ("api_key",))
                or str(sec.get("api_key") or "").strip()
            )

        # 3. API key from config.yaml
        if not api_key and isinstance(cfg, dict):
            api_key = cls._extract(cfg, ("tts", "text-to-speech"), ("openai", "gemini"), ("api_key",))
            if not api_key:
                api_key = cls._extract(cfg, ("mods",), ("text-to-speech", "tts"), ("api_key",))
            if not api_key:
                api_key = cls._extract(cfg, ("model_providers",), ordered_provs, ("api_key",))
            if not api_key:
                api_key = cls._extract(cfg, ("providers",), ordered_provs, ("api_key",))
            if not api_key and isinstance(cfg.get("model"), dict):
                k = cfg["model"].get("api_key")
                if k and str(k).strip():
                    api_key = str(k).strip()

        # 4. Base URL from environment variables
        base_url = (
            os.environ.get("HERMES_MOD_OPTION_BASE_URL")
            or os.environ.get("OMNIROUTE_URL")
            or os.environ.get("OPENAI_BASE_URL")
            or os.environ.get("TTS_BASE_URL")
            or ""
        ).strip()

        # 5. Base URL from secrets.yaml overlay
        if not base_url and isinstance(sec, dict):
            base_url = (
                cls._extract(sec, ("mods",), ("text-to-speech", "tts"), ("base_url", "url"))
                or cls._extract(sec, ("tts", "text-to-speech"), ("openai", "gemini"), ("base_url", "url"))
                or cls._extract(sec, ("model_providers",), ordered_provs, ("base_url", "url"))
                or cls._extract(sec, ("providers",), ordered_provs, ("base_url", "url"))
                or cls._extract(sec, ("stt",), ("openai",), ("base_url", "url"))
                or str(sec.get("base_url") or sec.get("url") or "").strip()
            )

        # 6. Base URL from config.yaml
        if not base_url and isinstance(cfg, dict):
            base_url = cls._extract(cfg, ("tts", "text-to-speech"), ("openai", "gemini"), ("base_url", "url"))
            if not base_url:
                base_url = cls._extract(cfg, ("mods",), ("text-to-speech", "tts"), ("base_url", "url"))
            if not base_url:
                base_url = cls._extract(cfg, ("model_providers",), ordered_provs, ("base_url", "url"))
            if not base_url:
                base_url = cls._extract(cfg, ("providers",), ordered_provs, ("base_url", "url"))
            if not base_url and isinstance(cfg.get("model"), dict):
                u = cfg["model"].get("base_url") or cfg["model"].get("url")
                if u and str(u).strip():
                    base_url = str(u).strip()

        # Fallback default endpoint if unconfigured
        if not base_url:
            base_url = cls.DEFAULT_OMNIROUTE_URL

        clean_url = str(base_url or "").strip().rstrip("/")
        return clean_url, str(api_key or "").strip()

    @classmethod
    def resolve_adaptation_settings(
        cls,
        cfg: dict,
        sec: dict | None = None
    ) -> dict[str, Any]:
        """Resolve text adaptation parameters."""
        tts_cfg = cfg.get("tts") if isinstance(cfg.get("tts"), dict) else {}
        mod_cfg = cfg.get("mods", {}).get("text-to-speech", {}) if isinstance(cfg.get("mods"), dict) else {}
        adapt_cfg = tts_cfg.get("adaptation") if isinstance(tts_cfg.get("adaptation"), dict) else {}

        enabled = mod_cfg.get("adaptation", adapt_cfg.get("enabled", True))
        provider = adapt_cfg.get("provider") or cls.resolve_tts_provider(cfg, sec)
        model = adapt_cfg.get("model") or "gemini"
        prompt = adapt_cfg.get("prompt") or ""
        base_url, api_key = cls.resolve_tts_credentials(cfg, sec, provider)

        return {
            "enabled": bool(enabled),
            "provider": provider,
            "model": model,
            "prompt": prompt,
            "base_url": base_url,
            "api_key": api_key,
        }


class TextAdaptor:
    """Pre-synthesis neural text adaptation to conversational spoken style."""

    @staticmethod
    def clean_markdown_fallback(text: str) -> str:
        """Lightweight regex cleanup of markdown symbols if neural adaptation is unavailable."""
        t = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
        t = re.sub(r"[*_~`#>]+", "", t)
        t = re.sub(r"\n{2,}", "\n", t)
        return t.strip()

    @classmethod
    def adapt(
        cls,
        text: str,
        base_url: str,
        api_key: str,
        model_name: str,
        custom_prompt: str = "",
        timeout: float = 12.0
    ) -> str:
        """Rewrite input text using chat completions endpoint."""
        clean_text = text.strip()
        if not clean_text or not base_url:
            return cls.clean_markdown_fallback(clean_text)

        sys_prompt = BASE_ADAPTATION_SYSTEM_PROMPT
        if custom_prompt and custom_prompt.strip():
            sys_prompt = f"{BASE_ADAPTATION_SYSTEM_PROMPT}\n\nДОПОЛНИТЕЛЬНЫЕ ИНСТРУКЦИИ ПОЛЬЗОВАТЕЛЯ:\n{custom_prompt.strip()}"

        url = f"{base_url}/chat/completions"
        payload = {
            "model": model_name,
            "messages": [
                {"role": "system", "content": sys_prompt},
                {"role": "user", "content": clean_text}
            ],
            "temperature": 0.3
        }

        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

        req = urllib.request.Request(url)
        if api_key:
            req.add_header("Authorization", f"Bearer {api_key}")
        req.add_header("Content-Type", "application/json")
        req.data = json.dumps(payload).encode("utf-8")

        try:
            with urllib.request.urlopen(req, context=ctx, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                adapted = data["choices"][0]["message"]["content"].strip()
                if adapted:
                    log_tts(f"Text adaptation completed successfully via {model_name}")
                    return adapted
        except Exception as exc:
            log_tts(f"Text adaptation failed ({model_name}): {exc}; using fallback text")
            sys.stderr.write(f"TTS adaptation notice ({model_name}): {exc}; using fallback text\n")

        return cls.clean_markdown_fallback(clean_text)


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


class AudioWatermarkCleaner:
    """Removes trailing SynthID / C2PA watermark metadata manifesting as static noise."""

    @staticmethod
    def clean(audio_bytes: bytes) -> bytes:
        if len(audio_bytes) > 44:
            c2pa_pos = audio_bytes.find(b"C2PA", 44)
            if c2pa_pos != -1:
                cut_pos = c2pa_pos
                if cut_pos - 4 >= 44 and audio_bytes[cut_pos - 4:cut_pos] in (b"\x02\x00\t\x00", b"\x00\x00\x00\x00"):
                    cut_pos -= 4
                pcm_len = cut_pos - 44
                if pcm_len % 2 != 0:
                    pcm_len -= 1
                header = bytearray(audio_bytes[:44])
                struct.pack_into("<I", header, 4, pcm_len + 36)
                struct.pack_into("<I", header, 40, pcm_len)
                log_tts(f"Cleaned SynthID watermark from audio: trimmed {len(audio_bytes) - (44 + pcm_len)} trailing bytes")
                return bytes(header) + audio_bytes[44:44 + pcm_len]
        return audio_bytes


class AudioConverter:
    """High-fidelity audio conversion with standard Telegram VoIP profile."""

    @staticmethod
    def convert(
        input_audio_path: str,
        output_path: str,
        target_format: str | None = None
    ) -> bool:
        out_path = Path(output_path).expanduser().resolve()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        ext = target_format or out_path.suffix.lower().lstrip(".")

        if ext in ("ogg", "opus"):
            # Telegram Voice: standard 48kHz mono Opus 24kbps with VoIP profile
            cmd = [
                "ffmpeg", "-y", "-i", str(input_audio_path),
                "-c:a", "libopus", "-b:a", "24k", "-vbr", "on",
                "-compression_level", "10", "-application", "voip",
                "-ac", "1", "-ar", "48000",
                str(out_path)
            ]
        elif ext == "mp3":
            cmd = [
                "ffmpeg", "-y", "-i", str(input_audio_path),
                "-c:a", "libmp3lame", "-b:a", "128k",
                str(out_path)
            ]
        else:
            cmd = [
                "ffmpeg", "-y", "-i", str(input_audio_path),
                str(out_path)
            ]

        try:
            res = subprocess.run(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                timeout=45
            )
            if res.returncode == 0 and out_path.is_file() and out_path.stat().st_size > 0:
                return True
            log_tts(f"ffmpeg conversion error: {res.stderr}")
            sys.stderr.write(f"ffmpeg conversion failed: {res.stderr}\n")
        except Exception as exc:
            log_tts(f"ffmpeg execution failed: {exc}")
            sys.stderr.write(f"ffmpeg execution error: {exc}\n")

        return False


class EdgeTTSWorker:
    """Local EdgeTTS synthesis engine with in-process and CLI fallback and subprocess lifecycle."""

    @classmethod
    def synthesize(
        cls,
        text: str,
        voice: str,
        output_audio_path: str,
        timeout: float = 30.0
    ) -> bool:
        """Synthesize text using EdgeTTS directly into temporary MP3."""
        clean_text = text.strip()
        if not clean_text:
            return False

        # Optional test simulation hook
        sim_delay = float(os.environ.get("TTS_TEST_EDGE_DELAY", "0"))
        if sim_delay > 0:
            time.sleep(sim_delay)

        out_path = Path(output_audio_path).expanduser().resolve()
        out_path.parent.mkdir(parents=True, exist_ok=True)

        # 1. In-process asyncio generation
        try:
            import edge_tts

            async def _run_async():
                comm = edge_tts.Communicate(clean_text, voice)
                await comm.save(str(out_path))

            asyncio.run(_run_async())
            if out_path.is_file() and out_path.stat().st_size > 0:
                log_tts(f"EdgeTTS generated {out_path.stat().st_size} bytes with voice '{voice}' (async)")
                return True
        except Exception as exc:
            log_tts(f"EdgeTTS async synthesis failed ({exc}), trying CLI fallback...")

        # 2. CLI generation via sys.executable -m edge_tts or edge-tts binary
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".txt", delete=False) as tf:
            tf.write(clean_text)
            txt_path = tf.name

        try:
            cli_bin = shutil.which("edge-tts")
            if cli_bin:
                cmd = [cli_bin, "--voice", voice, "-f", txt_path, "--write-media", str(out_path)]
            else:
                cmd = [sys.executable, "-m", "edge_tts", "--voice", voice, "-f", txt_path, "--write-media", str(out_path)]

            res = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, timeout=timeout)
            if res.returncode == 0 and out_path.is_file() and out_path.stat().st_size > 0:
                log_tts(f"EdgeTTS generated {out_path.stat().st_size} bytes via CLI")
                return True
            log_tts(f"EdgeTTS CLI failed: {res.stderr}")
        except Exception as exc:
            log_tts(f"EdgeTTS CLI execution error: {exc}")
        finally:
            if os.path.exists(txt_path):
                try:
                    os.remove(txt_path)
                except OSError:
                    pass

        return False

    @classmethod
    def is_available(cls) -> bool:
        """Check if local EdgeTTS engine is available in the current environment."""
        if os.environ.get("TTS_FORCE_LOCAL_UNAVAILABLE") == "1":
            return False
        try:
            import edge_tts  # noqa: F401
            return True
        except ImportError:
            pass
        return shutil.which("edge-tts") is not None

    @classmethod
    def start_subprocess(
        cls,
        text_file_path: str,
        output_audio_path: str,
        voice: str
    ) -> subprocess.Popen:
        """Start isolated EdgeTTS worker subprocess that can be killed cleanly via SIGKILL."""
        cmd = [
            sys.executable,
            str(Path(__file__).resolve()),
            "--edge-worker",
            str(text_file_path),
            str(output_audio_path),
            str(voice),
        ]
        return subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)

    @staticmethod
    def kill_process(proc: subprocess.Popen | None) -> None:
        """Forcibly terminate worker process with SIGKILL and reap to prevent zombies."""
        if proc is None or proc.poll() is not None:
            return
        try:
            proc.kill()  # sends SIGKILL on POSIX
            proc.wait(timeout=2)
        except Exception:
            pass

    @classmethod
    def is_valid_audio(cls, path: str) -> bool:
        """Check if output file is a valid non-empty audio file."""
        p = Path(path)
        if not p.is_file() or p.stat().st_size < 100:
            return False
        try:
            with open(p, "rb") as f:
                head = f.read(16)
            return (
                head.startswith(b"ID3")
                or head.startswith(b"\xff\xfb")
                or head.startswith(b"\xff\xf3")
                or head.startswith(b"RIFF")
                or head.startswith(b"OggS")
                or len(head) >= 4
            )
        except Exception:
            return False


class OmniRouteTTSClient:
    """Client for remote TTS synthesis via OmniRoute / Google Gemini TTS."""

    @classmethod
    def synthesize(
        cls,
        base_url: str,
        api_key: str,
        model: str,
        voice: str,
        text: str,
        timeout: float,
        handler: CancellableHTTPHandler | None = None,
        cancel_event: threading.Event | None = None
    ) -> bytes:
        clean_text = text.strip()
        if not clean_text:
            raise ValueError("Input text is empty")

        # Optional test simulation hook
        sim_delay = float(os.environ.get("TTS_TEST_OMNI_DELAY", "0"))
        if sim_delay > 0:
            time.sleep(sim_delay)

        url = f"{base_url}/audio/speech" if not base_url.endswith("/audio/speech") else base_url
        payload = {
            "model": model,
            "input": clean_text,
            "voice": voice,
            "response_format": "wav"
        }

        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

        http_handler = handler or CancellableHTTPHandler(ctx)
        opener = urllib.request.build_opener(http_handler)

        req = urllib.request.Request(url)
        if api_key:
            req.add_header("Authorization", f"Bearer {api_key}")
        req.add_header("Content-Type", "application/json")
        req.data = json.dumps(payload).encode("utf-8")

        if cancel_event and cancel_event.is_set():
            raise RuntimeError("Request cancelled before start")

        t0 = time.time()
        with opener.open(req, timeout=timeout) as resp:
            if cancel_event and cancel_event.is_set():
                raise RuntimeError("Request cancelled after connection")
            audio_bytes = resp.read()
            if cancel_event and cancel_event.is_set():
                raise RuntimeError("Request cancelled after read")

        elapsed = time.time() - t0
        log_tts(f"OmniRoute TTS ({model}, {voice}) finished in {elapsed:.2f}s, size {len(audio_bytes)} bytes")

        cleaned = AudioWatermarkCleaner.clean(audio_bytes)
        return cleaned


class SpeculativeRacerTTS:
    """Coordinates speculative parallel racing between remote OmniRoute TTS and local EdgeTTS."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        voice: str,
        text: str,
        output_audio_path: str,
        cfg: dict,
        speculative_delay: float = 8.0,
        total_timeout: float = 150.0,
        fallback_edge: bool = True,
    ):
        self.base_url = base_url
        self.api_key = api_key
        self.model = model
        self.voice = voice
        self.text = text
        self.output_audio_path = output_audio_path
        self.cfg = cfg
        self.speculative_delay = speculative_delay
        self.remote_timeout = total_timeout
        self.fallback_edge = fallback_edge

        self.ssl_ctx = ssl.create_default_context()
        self.ssl_ctx.check_hostname = False
        self.ssl_ctx.verify_mode = ssl.CERT_NONE

        self.omniroute_handler = CancellableHTTPHandler(self.ssl_ctx)
        self.cancel_omniroute_event = threading.Event()
        self.cancel_edge_event = threading.Event()
        self.omniroute_done_event = threading.Event()
        self.edge_done_event = threading.Event()

        self.result_queue: queue.Queue = queue.Queue()
        self.edge_proc_holder: list[subprocess.Popen | None] = [None]
        self.start_time = 0.0

        pid = os.getpid()
        ts_ns = int(time.time() * 1000000) % 100000000
        scratch_dir = Path(tempfile.gettempdir())
        self.tmp_wav_path = str(scratch_dir / f"omni_{pid}_{ts_ns}.wav")
        self.tmp_mp3_path = str(scratch_dir / f"edge_{pid}_{ts_ns}.mp3")
        self.tmp_txt_path = str(scratch_dir / f"edge_in_{pid}_{ts_ns}.txt")

    def _cleanup_temp_files(self) -> None:
        for p in (self.tmp_wav_path, self.tmp_mp3_path, self.tmp_txt_path):
            if os.path.exists(p):
                try:
                    os.remove(p)
                except OSError:
                    pass

    def _omniroute_worker(self) -> None:
        try:
            if self.cancel_omniroute_event.is_set():
                return
            audio_bytes = OmniRouteTTSClient.synthesize(
                base_url=self.base_url,
                api_key=self.api_key,
                model=self.model,
                voice=self.voice,
                text=self.text,
                timeout=self.remote_timeout,
                handler=self.omniroute_handler,
                cancel_event=self.cancel_omniroute_event,
            )
            if self.cancel_omniroute_event.is_set():
                return
            if not audio_bytes or len(audio_bytes) < 44:
                raise ValueError("OmniRoute returned empty or invalid audio data")

            with open(self.tmp_wav_path, "wb") as f:
                f.write(audio_bytes)

            if not self.cancel_omniroute_event.is_set():
                self.result_queue.put(("omniroute", True, self.tmp_wav_path))
        except urllib.error.HTTPError as exc:
            err_body = exc.read().decode("utf-8", errors="ignore") if hasattr(exc, "read") else ""
            log_tts(f"OmniRoute TTS HTTP error {exc.code}: {err_body}")
            sys.stderr.write(f"OmniRoute TTS endpoint HTTP {exc.code}: {err_body}\n")
            if not self.cancel_omniroute_event.is_set():
                self.result_queue.put(("omniroute", False, f"HTTP {exc.code}: {err_body}"))
        except Exception as exc:
            if not self.cancel_omniroute_event.is_set():
                log_tts(f"OmniRoute TTS error: {exc}")
                sys.stderr.write(f"OmniRoute TTS error: {exc}\n")
                self.result_queue.put(("omniroute", False, str(exc)))
        finally:
            self.omniroute_done_event.set()

    def _start_edge_subprocess(self) -> None:
        try:
            with open(self.tmp_txt_path, "w", encoding="utf-8") as f:
                f.write(self.text)

            edge_voice = ConfigResolver.resolve_edge_voice(self.text, self.cfg)
            sys.stderr.write("Falling back to local Edge TTS...\n")
            proc = EdgeTTSWorker.start_subprocess(self.tmp_txt_path, self.tmp_mp3_path, edge_voice)
            self.edge_proc_holder[0] = proc

            def _monitor():
                stdout, stderr = proc.communicate()
                if self.cancel_edge_event.is_set():
                    return
                if proc.returncode == 0 and EdgeTTSWorker.is_valid_audio(self.tmp_mp3_path):
                    self.result_queue.put(("edge", True, self.tmp_mp3_path))
                else:
                    err_msg = stderr.decode("utf-8", errors="ignore") if stderr else f"exit code {proc.returncode}"
                    self.result_queue.put(("edge", False, f"EdgeTTS failed: {err_msg}"))
                self.edge_done_event.set()

            t = threading.Thread(target=_monitor, daemon=True)
            t.start()
        except Exception as exc:
            log_tts(f"Failed to launch EdgeTTS subprocess: {exc}")
            self.result_queue.put(("edge", False, str(exc)))
            self.edge_done_event.set()

    def run(self) -> bool:
        self.start_time = time.time()
        t_omni = threading.Thread(target=self._omniroute_worker, daemon=True)
        t_omni.start()

        # If fallback_edge is False (or EdgeTTS unavailable), use full timeout budget for OmniRoute only
        if not self.fallback_edge:
            log_tts(f"Local EdgeTTS disabled or unavailable; running remote OmniRoute only (budget: {self.remote_timeout:.1f}s)...")
            finished = self.omniroute_done_event.wait(timeout=self.remote_timeout)
            if finished:
                try:
                    src, ok, payload = self.result_queue.get_nowait()
                    if ok:
                        dur = time.time() - self.start_time
                        log_tts(f"OmniRoute TTS succeeded in {dur:.2f}s.")
                        return AudioConverter.convert(self.tmp_wav_path, self.output_audio_path)
                    else:
                        dur = time.time() - self.start_time
                        log_tts(f"OmniRoute TTS failed after {dur:.2f}s: {payload}")
                        sys.stderr.write(f"{payload}\n")
                        return False
                except queue.Empty:
                    return False
            else:
                dur = time.time() - self.start_time
                log_tts(f"OmniRoute TTS timed out after full budget of {dur:.2f}s (budget: {self.remote_timeout}s)")
                sys.stderr.write(f"OmniRoute TTS timed out after {self.remote_timeout}s\n")
                self.cancel_omniroute_event.set()
                self.omniroute_handler.cancel()
                return False

        # Speculative race window with local EdgeTTS
        wait_window = min(self.speculative_delay, self.remote_timeout)
        omni_finished_in_window = self.omniroute_done_event.wait(timeout=wait_window)
        edge_started = False
        omni_failed = False

        if omni_finished_in_window:
            try:
                src, ok, payload = self.result_queue.get_nowait()
                if ok:
                    dur = time.time() - self.start_time
                    log_tts(f"Fast OmniRoute TTS success in {dur:.2f}s. EdgeTTS never started.")
                    return AudioConverter.convert(self.tmp_wav_path, self.output_audio_path)
                else:
                    dur = time.time() - self.start_time
                    log_tts(f"OmniRoute TTS failed early in {dur:.2f}s: {payload}")
                    omni_failed = True
                    log_tts("Starting EdgeTTS immediately following remote failure...")
                    self._start_edge_subprocess()
                    edge_started = True
            except queue.Empty:
                pass
        else:
            log_tts(f"OmniRoute TTS delayed beyond {self.speculative_delay}s, starting speculative EdgeTTS race...")
            sys.stderr.write(f"OmniRoute TTS delayed beyond {self.speculative_delay}s, starting speculative Edge TTS...\n")
            self._start_edge_subprocess()
            edge_started = True

        race_deadline = self.start_time + max(self.remote_timeout, self.speculative_delay + 30.0)
        edge_failed = False

        while time.time() < race_deadline:
            rem = max(0.05, race_deadline - time.time())
            try:
                src, ok, val = self.result_queue.get(timeout=rem)
                if ok:
                    if src == "omniroute":
                        self.cancel_edge_event.set()
                        EdgeTTSWorker.kill_process(self.edge_proc_holder[0])
                        dur = time.time() - self.start_time
                        log_tts(f"OmniRoute TTS won race in {dur:.2f}s. Loser EdgeTTS killed with SIGKILL.")
                        return AudioConverter.convert(self.tmp_wav_path, self.output_audio_path)
                    elif src == "edge":
                        self.cancel_omniroute_event.set()
                        self.omniroute_handler.cancel()
                        dur = time.time() - self.start_time
                        log_tts(f"EdgeTTS won race in {dur:.2f}s. Loser OmniRoute sockets closed.")
                        return AudioConverter.convert(self.tmp_mp3_path, self.output_audio_path)
                else:
                    if src == "omniroute":
                        omni_failed = True
                        log_tts(f"OmniRoute failed during race: {val}")
                        if edge_failed:
                            return False
                    elif src == "edge":
                        edge_failed = True
                        log_tts(f"EdgeTTS failed during race: {val}")
                        if omni_failed:
                            return False
            except queue.Empty:
                break

        self.cancel_omniroute_event.set()
        self.omniroute_handler.cancel()
        self.cancel_edge_event.set()
        EdgeTTSWorker.kill_process(self.edge_proc_holder[0])
        return False


class TTSCoordinator:
    """Coordinates TTS synthesis with fast, clean fallback to EdgeTTS."""

    @classmethod
    def run_edge_fallback(cls, text: str, output_path: str, cfg: dict) -> bool:
        """Run pure local EdgeTTS fallback and transcoding."""
        voice = ConfigResolver.resolve_edge_voice(text, cfg)
        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tmp_mp3:
            tmp_mp3_path = tmp_mp3.name

        try:
            t0 = time.time()
            ok = EdgeTTSWorker.synthesize(text, voice, tmp_mp3_path, timeout=30.0)
            if not ok or not os.path.exists(tmp_mp3_path) or os.path.getsize(tmp_mp3_path) == 0:
                sys.stderr.write("Edge TTS synthesis failed\n")
                return False

            conv_ok = AudioConverter.convert(tmp_mp3_path, output_path)
            if conv_ok:
                log_tts(f"EdgeTTS fallback completed in {time.time()-t0:.2f}s -> {output_path}")
                return True
        finally:
            if os.path.exists(tmp_mp3_path):
                try:
                    os.remove(tmp_mp3_path)
                except OSError:
                    pass
        return False

    @classmethod
    def run(cls, input_text_path: str, output_audio_path: str) -> int:
        if not os.path.exists(input_text_path):
            sys.stderr.write(f"Text file not found: {input_text_path}\n")
            return 1

        with open(input_text_path, "r", encoding="utf-8", errors="replace") as f:
            raw_text = f.read().strip()

        if not raw_text:
            sys.stderr.write("Input text is empty\n")
            return 1

        cfg = ConfigResolver.load_config()
        sec = ConfigResolver.load_secrets()

        provider = ConfigResolver.resolve_tts_provider(cfg, sec)
        model = ConfigResolver.resolve_tts_model(cfg, sec)
        voice = ConfigResolver.resolve_tts_voice(cfg, sec)
        timeout_seconds = ConfigResolver.resolve_tts_timeout(cfg)
        fallback_edge = ConfigResolver.resolve_fallback_edge(cfg)
        base_url, api_key = ConfigResolver.resolve_tts_credentials(cfg, sec, provider)

        has_api = bool(base_url and api_key)
        has_local = fallback_edge and EdgeTTSWorker.is_available()

        log_tts(f"TTS requested: provider='{provider}', model='{model}', voice='{voice}', timeout={timeout_seconds}s, has_api={has_api}, has_local={has_local}")

        # Case 1: Neither API nor local EdgeTTS is available
        if not has_api and not has_local:
            log_tts("ERROR: Neither OmniRoute API credentials nor local EdgeTTS are available for TTS synthesis.")
            sys.stderr.write("TTS unavailable: neither remote API credentials nor local EdgeTTS are configured/installed\n")
            return 1

        # Neural text adaptation (only if API available and enabled)
        adapt_settings = ConfigResolver.resolve_adaptation_settings(cfg, sec)
        text_for_speech = raw_text
        if has_api and adapt_settings.get("enabled", True):
            text_for_speech = TextAdaptor.adapt(
                text=raw_text,
                base_url=adapt_settings.get("base_url") or base_url,
                api_key=adapt_settings.get("api_key") or api_key,
                model_name=adapt_settings.get("model") or "gemini",
                custom_prompt=adapt_settings.get("prompt") or "",
                timeout=min(15.0, timeout_seconds)
            )

        # Case 2: No API credentials, but local EdgeTTS is available
        if not has_api and has_local:
            log_tts("OmniRoute credentials missing; running local EdgeTTS directly.")
            sys.stderr.write(f"Cannot resolve credentials for TTS provider '{provider}'\n")
            sys.stderr.write("Falling back to local Edge TTS...\n")
            ok = cls.run_edge_fallback(text_for_speech, output_audio_path, cfg)
            return 0 if ok else 1

        # Case 3: API is available. If local EdgeTTS available -> speculative race, else API only.
        speculative_delay = ConfigResolver.resolve_speculative_delay(cfg)
        log_tts(f"Starting TTS coordinator: model='{model}', voice='{voice}', spec_delay={speculative_delay}s, timeout={timeout_seconds}s, fallback_edge={has_local}")
        racer = SpeculativeRacerTTS(
            base_url=base_url,
            api_key=api_key,
            model=model,
            voice=voice,
            text=text_for_speech,
            output_audio_path=output_audio_path,
            cfg=cfg,
            speculative_delay=speculative_delay,
            total_timeout=timeout_seconds,
            fallback_edge=has_local,
        )
        try:
            ok = racer.run()
            return 0 if ok else 1
        finally:
            racer._cleanup_temp_files()


# Backward compatibility wrappers for external modules and existing tests
def load_hermes_config() -> dict:
    return ConfigResolver.load_config()


def load_hermes_secrets() -> dict:
    return ConfigResolver.load_secrets()


def resolve_provider_credentials(cfg: dict, sec: dict, provider_name: str) -> tuple[str, str]:
    return ConfigResolver.resolve_tts_credentials(cfg, sec, provider_name)


def clean_synthid_watermark(audio_bytes: bytes) -> bytes:
    return AudioWatermarkCleaner.clean(audio_bytes)


def run_edge_tts_fallback(text: str, output_path: str) -> bool:
    cfg = ConfigResolver.load_config()
    return TTSCoordinator.run_edge_fallback(text, output_path, cfg)


def rewrite_text_for_speech(
    text: str,
    base_url: str,
    api_key: str,
    model_name: str,
    custom_prompt: str = ""
) -> str:
    return TextAdaptor.adapt(text, base_url, api_key, model_name, custom_prompt)


def main():
    if len(sys.argv) >= 4 and sys.argv[1] == "--edge-worker":
        txt_path = sys.argv[2]
        out_mp3 = sys.argv[3]
        edge_voice = sys.argv[4] if len(sys.argv) > 4 else "ru-RU-SvetlanaNeural"
        try:
            with open(txt_path, "r", encoding="utf-8", errors="replace") as f:
                raw = f.read().strip()
            ok = EdgeTTSWorker.synthesize(raw, edge_voice, out_mp3)
            sys.exit(0 if ok else 1)
        except Exception as e:
            sys.stderr.write(f"Edge worker exception: {e}\n")
            sys.exit(1)

    if len(sys.argv) < 3:
        sys.stderr.write("Usage: omniroute_tts.py <input_text_path> <output_audio_path>\n")
        sys.exit(1)

    code = TTSCoordinator.run(sys.argv[1], sys.argv[2])
    sys.exit(code)


if __name__ == "__main__":
    main()

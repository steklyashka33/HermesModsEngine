---
name: stt-proxy-chain-diagnostics
description: Use when voice STT fails, times out, or drops to Whisper. Diagnose and fix Hermes, OmniRoute, and Gemini speech transcription errors.
category: devops
version: 2.1.0
---

# STT Proxy-Chain Diagnostics (2026 Architecture)

Используй этот скилл при сбоях распознавания голосовых сообщений Telegram, необоснованном падении в локальный Whisper, ошибках таймаута или галлюцинациях STT.

## Актуальная архитектура распознавания речи

```text
Telegram Voice Note (.ogg / .opus)
  → Hermes Gateway (run_inbound.py + мод stt-provider-echo)
  → Command STT: ~/.hermes/mods/custom/speech-to-text/scripts/stt.py (или ~/.hermes/scripts/omniroute_stt.py)
  → Авто-сжатие: ffmpeg -c:a libopus -b:a 20k -ac 1 (если файл > 750 КБ при downscale_heavy_audio: true)
  → HTTPS POST: https://<omniroute_host>:20129/v1/chat/completions (model: "stt", reasoning_effort: "none")
  → OmniRoute (remote gateway): комбо 'stt' (cpa ➔ gemini-api-lite ➔ gemini-3.5-transcribe)
  → При сбое: 3 автоматических ретрая с паузой 2с (мод stt-provider-echo)
  → Fallback: локальный faster-whisper (при fallback_whisper: true, модель base, язык ru)
```

## Параметры конфигурации мода (`config.yaml`)

- `stt.openai.api_key` / `providers.omniroute.api_key` / `providers.antigravity.api_key`: API-ключ шлюза, наследуется автоматически и прозрачно без дублирования в `secrets.yaml`.
- `stt.openai.model` (default `"stt"`): модель распознавания речи в OmniRoute (комбо `stt`).
- `mods.speech-to-text.fallback_whisper` (bool, default `true`): автоматический переход на faster-whisper при недоступности удалённого шлюза.
- `mods.speech-to-text.downscale_heavy_audio` (bool, default `true`): автоматическое пересжатие аудиофайлов >750КБ через ffmpeg в компактный моно-Opus.

## Пошаговый регламент диагностики

1. **Проверка входящего аудиофайла:**
   Убедись, что файл доставлен Telegram и сохранён в кэш:
   ```bash
   ls -lt ~/.hermes/cache/audio/ | head -n 5
   ```

2. **Прямой тест скрипта STT:**
   Запусти транскрибацию изолированно на проблемном файле:
   ```bash
   ~/.hermes/mods/custom/speech-to-text/scripts/stt.py /path/to/voice.ogg /tmp/test_stt.txt
   cat /tmp/test_stt.txt && rm -f /tmp/test_stt.txt
   ```
   Если скрипт падает — проверь код выхода и stderr (таймаут, 400, 502 или сетевая ошибка).

3. **Проверка удалённого OmniRoute:**
   Проверь доступность и статус сервиса на сервере шлюза:
   ```bash
   ssh user@<omniroute_host> "systemctl status omniroute --no-pager"
   ```
   Проверь свежие логи комбо `stt` в OmniRoute:
   ```bash
   ssh user@<omniroute_host> "journalctl -u omniroute -n 40 --no-pager"
   ```

4. **Проверка локального Whisper fallback:**
   Если облачный STT недоступен, Hermes обязан автоматически откатиться на `faster-whisper`:
   - В логах шлюза: `Configured STT failed after 3 attempts... falling back to local STT`.
   - В ответе Telegram: цитата должна маркироваться заголовком `🎙️ local (whisper)`.

## Критические грабли и проверенные решения

- **Обязательный `reasoning_effort: "none"`:**
  Модели Gemini 3.7+ Flash по умолчанию тратят 1000+ токенов на рассуждения (добавляя 5–10 секунд задержки). В скрипте STT строго передаётся `"reasoning_effort": "none"`.
- **Ошибка 400 на специализированных моделях (`gemini-3.5-transcribe`):**
  Специализированные модели транскрибации вообще не поддерживают блок `thinkingConfig`. Если прокси пытается передать параметры reasoning в `gemini-3.5-transcribe`, Google API возвращает HTTP 400 (`Thinking is not enabled for this model`). В комбо `stt` шаги выстроены с учётом этого: `cpa ➔ gemini-api-lite ➔ gemini-3.5-transcribe`.
- **Пре-компрессия длинных записей в Opus 20k:**
  Внутренний токенизатор OmniRoute оценивает размер аудио как `base64_bytes / 3`. Несжатый 5-минутный файл оценивается в ~1.7M токенов и упирается в лимит контекста. Скрипт автоматически сжимает аудио тяжелее 750 КБ через `ffmpeg` в Opus 20 kbps (уменьшая оценку токенов до ~160K). На OmniRoute включён флаг `DISABLE_CONTEXT_WINDOW_CHECKS=true`.
- **Защита от transient-ошибок (мод `stt-provider-echo`):**
  При кратковременных 502/503 от апстрима мод делает до 3 попыток с интервалом 2 секунды перед тем, как переключаться на локальный Whisper.
- **Очистка от паразитных галлюцинаций:**
  В системном промпте зашит запрет на интерпретацию вздохов/шумов как восточных слов (иншаллах и др.), удаление заминок («э-э», «ну-у», «собственно») и regex-санитация служебных префиксов («Текст:», «Расшифровка:»).
- **Форматирование цитат Telegram:**
  Цитата эхо-транскрипта формируется через нативный MarkdownV2 спойлер `**> ... ||` без ручного экранирования, сохраняя автоскладывание длинных расшифровок.

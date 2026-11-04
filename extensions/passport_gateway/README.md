# Passport Voice Gateway 0.2.0 (preview)

An independently installable gateway for Passport-compatible voice terminals. It authenticates devices, transcribes audio, calls your chatbot, and streams speech back. Choose an HTTP backend for your own application, or the existing Unix-socket adapter for Dafeiyu. The wheel contains only `passport_gateway`; MaiBot, QQ adapters, personas and conversation databases are not dependencies.

**Requirements:** Python 3.11+ on Linux or macOS; Python 3.12 is verified. A real terminal must implement the existing `dafeiyu.voice.v1` WebSocket protocol. Firmware and hardware are separate. This is a preview release, not an app-store plugin that automatically connects every chatbot.

## Install and try without hardware or API charges

```sh
git clone https://github.com/ChaoZhang173/chatbot-qq-maibot.git
cd chatbot-qq-maibot
python3 -m venv .venv
. .venv/bin/activate
python -m pip install .
passport-smoke
```

`passport-smoke` starts temporary loopback WebSocket and HTTP servers, authenticates a synthetic device, sends synthetic PCM, and verifies the text/audio response. ASR/TTS are simulated and the backend echoes text. It does not use a model, microphone or paid API. All servers and temporary bindings are removed on exit. Success is not a real speech or hardware acceptance test.

Nothing has been published to PyPI; install from this repository or a wheel built from it. For a reproducible install, pin a reviewed Git commit.

## Connect your chatbot

1. Implement the JSON HTTP contract in [the integration guide](../../docs/PORTABLE-GATEWAY.zh_CN.md). The example server `passport-demo-backend` shows request validation and authentication; replace its echo with your own chatbot adapter.
2. Copy [the HTTP configuration example](../../config/passport-http.example.env) to a private location and fill in the backend token, device-binding path, TLS certificate/key and speech-provider key.
3. Issue a device credential with `passport-device --device-id YOUR_DEVICE --bindings /path/to/bindings.json`. Assign that device to a user in your backend. Provision the displayed device token privately into the terminal.
4. Load your environment settings and run `passport-gateway` (equivalent to `python -m passport_gateway`). Devices connect to `wss://YOUR_HOST:8443/v1/voice` using subprotocol `dafeiyu.voice.v1`.

HTTP backends receive only `op`, authenticated `device_id`, `turn_uid` and `text`; device credentials and client-supplied identities are never forwarded. Your backend owns user/session mapping, authorization, memory and tool execution. `turn_uid` and `Idempotency-Key` support deduplication; the gateway never retries chat requests automatically. Remote endpoints require HTTPS; plaintext HTTP is restricted to loopback. Redirects and environment HTTP proxies are disabled.

The default speech provider is SiliconFlow SenseVoice/CosyVoice; DashScope is also selectable. Model/voice/style settings are configurable. Provider accounts and keys are supplied by the operator.

Optional proactive reminders require a compatible terminal **and** the reminder backend operations; HTTP reminder polling is off unless `PASSPORT_HTTP_REMINDERS=1`. The gateway does not itself provide a calendar, NLP scheduler, chatbot memory or a persona.

## Existing Dafeiyu installations

`PASSPORT_BACKEND=unix` is the default and preserves the existing unified-core socket. Continue to use server-owned identity bindings and the Host Planner/tools/Replyer pipeline. The backend relies on Linux peer credentials and socket ACLs. QQ is optional; see [identity management](../../docs/PASSPORT-IDENTITY.md). A device binding assigns a person, not a speaker voiceprint. The MaiBot Host patch is still required for this adapter; it is not included in the standalone wheel.

The published production snapshot is commit `3f2b20d`; the portable changes are separate and have not been deployed to that running instance.

## Verification and license

From a checkout, `python scripts/run_passport_checks.py` runs mock-provider/backend tests, including loopback transport tests. Six Linux-only backend checks are skipped on macOS. The standalone package can be verified with `passport-smoke` without the rest of the checkout. See [the release validation](../../docs/PORTABLE-GATEWAY-VALIDATION.json) for the actual checks performed.

The source in this directory is MIT licensed; see [LICENSE](LICENSE). This does not change the licenses of MaiBot, other repository components or third-party dependencies. See [upstream notices](../../UPSTREAM.md).

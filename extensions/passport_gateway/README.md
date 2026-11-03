# Passport voice gateway

Authenticated WSS audio/text entry into the existing Dafeiyu backend. The gateway performs speech recognition and synthesis; the unified core verifies the server-owned device/person binding and runs the real Host Planner/tools/Replyer pipeline. It does not maintain a second chatbot persona or memory store.

From the repository root, install `extensions/passport_gateway/requirements.txt`, add `extensions` to `PYTHONPATH`, and run `python -m passport_gateway`. Configuration uses environment variables documented in `config.py`. Production requires operator-provided `PASSPORT_TLS_CERT`, `PASSPORT_TLS_KEY`, `PASSPORT_BINDINGS`, and speech-provider credentials. No credential or binding is supplied by this snapshot. The backend socket defaults to the existing unified-core local socket.

`python -m passport_gateway.tokens --device-id YOUR_DEVICE --bindings /path/to/private-bindings.json` creates a device token; keep it private. The gateway binding contains the token hash and enabled state. The backend separately owns the person/profile and permitted session mapping. QQ is optional; independent friends use Passport-native sessions. See the [identity guide](../../docs/PASSPORT-IDENTITY.md). Device binding identifies the assigned person, not the speaker by voice. Phone Wi-Fi provisioning belongs to the separate device firmware.

The default speech adapter uses SenseVoice and streaming CosyVoice. The configured voice/style remains configurable; the alternative DashScope adapter is included but is not the deployed default.

Run `python scripts/run_passport_checks.py` for the packaged offline checks. They use mock speech providers and mock backend responses; some transport tests open only loopback sockets. Source and acceptance details: [current update](../../docs/UPDATE-20261103-identity-memory.zh.md) and [earlier snapshot](../../docs/SNAPSHOT-20261103.zh.md).

The backend uses Linux peer credentials and socket ACLs. Those six platform-specific checks are explicitly skipped on macOS; skipped checks are not counted as passing.

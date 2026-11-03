<p align="right"><a href="PASSPORT-IDENTITY.zh_CN.md">简体中文</a> · <strong>English</strong></p>

# Per-person Passport identity (profiles, devices, optional verified QQ link)

Status: deployed. The existing owner device has user-confirmed audible replies and its preferred spoken address. A second physical device and live friend/QQ linking remain unverified; see the end and the [current update](UPDATE-20261103-identity-memory.zh.md).

## Model

* A **profile** is a small server-side person record with a stable random id (`pf_<16 hex>`), a display name, an optional
  *spoken address* preference, and a role. The only role a profile can have is `friend`. **The owner is not a profile**:
  the existing owner device stays on the legacy `voice_bindings.json` (`owner_private_id` + `session`) and keeps its
  identity, memories, privileges and the full Maisaka planner/tool/replyer pipeline unchanged.
* A **device** (unique id + gateway token) is *assigned* to one profile by the administrator. Several devices may belong to
  one profile. The device never sends a profile, QQ number, session or role: the wire protocol is unchanged
  (`{device_id, turn_uid, text}`); any extra field is rejected (`unexpected_fields`). Network, Wi-Fi, IP, MAC and display
  names play no part. A device id alone is not proof (the gateway token is checked first).
* **QQ is optional.** A person without QQ talks through a real Host private session of platform `passport`
  (opened with the Host's `chat.open_session`, scope `passport`, user = the profile id). No QQ account is forged.
* Display name and spoken address are **presentation only**. Choosing the word 主人 never changes role, tools, session
  or memory (tested).

## Files

| File | Written by | Purpose |
| --- | --- | --- |
| `<root>/voice_bindings.json` | Codex/operator (legacy, untouched by this work) | device -> owner private session. May carry optional `"preferred_address": "主人"` |
| `<root>/passport_identity.json` | `passport_admin` only (0600, atomic, `.bak`) | profiles, device assignments, QQ association |
| gateway `bindings.json` | `passport_gateway.tokens` | device **credential** (token hash + enabled). Holds no person/QQ/role |

A device that appears in both identity files is ambiguous and fails closed. A malformed `passport_identity.json` makes every
profile route fail closed; the owner device is unaffected (different file).

## Admin workflow

All mutating commands are a dry run unless `--apply` (a top-level flag, before the command).

```
# 0. credential (separate from identity; prints the token once, never replaces an existing one without --rotate)
python -m passport_gateway.tokens --bindings B --device-id dev-xxxx
# 1. profile (QQ number optional, recorded as PENDING only; address optional)
python -m unified_core.passport_admin --root R --apply create-profile --name 小红 [--address 称呼] [--qq NUMBER]
# 2. bind the device to the person
python -m unified_core.passport_admin --root R --apply assign-device --profile pf_... --device dev-xxxx [--reassign]
# inspect (numbers masked, credential state only, no hashes)           validate (re-checks QQ links)
python -m unified_core.passport_admin --root R list --bindings B       python -m unified_core.passport_admin --root R validate
# other: unassign-device / disable-device / enable-device / disable-profile / enable-profile / rename-profile / set-address
```

No restart is needed: the identity file is re-read on every turn.

## Optional QQ association

1. Recording a number (`--qq`, `set-qq-pending`) only stores an admin note. It grants no QQ history, owner power, send
   authority or session access, and the person keeps using the Passport-native session.
2. Activation needs all of: the person completed the existing familiar-private challenge (DM `验证熟人`, then
   `私聊核验 <code>` in an authorised group) so a verified `qq_private_bindings` row with a registered session exists; that
   account is not the owner; one private binding belongs to at most one profile; **trusted evidence that the account is the
   pending number**; and an explicit administrator command:
   `verify-qq --profile pf_... --private-id <id> --confirm-number NUMBER` (the administrator re-types the number).
3. Evidence (nothing else counts): the verified account's platform id (private id, or its group-side actor id) equals the
   number, or the operator's `policy.json` maps the number to that private id (`"passport_qq_number_evidence": {"<number>": "<private_id>"}`).
   With opaque ids and no mapping the number stays pending: someone else's verified id can never be attached to a number.
4. After activation, that profile's Passport turns use her real QQ private session (shared history with her QQ chat, the same
   as her QQ chat already has) with **friend** authority; the owner is never reachable this way. The link is re-validated on
   every turn; if it stops holding the turn fails closed (no silent fallback). `unlink-qq` returns to the native session.

## Authority (audited paths, not a prompt)

* Route and role come only from the server files. The plugin records the resolved route and device in the active turn scope.
  Every hook/tool entry re-resolves the device (`scope_for`), so a revoked/disabled/reassigned device, changed QQ link or
  revoked owner binding stops dispatching immediately.
* Plugin tools are owner-only by default (`voice_scoped`); only the context hooks and `public_web_search`, `public_web_read`,
  `unified_memory`, `unified_activity`, `unified_persona` are friend-safe. Bilibili actions/settings, image import/send, gallery
  are owner-only and not even declared for platform `passport`.
* The Host enforces a tool **allowlist** for friend routes at its registry (listing, sub-agent grants and invocation): builtin
  names must come from the builtin provider and plugin names from `local.unified-core`; MCP/agent tools can never match. This
  blocks the Host's unscoped `query_person_profile` / `query_memory`, sub-agents, emoji/image/forward tools. Without an
  allowlist a restricted route is refused by the capability.
* Context, memory search, activity and recall for a friend use only that person's own session (`state.allowed`, platform
  `passport`); shared-group recall and owner rules are not used. Friends cannot read the owner's history/config, manage the
  bot or approve public posting. Voice delivery stays captured (never sent through QQ).
* Persistence/provenance: events keep the real platform (`qq` or `passport`), actor, `profile_id`, route kind and `channel=device_voice`.

## Replay, reassignment and in-flight changes

* Idempotency, execution reservations, memory jobs and event ids are namespaced per profile (the owner keeps its historical keys).
  A device moved A -> B never replays A's result, history, pending turn or memory; the same turn id after the move runs fresh for B;
  moving it back replays only A's own record.
* The route is re-checked after every wait (idempotency lock, session lock, friend slot), after the Host answers and after the memory
  sync await. If the authority changed, the answer is not published (`voice_binding_changed`, shown to the device as `backend_unbound`),
  the durable reservation refuses a re-run to avoid repeating tool side effects. If reassignment occurs during memory synchronization, records already written remain attributed to the original person; the final reply is not delivered to the reassigned identity.
  A friend's completed turn still syncs its memory to its own profile chat while that profile is enabled.
* Concurrency: one turn per person session at a time (`voice_busy` after 3 s); friends share a pool of 2 (Host: 2 of 3 slots), so the owner keeps a slot.

## Spoken address

Optional `preferred_address` (1-16 letters/digits/CJK/space/-/_) on a profile (`--address`, `set-address`) or on the legacy owner
entry. It is used only in Passport voice: it appears in the voice prompt as an explicit preference and as the nickname of the
Host voice message. The real QQ nickname/account and QQ chats are unchanged; absent => the previous behaviour.

## Remaining live acceptance

A second physical device, independent friend Passport sessions against the live Host database, friend voice behaviour and the real QQ verification flow remain unverified. The existing owner device and preferred address were user-confirmed; required deployed plugin capabilities were checked.

Linking QQ changes the route of future conversations. Existing Passport-native history is retained and is not automatically migrated into QQ.

The public directory `extensions/maibot_unified_core` is installed as package `unified_core`; run the documented module commands in that installed environment. No personal numbers, device tokens or production bindings are included.

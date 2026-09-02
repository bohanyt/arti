# Minecraft — ARTI as a real player

ARTI's Minecraft integration uses a Python runner plus a Node.js Mineflayer process in [`mc-bot/`](../mc-bot/). The bot exchanges newline-delimited JSON with the Python runtime over stdio; human-readable bot logs go to stderr.

This public integration is shipped **off by default**. Use it only on a world/server where you are allowed to run an automated player.

## Requirements

- Python 3.11 for the ARTI bridge
- Node.js 22 recommended (the public CI version)
- a Minecraft Java server/world reachable by the machine running ARTI
- a bot username that is permitted by that server

The current Mineflayer dependency set is pinned in [`mc-bot/package-lock.json`](../mc-bot/package-lock.json). Do not rely on a version number copied from an old document; `npm ci` installs the exact public lockfile.

## Install

From the repository root:

```bash
cd mc-bot
npm ci
```

Return to the repository root before starting the Python bridge.

## Local configuration

Copy [`config_local.json.example`](../config_local.json.example) to `config_local.json`, then set the values for your environment. A minimal Minecraft section looks like:

```json
{
  "minecraft_enabled": true,
  "minecraft_host": "127.0.0.1",
  "minecraft_port": 25565,
  "minecraft_bot_name": "Arti",
  "minecraft_streamer_name": "YourMinecraftName",
  "minecraft_node_path": "node",
  "minecraft_bot_script": "mc-bot/bot.js"
}
```

`config_local.json` is local-only and must not be committed.

The Node process uses Mineflayer's offline-account mode. That is appropriate only for a server configuration where that bot identity is intentionally accepted. Do not use this setup to bypass authentication or server rules.

## Start through the bridge

Run:

```bash
python hermes_vtuber_bridge.py
```

With Minecraft enabled in local config, the bridge can start/stop the Mineflayer runner and inject Minecraft state into ARTI's conversation context. Console/chat commands and model-emitted Minecraft tags are validated by the Python/Node integration before execution.

## What the shipped bot can do

The current public bot contains more than simple follow behavior. Depending on runtime state, commands, inventory, and local configuration, the code has handlers for behavior such as:

- follow / come / stop / roam;
- chat and status reporting;
- movement/pathfinding and basic recovery;
- collecting/mining and item pickup;
- placing/building-related actions;
- crafting/tool preparation and inventory interactions;
- eating and survival/defensive reactions;
- selected camera/POV behavior.

These capabilities are not a claim that every Minecraft version/server/plugin combination works. Public CI exercises protocol/orchestration logic and JavaScript syntax; a real world/server remains a local verification step.

### Guest/passive mode

`mc-bot/bot.js` also has a guest/passive mode intended for worlds owned by someone else. In that mode, world-changing command classes are blocked at the dispatcher so the bot can remain comparatively passive while following/roaming/chatting.

Treat this as an additional safety guard, not permission to automate on a server that forbids bots.

## Public-safe checks

The repository intentionally does **not** publish private local spike/harness scripts. The checks that actually ship are:

```bash
node --check mc-bot/bot.js
node --check mc-bot/pov.js
python -m pytest -q tests/test_minecraft_events.py tests/test_minecraft_runner.py
```

GitHub Actions also runs `npm ci` for `mc-bot/` on every public PR. These checks are deterministic/cloud-safe and do not claim a live Minecraft session.

## Optional POV

[`mc-bot/pov.js`](../mc-bot/pov.js) supports the public POV/camera side of the Minecraft integration. OBS/browser-source and local camera behavior depend on your own Minecraft/OBS setup and must be tested locally.

## Troubleshooting

- **Node process exits immediately:** run `npm ci` again and check stderr from the bridge.
- **Bot cannot connect:** verify host, port, server authentication mode, allowlist, and bot username.
- **Follow/come cannot find the streamer:** verify `minecraft_streamer_name` exactly matches the in-world player name and that the player entity is visible to the bot.
- **Pathfinding stalls:** stop/reissue the task and inspect bot stderr; world geometry and server plugins can make goals unreachable.
- **POV/camera does not appear:** verify the optional camera configuration independently from the core Mineflayer connection.

Never publish real server addresses, private player/session captures, authentication material, or local telemetry when reporting an issue.

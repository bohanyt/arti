# VTube Studio animation and expression wiring

This guide covers the public VTube Studio wiring for ARTI. It intentionally avoids model-specific hotkey IDs, local model paths, private assets, and captured live-session evidence.

For the broader runtime setup, start with [`WIRING.md`](WIRING.md). For architecture and behavior details, see [`Expression-Motion-System.md`](Expression-Motion-System.md).

## 1. Enable the VTube Studio API

1. Open VTube Studio.
2. Enable the plugin/API connection in VTube Studio settings.
3. Start ARTI with:

   ```bash
   python hermes_vtuber_bridge.py
   ```

4. Approve the ARTI plugin request when VTube Studio prompts you.

The authentication token is local runtime state and must not be committed.

## 2. Configure the API port

The default public configuration uses port `8002`. Override it through local config if your VTube Studio instance uses a different port:

```json
{
  "vts_api_port": 8002
}
```

Keep the real `config_local.json` outside Git.

## 3. Configure your model directory

Some expression/motion features need access to your local VTube Studio model files. Point ARTI at your own model directory through:

```json
{
  "vts_model_dir": "PATH_TO_YOUR_VTS_MODEL_DIRECTORY"
}
```

Do not publish the maintainer's path or assume another user's Steam/VTube Studio install lives in the same directory.

## 4. Logical expression states

The bridge uses logical conversation states such as:

- `aware` — listening/attention;
- `mikir` — processing/thinking;
- `bicara` — speaking;
- `default` — neutral/default.

Your Live2D model must provide suitable expression assets/hotkeys for whichever states you enable. File names and parameters are model-specific.

The public source contains ARTI-oriented defaults, but a reusable setup should map those logical states to assets that actually exist in your model.

## 5. Emotion overlays

[`arti_expression_runtime.py`](../arti_expression_runtime.py) can parse hidden emotion tags and apply optional model expression overlays while ARTI speaks. The supported logical emotion keys are defined in code.

Enable emotion behavior only after verifying the mapped expressions on your own model. A missing optional mood should be diagnosable from logs rather than treated as successful local verification.

See:

- [`SPEC-arti-emotion.md`](SPEC-arti-emotion.md)
- [`SMOKE-TEST-emotion.md`](SMOKE-TEST-emotion.md)

## 6. Nod and parameter-driven movement

[`arti_nod.py`](../arti_nod.py) and the bridge contain optional movement behavior that can drive supported VTube Studio tracking/input parameters.

Parameter IDs and useful ranges vary by model. Test conservative values first and verify that the model returns to neutral after speech, interruption, reconnect, and shutdown.

Do not copy generated hotkey IDs or local tracking calibration from another user's machine.

## 7. Idle motion

The bridge can coordinate idle expression/pose targets and VTube Studio motion hotkeys when local assets are configured. Idle behavior should yield while ARTI is actively speaking and recover cleanly afterward.

If your model does not have the expected idle assets, keep that optional path disabled or map it to your own assets rather than creating placeholder files in the public repository.

## 8. Local smoke check

Before using VTube Studio integration on stream:

1. start VTube Studio and load your model;
2. start `hermes_vtuber_bridge.py`;
3. confirm plugin authorization succeeds;
4. trigger a short local turn;
5. verify listening/thinking/talking/default transitions;
6. if enabled, verify emotion overlay and nod behavior;
7. verify idle state resumes and no expression remains stuck;
8. restart/reconnect once to make sure cleanup still works.

Public cloud CI does not exercise VTube Studio, your model files, GPU/audio devices, or local application state. Those checks are local-only.

## Privacy

Never include a VTube Studio token, private model asset, local user path, device inventory, or captured stream transcript in a public issue. Use synthetic placeholders and describe only the minimum reproducible configuration.

"""Live7 save identity from the existing native scene stream, never SMAPI logs."""
from pathlib import Path
import re
from urllib.parse import unquote


def scene_save_identity(frame, session):
    """SceneIdentity.SessionId = obs:<save token>:<actor>:<epoch>.

    The mod uses Constants.SaveFolderName, or uniqueIDForThisGame before the
    first save. Bind both the envelope and token to the enrolled farmhand.
    """
    if not isinstance(frame, dict) or frame.get("protocol") != 1 or frame.get("ev") != "observation":
        raise ValueError("native_save_identity_unavailable")
    payload = frame.get("payload") or {}
    if (payload.get("product") != "scene" or payload.get("available") is not True
            or str(frame.get("player_id")) != str(session.get("player_id"))
            or frame.get("screen_id") != session.get("screen_id", 0)):
        raise ValueError("native_save_identity_mismatch")
    parts = str(payload.get("session_id", "")).split(":")
    if (len(parts) != 4 or parts[0] != "obs" or parts[2] != str(session.get("player_id"))
            or not re.fullmatch(r"[0-9]+", parts[3])):
        raise ValueError("native_save_identity_mismatch")
    token = unquote(parts[1])
    save_id = token if re.fullmatch(r"[0-9]+", token) else token.rpartition("_")[2]
    # Truncated identity tokens contain '~'; never guess their missing suffix.
    if "~" in parts[1] or not re.fullmatch(r"[0-9]+", save_id):
        raise ValueError("native_save_id_unresolved")
    return dict(save_id=save_id, session_id=session["session_id"], scene_session_id=payload["session_id"])


def resolve_save(saves_root, save_id):
    """One actual top-level folder with the native world ID, or still pending."""
    if not isinstance(save_id, str) or not re.fullmatch(r"[0-9]+", save_id):
        raise ValueError("native_save_id_unresolved")
    try:
        matches = [p for p in Path(saves_root).iterdir() if p.is_dir() and p.name.endswith("_" + save_id)]
    except OSError:
        return None  # absent/unreadable Saves is handled by the bounded night wait
    if len(matches) > 1:
        raise ValueError("save_name_ambiguous:" + save_id)
    return dict(save_name=matches[0].name, save_folder=str(matches[0])) if matches else None

"""Read the public lossless render formats for behavioral assertions.

PM-1 paging (78a176f72) and fix10b (4a9f0910b)/fix18 (603775723) moved facts to read
pages and shared row tables. Tests compare decoded facts and reachable choices
instead of requiring the pre-paging JSON layout on the first page.
"""
from copy import deepcopy
import json

import arti_stardew_skills as S


def expand_rows(value):
    if isinstance(value, list):
        return [expand_rows(v) for v in value]
    if not isinstance(value, dict):
        return value
    if "rows" in value and (isinstance(value.get("cols"), list) or ("all_rows" in value and all(isinstance(r, dict) for r in value["rows"]))):
        rows = []
        for raw in value["rows"]:
            if "cols" in value:
                row = dict(zip(value["cols"], raw))
                if value.get("cols_extra") and len(raw) > len(value["cols"]):
                    row.update(raw[-1])
            else:
                row = dict(raw)
            shared = {} if row.pop("all_rows", None) is False else value.get("all_rows", {})
            row = dict(shared, **row)
            for key, other in value.get("same_as", {}).items():
                row[key] = row[other]
            if "i" in row and isinstance(row.get("path"), str):
                row["path"] = row["path"].replace("[i]", "[%s]" % row.pop("i"))
            rows.append(expand_rows(row))
        return rows
    return {k: expand_rows(v) for k, v in value.items()}


def render_pages(request, fits=None):
    """Follow only published read_boundary links, checking every rendered page."""
    pending, seen, pages = [None], set(), []
    while pending:
        selection = pending.pop(0)
        if selection in seen:
            continue
        seen.add(selection)
        assert len(seen) < 5000, "read navigation failed to converge"
        offer = deepcopy(request.skill_offer)
        if selection is not None:
            offer["boundary_page"] = selection
        messages = S.render_skill_messages(S.replace_offer(request, offer), fits)
        assert fits is None or fits(messages)
        page = expand_rows(json.loads(messages[1]["content"]))
        pages.append(page)
        for entry in page.get("skills", []):
            if entry["name"] != "read_boundary":
                continue
            for arg in entry.get("args", []):
                handle, index = arg.rsplit(":", 1)
                target = (handle, int(index))
                if target not in seen:
                    pending.append(target)
    return pages


def read_detail_pages(read, arg, until=None):
    """Follow a detail read's published links only, breadth first. ``read((handle, index))`` returns one decoded page."""
    pending, seen, pages = [arg], set(), []
    while pending and not (until and until(pages)):
        link = pending.pop(0)
        if link in seen:
            continue
        seen.add(link)
        assert len(seen) < 5000, "detail read failed to converge"
        handle, index = link.rsplit(":", 1)
        page = read((handle, int(index)))
        pages.append(page)
        pending += [a for e in page.get("skills", []) if e["name"] == "read_boundary" for a in e.get("args", [])]
    return pages


def detail_tree(pages):
    """ADR 121: the payload rebuilt from what the opened section pages printed, never from the input."""
    tree = {}
    rows = [(d["path"], d["value"]) for d in (p.get("detail_page") for p in pages) if isinstance(d, dict) and "value" in d]
    for path, value in sorted(rows, key=lambda row: len(row[0])):
        if not path:
            tree.update(value)
            continue
        cursor = tree
        for key in path[:-1]:
            cursor = cursor.setdefault(key, {})
        cursor[path[-1]] = value

    def whole(value):
        if isinstance(value, list):
            return [whole(v) for v in value]
        if not isinstance(value, dict):
            return value
        if set(value) == {"text"} and isinstance(value["text"], dict):
            return "".join(value["text"][n] for n in sorted(value["text"]))
        if value and all(type(k) is int for k in value):
            return [whole(value[n]) for n in sorted(value)]
        return {k: whole(v) for k, v in value.items()}
    return whole(tree)


def rendered_offer(request, fits=None):
    """Reassemble facts and choices from reachable pages, never from the input."""
    pages = render_pages(request, fits)
    result = deepcopy(pages[0])
    choices, fragments = {}, {}
    for page in pages:
        for entry in page.get("skills", []):
            if entry["name"] == "read_boundary":
                continue
            dest = choices.setdefault(entry["name"], dict(entry, args=[]))
            dest["args"] = list(dict.fromkeys(dest["args"] + entry.get("args", [])))
        for path, value in page.get("presentation", {}).get("details", []):
            fragments.setdefault(tuple(path), []).append(value)
        # Detail-read pages may carry whole fact blocks directly.
        if "presentation" not in page and "detail_page" not in page:
            for key, value in page.items():
                if key not in ("skills", "echo"):
                    result[key] = value
    tree = detail_tree(pages)
    for entry in tree.get("skills", ()):
        if entry.get("name") in choices:
            for key, value in entry.items():
                if key != "args":
                    choices[entry["name"]].setdefault(key, value)
    result.update({k: v for k, v in tree.items() if k not in ("skills", "echo")})
    result.pop("detail_page", None)
    result["skills"] = list(choices.values())
    restore_details(result, fragments, choices)
    texts = {k: v for page in pages for k, v in page.get("shared_text", {}).items()}
    def unshare(value):
        if isinstance(value, str):
            return texts.get(value[1:], value) if value.startswith("@") else value
        if isinstance(value, list): return [unshare(v) for v in value]
        if isinstance(value, dict): return {k: unshare(v) for k, v in value.items()}
        return value
    return unshare(result)


def restore_details(result, fragments, choices=None):
    for path, values in sorted(fragments.items(), key=lambda item: len(item[0])):
        if not path:
            result.update(expand_rows(values[-1]))
            continue
        if all(isinstance(v, dict) and set(v) == {"offset", "text", "chars"} for v in values):
            chunks = {v["offset"]: v["text"] for v in values}
            value = "".join(chunks[n] for n in sorted(chunks))
            assert len(value) == values[0]["chars"]
        else:
            value = values[-1]
        cursor = result
        parts = list(path)
        if parts[0] == "skills":
            cursor = choices[parts[1]]
            parts = parts[2:]
            if not parts:
                cursor.update(expand_rows(value))
        for i, part in enumerate(parts):
            if isinstance(cursor, list):
                while len(cursor) <= part:
                    cursor.append(None)
            if i == len(parts) - 1:
                cursor[part] = expand_rows(value)
            else:
                next_type = list if isinstance(parts[i + 1], int) else dict
                if (isinstance(cursor, dict) and part not in cursor) or not isinstance(cursor[part], next_type):
                    cursor[part] = next_type()
                cursor = cursor[part]


def ui_detail_facts(offer):
    from arti_stardew_generic_ui import compact_ui_offer
    copy = deepcopy(offer)
    count = compact_ui_offer(copy)["unknown_ui"]["inspect"]["pages"]
    result, fragments = {}, {}
    for n in range(count):
        copy["unknown_ui"]["inspect_page"] = n
        page = compact_ui_offer(copy)["unknown_ui"]
        result.update({k: v for k, v in page.items() if k not in ("details", "inspect")})
        for path, value in page.get("details", []):
            fragments.setdefault(tuple(path), []).append(value)
    restore_details(result, fragments)
    return result

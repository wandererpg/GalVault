#!/usr/bin/env python3
"""Consolidate releases by work and enrich genuinely empty work notes.

Preview is the default. All deleted/modified notes are backed up before --apply.
VNDB transport failures never authorize deletion.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from dataclasses import dataclass
import json
from pathlib import Path
import re
import sys
import time
from datetime import datetime, timezone
import urllib.error
import urllib.request
import zipfile
import subprocess
import os

import yaml

import deduplicate_exact_releases as exact

ROOT = Path(__file__).resolve().parents[2]
TECHNICAL = {"entity_id", "type", "display_title", "title_ja", "title_en",
             "title_zh", "title_status", "source_ids", "source_urls", "aliases",
             "merged_source_records", "consolidated_releases"}


def markdown_paths(root: Path):
    for directory, folders, files in os.walk(root):
        folders[:] = [f for f in folders if f not in {".git", ".obsidian", ".baoyu-skills", "assets", "__pycache__", "vendor", "vendor_local"}]
        for name in files:
            if name.endswith(".md"):
                yield Path(directory) / name


@dataclass
class Note:
    path: Path
    text: str
    data: dict
    body: str


def parse_note(path: Path, text: str) -> Note:
    match = exact.FRONTMATTER_RE.match(text)
    data = yaml.load(match.group(1), Loader=getattr(yaml, "CSafeLoader", yaml.SafeLoader)) if match else {}
    if data is not None and not isinstance(data, dict):
        raise ValueError(f"Invalid properties: {path}")
    return Note(path, text, data or {}, text[match.end():] if match else text)


def filled(value) -> bool:
    if isinstance(value, (list, dict)):
        return any(filled(item) for item in (value.values() if isinstance(value, dict) else value))
    return value is not None and value != "" and value != "unknown" and value != "unresolved"


def content_links(note: Note) -> set[str]:
    body = re.sub(r"<!-- AUTO:BEGIN (?:provenance|image) -->.*?<!-- AUTO:END (?:provenance|image) -->",
                  "", note.body, flags=re.S)
    return {exact.link_target(link) for link in exact.LINK_RE.findall(body)}


def substantive_text(note: Note) -> str:
    body = exact.AUTO_BLOCK_RE.sub("", note.body)
    body = re.sub(r"<!--.*?-->", "", body, flags=re.S)
    body = exact.LINK_RE.sub("", body)
    lines = []
    for line in body.splitlines():
        if re.match(r"^\s*(?:#{1,6}\s|!\[)", line):
            continue
        line = re.sub(r"^\s*[-*]\s+", "", line)
        line = re.sub(r"https?://\S+|vndb:[vr]\d+", "", line).strip()
        if line:
            lines.append(line)
    return "\n".join(lines)


def empty_work(note: Note) -> bool:
    # Rich links/structured game facts count as content, even without a synopsis.
    facts = {k: v for k, v in note.data.items() if k not in TECHNICAL
             and k not in {"presentation_type", "work_type", "release_count", "cover_image"}}
    body = re.sub(r"<!-- AUTO:BEGIN relation -->.*?<!-- AUTO:END relation -->", "", note.body, flags=re.S)
    display = Note(note.path, note.text, note.data, body)
    return not content_links(display) and not substantive_text(display) and not any(filled(v) for v in facts.values())


def work_id(note: Note) -> str | None:
    source = note.data.get("source_ids", {}).get("vndb")
    if isinstance(source, str) and re.fullmatch(r"v\d+", source):
        return source
    match = re.search(r"(?:work:|vndb\.org/)(v\d+)\b", str(note.data.get("entity_id", "")))
    return match.group(1) if match else None


def release_work_id(note: Note) -> str | None:
    match = re.search(r"work:vndb:(?:work:)?(v\d+)\b", str(note.data.get("entity_id", "")))
    return match.group(1) if match else None


def release_score(note: Note, incoming: Counter) -> tuple:
    fields = sum(filled(v) for k, v in note.data.items() if k not in TECHNICAL)
    links = len(content_links(note))
    body_size = len(substantive_text(note))
    # Complete games beat trials; completeness and connections break ties.
    complete = note.data.get("edition_type") == "complete"
    return (int(complete), fields, links, body_size, incoming[note.path.stem], len(note.text))


def scan(root: Path) -> dict:
    notes = {}
    incoming = Counter()
    for folder in ("作品", "发行版", "会社", "创作者", "角色", "标签"):
        for path in sorted((root / folder).glob("*.md")):
            text = path.read_text(encoding="utf-8")
            if folder in {"作品", "发行版"}:
                notes[path] = parse_note(path, text)
            incoming.update(exact.link_target(link) for link in exact.LINK_RE.findall(text))
    groups = defaultdict(list)
    unmatched = []
    works = []
    for note in notes.values():
        if note.path.parent.name == "发行版":
            key = release_work_id(note)
            if key:
                groups[key].append(note)
            else:
                unmatched.append(str(note.path.relative_to(root)))
        elif note.path.parent.name == "作品":
            works.append(note)
    empty = [note for note in works if empty_work(note)]
    summary = {
        "works": len(works), "releases": sum(map(len, groups.values())) + len(unmatched),
        "release_groups": len(groups), "groups_to_merge": sum(len(g) > 1 for g in groups.values()),
        "releases_to_remove": sum(len(g) - 1 for g in groups.values()),
        "unmatched_releases": unmatched,
        "empty_works": [{"path": str(n.path.relative_to(root)), "id": work_id(n)} for n in empty],
    }
    return {"notes": notes, "groups": groups, "works": works, "empty": empty,
            "incoming": incoming, "summary": summary}


def api_request(filters: list, fields: str) -> list[dict]:
    payload = json.dumps({"filters": filters, "fields": fields, "results": 100}).encode("utf-8")
    for attempt in range(5):
        request = urllib.request.Request("https://api.vndb.org/kana/vn", data=payload,
                                         headers={"Content-Type": "application/json",
                                                  "User-Agent": "GalVault-content-maintenance/1.0"})
        try:
            with urllib.request.urlopen(request, timeout=45) as reply:
                data = json.load(reply)
            if data.get("more"):
                raise RuntimeError("Unexpected truncated VNDB response")
            return data.get("results", [])
        except urllib.error.HTTPError as error:
            if error.code != 429 and error.code < 500:
                raise RuntimeError(f"VNDB HTTP {error.code}: {error.read().decode('utf-8', errors='replace')}") from error
            delay = max(5, int(error.headers.get("Retry-After", 10)))
        except (urllib.error.URLError, TimeoutError) as error:
            delay = min(30, 2 ** (attempt + 1))
        if attempt == 4:
            raise RuntimeError("VNDB connection failed after retries")
        time.sleep(delay)
    raise RuntimeError("VNDB connection failed")


VN_FIELDS = ("id,title,titles{lang,title,latin,official,main},alttitle,description,released,"
             "rating,votecount,length,length_minutes,languages,platforms,devstatus,"
             "developers{id,name,original},staff{id,name,original,role,note},"
             "tags{id,name,rating,spoiler},relations{id,title,relation},image{id,url},"
             "va{staff{id,name,original},character{id,name,original},note}")


def fetch_empty(empty: list[Note], cache: Path) -> dict:
    cached = json.loads(cache.read_text(encoding="utf-8")) if cache.exists() else {}
    outcomes = {}
    by_id = defaultdict(list)
    for note in empty:
        key = work_id(note)
        if key:
            by_id[key].append(note)
        else:
            outcomes[str(note.path)] = {"status": "needs_search"}
    missing = [key for key in by_id if key not in cached]
    cache.parent.mkdir(parents=True, exist_ok=True)
    for offset in range(0, len(missing), 60):
        batch = missing[offset:offset + 60]
        filters = ["or", *[["id", "=", key] for key in batch]] if len(batch) > 1 else ["id", "=", batch[0]]
        try:
            rows = {row["id"]: row for row in api_request(filters, VN_FIELDS)}
            for key in batch:
                cached[key] = {"status": "found", "record": rows[key]} if key in rows else {"status": "not_found"}
            cache.write_text(json.dumps(cached, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"VNDB {min(offset + len(batch), len(missing))}/{len(missing)} checked", flush=True)
        except RuntimeError as error:
            for key in batch:
                cached[key] = {"status": "error", "error": str(error)}
            # Failed requests are not written to the persistent cache.
            print(f"VNDB batch failed: {error}", flush=True)
        time.sleep(1)
    for key, notes in by_id.items():
        for note in notes:
            outcomes[str(note.path)] = cached[key]
    missing_voice = [key for key in by_id if cached[key].get("status") == "found"
                     and "va" not in cached[key]["record"]]
    for offset in range(0, len(missing_voice), 60):
        batch = missing_voice[offset:offset + 60]
        filters = ["or", *[["id", "=", key] for key in batch]] if len(batch) > 1 else ["id", "=", batch[0]]
        try:
            rows = api_request(filters, "id,va{staff{id,name,original},character{id,name,original},note}")
            for row in rows:
                cached[row["id"]]["record"]["va"] = row.get("va", [])
            cache.write_text(json.dumps(cached, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"VNDB voice links {min(offset + len(batch), len(missing_voice))}/{len(missing_voice)} checked", flush=True)
        except RuntimeError as error:
            print(f"Additional voice lookup deferred: {error}", flush=True)
        time.sleep(1)
    for note in empty:
        if work_id(note):
            continue
        title = note.data.get("display_title", note.path.stem)
        try:
            rows = api_request(["search", "=", title], VN_FIELDS)
            names = {title.casefold(), note.path.stem.casefold()}
            matches = [row for row in rows if names.intersection(
                str(value).casefold() for value in [row["title"], *row.get("alttitle", "").split("\n"),
                                                  *[t.get("title") for t in row.get("titles", [])],
                                                  *[t.get("latin") for t in row.get("titles", [])]] if value)]
            if len(matches) == 1:
                outcomes[str(note.path)] = {"status": "found", "record": matches[0]}
            elif not rows:
                outcomes[str(note.path)] = {"status": "not_found"}
            else:
                outcomes[str(note.path)] = {"status": "ambiguous", "candidates": [r["id"] for r in rows]}
        except RuntimeError as error:
            outcomes[str(note.path)] = {"status": "error", "error": str(error)}
        time.sleep(1)
    return outcomes


def render_note(note: Note, data: dict, body: str | None = None) -> str:
    front = yaml.dump(data, Dumper=getattr(yaml, "CSafeDumper", yaml.SafeDumper),
                      allow_unicode=True, sort_keys=False, width=120)
    return "---\n" + front + "---\n" + (note.body if body is None else body)


def bbcode(text: str) -> str:
    text = re.sub(r"\[url=([^\]]+)\](.*?)\[/url\]", r"[\2](\1)", text, flags=re.S)
    text = re.sub(r"\[url\](.*?)\[/url\]", r"<\1>", text, flags=re.S)
    for tag, mark in (("b", "**"), ("i", "*"), ("u", ""), ("s", "~~")):
        text = re.sub(rf"\[{tag}\](.*?)\[/{tag}\]", lambda m: mark + m[1] + mark, text, flags=re.S)
    text = re.sub(r"\[/?(?:spoiler|quote|raw)\]", "", text)
    return "\n".join(line.rstrip() for line in text.strip().splitlines())


def record_has_content(row: dict) -> bool:
    return bool(row.get("description") or row.get("released") or row.get("rating")
                or row.get("developers") or row.get("staff") or row.get("tags") or row.get("va"))


def entity_index(root: Path, folder: str, prefix: str) -> dict[str, str]:
    result = {}
    for path in (root / folder).glob("*.md"):
        front = path.read_text(encoding="utf-8").split("\n---", 1)[0]
        match = re.search(rf'(?m)^\s+vndb:\s*["\']?({prefix}\d+)', front)
        if match:
            result[match[1]] = f"{folder}/{path.stem}"
    return result


def enrich(note: Note, row: dict, indices: dict) -> str:
    data = dict(note.data)
    data.setdefault("source_ids", {})["vndb"] = row["id"]
    data.setdefault("source_urls", {})["vndb"] = "https://vndb.org/" + row["id"]
    data["vndb_title"] = row["title"]
    data["vndb_content_updated"] = datetime.now(timezone.utc).date().isoformat()
    for title in row.get("titles", []):
        lang = title["lang"]
        if lang in {"ja", "en", "zh-Hans", "zh-Hant"} and title.get("official"):
            data.setdefault("title_" + lang.replace("-", "_"), title["title"])
    if row.get("released"):
        data["first_release_date"] = row["released"]
    if row.get("rating") is not None:
        data.setdefault("ratings", {})["vndb"] = round(row["rating"] / 10, 2)
    if row.get("votecount") is not None:
        data["vndb_vote_count"] = row["votecount"]
    for key in ("length", "length_minutes", "languages", "platforms", "devstatus"):
        if row.get(key) is not None:
            data["vndb_" + key] = row[key]
    body = note.body
    def named(item, kind):
        target = indices[kind].get(item["id"])
        label = item.get("original") or item.get("name") or item.get("title")
        return f"[[{target}|{label}]]" if target else f"[{label}](https://vndb.org/{item['id']})"
    blocks = {
        "duty": ["- " + named(item, "developers") + "（developer）" for item in row.get("developers", [])],
        "role": ["- " + named(item, "staff") + "（" + item["role"] + "）" for item in row.get("staff", [])],
        "tag": ["- " + named(item, "tags") for item in row.get("tags", []) if item["rating"] > 0 and item["spoiler"] == 0],
        "relation": ["- " + named(item, "works") + "（" + item["relation"] + "）" for item in row.get("relations", [])],
        "character": list(dict.fromkeys("- " + named(item["character"], "characters") for item in row.get("va", []))),
        "voice": list(dict.fromkeys("- " + named(item["staff"], "staff") + " → " + named(item["character"], "characters")
                                    for item in row.get("va", []))),
    }
    for kind, lines in blocks.items():
        if not lines:
            continue
        pattern = rf"(<!-- AUTO:BEGIN {kind} -->)(.*?)(<!-- AUTO:END {kind} -->)"
        def replace(match):
            existing = match[2].strip()
            new_lines = list(dict.fromkeys(([existing] if existing else []) + lines))
            return match[1] + "\n" + "\n".join(new_lines) + "\n" + match[3]
        body, count = re.subn(pattern, replace, body, flags=re.S)
        if not count:
            body += f"\n\n## VNDB {kind}\n<!-- AUTO:BEGIN {kind} -->\n" + "\n".join(lines) + f"\n<!-- AUTO:END {kind} -->\n"
    if row.get("description"):
        body += "\n\n## 简介（VNDB 原文）\n\n" + bbcode(row["description"]) + "\n"
    body += "\n\n## VNDB 资料来源\n\n" + f"[VNDB：{row['id']}](https://vndb.org/{row['id']})，补全于 {data['vndb_content_updated']}。\n"
    return render_note(note, data, body)


def consolidated_note(keeper: Note, others: list[Note]) -> str:
    data = dict(keeper.data)
    aliases = list(data.get("aliases", []))
    records = list(data.get("consolidated_releases", []))
    existing_ids = {r["id"] for r in records}
    for note in [keeper, *others]:
        if note != keeper:
            aliases.append(note.path.stem)
            aliases.extend(note.data.get("aliases", []))
        source = note.data.get("source_ids", {}).get("vndb")
        if source and source not in existing_ids:
            record = {"id": source, "title": note.data.get("display_title", note.path.stem),
                      "url": f"https://vndb.org/{source}"}
            record.update({k: note.data[k] for k in ("release_date", "platform", "edition_type", "is_all_ages")
                           if k in note.data})
            records.append(record)
            existing_ids.add(source)
        for record in note.data.get("merged_source_records", []):
            if record["id"] not in existing_ids:
                records.append(record)
                existing_ids.add(record["id"])
    data["aliases"] = sorted(set(aliases), key=str.casefold)
    data["consolidated_releases"] = records
    data["retained_release_count"] = 1
    return render_note(keeper, data)


def rewrite_links(text: str, path: Path, redirects: dict[str, str], deleted: dict[str, str | None],
                  collisions: set[str]) -> str:
    def replace(match):
        contents = match[1]
        target, sep, label = contents.partition("|")
        address, anchor, heading = target.partition("#")
        normalized = address.replace("\\", "/").removesuffix(".md")
        stem = normalized.rsplit("/", 1)[-1]
        qualified = "/" in normalized
        is_release = normalized.startswith("发行版/") or (not qualified and stem not in collisions)
        # A qualified redirect always remains a release link.
        if is_release and stem in redirects:
            return "[[发行版/" + redirects[stem] + (anchor + heading if anchor else "") + (sep + label if sep else "") + "]]"
        is_work = normalized.startswith("作品/") or (not qualified and stem not in collisions)
        if is_work and stem in deleted:
            display = label if sep else stem
            url = deleted[stem]
            return f"[{display}]({url})" if url else display
        return match[0]
    # For ambiguous basenames, only the work note's release section is authoritative.
    def release_block(match):
        def block_link(link):
            target, sep, label = link[1].partition("|")
            address, anchor, heading = target.partition("#")
            stem = address.replace("\\", "/").rsplit("/", 1)[-1].removesuffix(".md")
            if stem in redirects:
                return "[[发行版/" + redirects[stem] + (anchor + heading if anchor else "") + (sep + label if sep else "") + "]]"
            return link[0]
        return match[1] + exact.LINK_RE.sub(block_link, match[2]) + match[3]
    if path.parent.name != "发行版":
        text = re.sub(r"(<!-- AUTO:BEGIN release -->)(.*?)(<!-- AUTO:END release -->)", release_block, text, flags=re.S)
    return exact.deduplicate_auto_block_lines(exact.LINK_RE.sub(replace, text))


def plan_changes(root: Path, result: dict, outcomes: dict, indices: dict) -> dict:
    updates = {}
    removed = set()
    redirects = {}
    release_plans = []
    for key, group in sorted(result["groups"].items()):
        if len(group) <= 1:
            continue
        ordered = sorted(group, key=lambda n: (release_score(n, result["incoming"]), n.path.stem), reverse=True)
        keeper, others = ordered[0], ordered[1:]
        updates[keeper.path] = consolidated_note(keeper, others)
        for note in others:
            redirects[note.path.stem] = keeper.path.stem
            removed.add(note.path)
        release_plans.append({"work_id": key, "keeper": str(keeper.path.relative_to(root)),
                              "score": release_score(keeper, result["incoming"]),
                              "removed": [str(n.path.relative_to(root)) for n in others]})
    work_actions = []
    deleted_works = {}
    for note in result["empty"]:
        outcome = outcomes.get(str(note.path), {"status": "not_checked"})
        row = outcome.get("record")
        status = outcome["status"]
        if status == "found" and record_has_content(row):
            updates[note.path] = enrich(note, row, indices)
            action = "enriched"
        elif status == "not_found" or status == "found":
            removed.add(note.path)
            deleted_works[note.path.stem] = None
            action = "deleted"
        else:
            action = "deferred"
        work_actions.append({"path": str(note.path.relative_to(root)), "vndb_id": row["id"] if row else work_id(note),
                             "action": action, "reason": status,
                             **({"error": outcome["error"]} if outcome.get("error") else {})})
    all_paths = list(markdown_paths(root))
    live_stems = {p.stem for p in all_paths if p not in removed}
    collisions = (set(redirects) | set(deleted_works)) & live_stems
    for path in all_paths:
        if path in removed:
            continue
        text = updates.get(path, path.read_text(encoding="utf-8"))
        new = rewrite_links(text, path, redirects, deleted_works, collisions)
        # Avoid aliases shadowing another surviving page.
        if path in updates and path.parent.name == "发行版":
            note = parse_note(path, new)
            note.data["aliases"] = [a for a in note.data.get("aliases", []) if a not in live_stems]
            new = render_note(note, note.data)
        original = path.read_text(encoding="utf-8")
        if new != original:
            updates[path] = new
    return {"updates": updates, "removed": removed, "redirects": redirects, "deleted_works": deleted_works,
            "collisions": collisions, "release_plans": release_plans, "work_actions": work_actions}


def validate_plan(root: Path, plan: dict) -> dict:
    groups = Counter()
    remaining_empty = []
    broken = []
    removed_paths = {str(p.relative_to(root).with_suffix("")).replace("\\", "/") for p in plan["removed"]}
    removed_stems = {p.stem for p in plan["removed"]} - plan["collisions"]
    for folder in ("发行版", "作品"):
        for path in (root / folder).glob("*.md"):
            if path in plan["removed"]:
                continue
            note = parse_note(path, plan["updates"].get(path, path.read_text(encoding="utf-8")))
            if folder == "发行版":
                groups[release_work_id(note)] += 1
            elif empty_work(note):
                remaining_empty.append(str(path.relative_to(root)))
    for path in markdown_paths(root):
        if path in plan["removed"]:
            continue
        text = plan["updates"].get(path, path.read_text(encoding="utf-8"))
        for link in exact.LINK_RE.findall(text):
            target = link.split("|", 1)[0].split("#", 1)[0].replace("\\", "/").removesuffix(".md")
            if target in removed_paths or ("/" not in target and target in removed_stems):
                broken.append({"path": str(path.relative_to(root)), "target": target})
    duplicate_groups = {key: count for key, count in groups.items() if count > 1}
    deferred = {a["path"] for a in plan["work_actions"] if a["action"] == "deferred"}
    unexpected_empty = sorted(set(remaining_empty) - deferred)
    if duplicate_groups or broken or unexpected_empty:
        raise RuntimeError(json.dumps({"duplicate_groups": duplicate_groups, "broken": broken[:20],
                                       "unexpected_empty": unexpected_empty}, ensure_ascii=False))
    return {"duplicate_release_groups": 0, "links_to_removed_notes": 0,
            "remaining_empty_works": remaining_empty, "remaining_releases": sum(groups.values())}


def apply_changes(root: Path, result: dict, plan: dict, report_dir: Path) -> Path:
    # Guard against other writers changing files while VNDB is being fetched.
    for note in result["notes"].values():
        if note.path.read_text(encoding="utf-8") != note.text:
            raise RuntimeError(f"Concurrent edit detected; rerun preview: {note.path}")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_dir = root.parent / "GalVault-maintenance-backups"
    backup_dir.mkdir(exist_ok=True)
    backup = backup_dir / f"consolidation-{stamp}.zip"
    paths = set(plan["updates"]) | plan["removed"]
    with zipfile.ZipFile(backup, "x", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(paths):
            archive.write(path, str(path.relative_to(root)))
        archive.writestr("maintenance-manifest.json", json.dumps({
            "removed": [str(p.relative_to(root)) for p in sorted(plan["removed"])],
            "updated": [str(p.relative_to(root)) for p in sorted(plan["updates"])],
        }, ensure_ascii=False, indent=2))
    with zipfile.ZipFile(backup) as archive:
        if archive.testzip() is not None:
            raise RuntimeError("Backup failed verification")
    report_dir.mkdir(parents=True, exist_ok=True)
    # Used by the commit step to preserve pre-existing worktree changes.
    dirty = subprocess.check_output(["git", "diff", "--name-only", "-z"], cwd=root).decode("utf-8").split("\0")
    manifest = {"backup": str(backup), "initial_dirty": [p for p in dirty if p],
                "updated": [str(p.relative_to(root)).replace("\\", "/") for p in sorted(plan["updates"])],
                "removed": [str(p.relative_to(root)).replace("\\", "/") for p in sorted(plan["removed"])]}
    (report_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    for path, content in plan["updates"].items():
        path.write_text(content, encoding="utf-8")
    for path in plan["removed"]:
        # Exact paths were resolved and backed up; never delete directories.
        if path.parent not in {root / "发行版", root / "作品"}:
            raise ValueError(f"Refusing unexpected deletion: {path}")
        path.unlink()
    return backup


def stage_task_changes(root: Path, own_dirty=()):
    """Stage our edits while retaining the user's pre-existing unstaged delta."""
    if subprocess.check_output(["git", "diff", "--cached", "--name-only"], cwd=root).strip():
        raise RuntimeError("Existing staged changes must be kept separate; refusing to replace the index")
    report_dir = root / "系统" / "内容清理"
    manifest = json.loads((report_dir / "manifest.json").read_text(encoding="utf-8"))
    report = json.loads((report_dir / "report.json").read_text(encoding="utf-8"))
    dirty = set(manifest["initial_dirty"]) - set(own_dirty)
    paths = set(manifest["updated"]) | set(manifest["removed"])
    additions = {".gitignore", "README.md", "系统/内容清理/report.json", "系统/内容清理/清理报告.md",
                 "系统/工具/consolidate_vault.py", "系统/工具/tests/test_consolidate_vault.py"}
    paths.update(p for p in additions if (root / p).exists())
    clean = sorted(paths - dirty)
    if clean:
        subprocess.run(["git", "add", "--pathspec-from-file=-", "--pathspec-file-nul"], cwd=root,
                       input=("\0".join(clean) + "\0").encode("utf-8"), check=True)
    redirects = {Path(old).stem: Path(group["keeper"]).stem
                 for group in report["release_groups"] for old in group["removed"]}
    deleted = {Path(action["path"]).stem: None for action in report["work_actions"] if action["action"] == "deleted"}
    actions = {action["path"].replace("\\", "/"): action for action in report["work_actions"]}
    cached = json.loads((report_dir / "vndb-empty-work-records.json").read_text(encoding="utf-8"))
    indices = {
        "developers": entity_index(root, "会社", "p"), "staff": entity_index(root, "创作者", "s"),
        "tags": entity_index(root, "标签", "g"), "characters": entity_index(root, "角色", "c"),
        "works": {},
    }
    for path in (root / "作品").glob("*.md"):
        item = parse_note(path, path.read_text(encoding="utf-8"))
        if work_id(item):
            indices["works"][work_id(item)] = f"作品/{path.stem}"
    for relative in sorted(paths & dirty):
        path = root / relative
        if relative in manifest["removed"]:
            subprocess.run(["git", "update-index", "--remove", "--", relative], cwd=root, check=True)
            continue
        before = subprocess.check_output(["git", "show", f"HEAD:{relative}"], cwd=root).decode("utf-8")
        transformed = before
        action = actions.get(relative)
        if action and action["action"] == "enriched":
            transformed = enrich(parse_note(path, before), cached[action["vndb_id"]]["record"], indices)
        transformed = rewrite_links(transformed, path, redirects, deleted, set(report["colliding_basenames"]))
        if transformed == before:
            continue
        oid = subprocess.check_output(["git", "hash-object", "-w", "--stdin"], cwd=root,
                                      input=transformed.encode("utf-8")).decode().strip()
        subprocess.run(["git", "update-index", "--add", "--cacheinfo", f"100644,{oid},{relative}"], cwd=root, check=True)
    print("Staged task changes; pre-existing user edits remain unstaged.", flush=True)


def normalize_descriptions(root: Path):
    """Format only newly imported descriptions in both worktree and index."""
    report = json.loads((root / "系统/内容清理/report.json").read_text(encoding="utf-8"))
    pattern = r"(## 简介（VNDB 原文）\n\n)(.*?)(\n\n## VNDB 资料来源)"
    def normalize(text):
        return re.sub(pattern, lambda m: m[1] + "\n".join(line.rstrip() for line in m[2].split("\n")) + m[3], text, flags=re.S)
    count = 0
    for action in report["work_actions"]:
        if action["action"] != "enriched":
            continue
        relative = action["path"].replace("\\", "/")
        path = root / relative
        before = path.read_text(encoding="utf-8")
        after = normalize(before)
        if after != before:
            path.write_text(after, encoding="utf-8")
            count += 1
        staged = subprocess.check_output(["git", "show", f":{relative}"], cwd=root).decode("utf-8")
        staged_after = normalize(staged)
        if staged_after != staged:
            oid = subprocess.check_output(["git", "hash-object", "-w", "--stdin"], cwd=root,
                                          input=staged_after.encode("utf-8")).decode().strip()
            subprocess.run(["git", "update-index", "--add", "--cacheinfo", f"100644,{oid},{relative}"], cwd=root, check=True)
    print(f"Normalized imported descriptions in {count} notes; existing user edits retained.", flush=True)


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--inspect", action="store_true")
    parser.add_argument("--fetch", action="store_true", help="fetch empty-note facts from VNDB")
    parser.add_argument("--apply", action="store_true", help="apply after backup and validation")
    parser.add_argument("--stage-task", action="store_true", help="stage completed task, preserving prior user changes")
    parser.add_argument("--own-dirty", action="append", default=[], help="explicit task-owned edits predating apply")
    parser.add_argument("--normalize-descriptions", action="store_true", help="format new descriptions, retaining the staged/worktree split")
    args = parser.parse_args()
    root = args.root.resolve()
    if args.normalize_descriptions:
        normalize_descriptions(root)
        return
    if args.stage_task:
        stage_task_changes(root, args.own_dirty)
        return
    result = scan(root)
    summary = dict(result["summary"])
    summary["empty_works"] = len(result["empty"])
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
    if args.inspect:
        ordered = sorted(result["works"], key=lambda n: (len(content_links(n)), len(substantive_text(n)), len(n.text)))
        for note in ordered[:5]:
            print(json.dumps({"path": str(note.path.relative_to(args.root)), "links": len(content_links(note)),
                              "prose": len(substantive_text(note)), "data": note.data, "body": note.body}, ensure_ascii=False))
    if not (args.fetch or args.apply):
        return
    report_dir = root / "系统" / "内容清理"
    outcomes = fetch_empty(result["empty"], report_dir / "vndb-empty-work-records.json")
    print("Building local entity indices...", flush=True)
    indices = {
        "developers": entity_index(root, "会社", "p"),
        "staff": entity_index(root, "创作者", "s"),
        "tags": entity_index(root, "标签", "g"),
        "characters": entity_index(root, "角色", "c"),
        "works": {work_id(n): f"作品/{n.path.stem}" for n in result["works"] if work_id(n)},
    }
    print("Planning release consolidation and note enrichment...", flush=True)
    plan = plan_changes(root, result, outcomes, indices)
    print("Validating projected links and note content...", flush=True)
    validation = validate_plan(root, plan)
    report = {"before": summary, "release_groups": plan["release_plans"], "work_actions": plan["work_actions"],
              "validation": validation, "updated_notes": len(plan["updates"]),
              "removed_notes": len(plan["removed"]), "colliding_basenames": sorted(plan["collisions"])}
    report_dir.mkdir(parents=True, exist_ok=True)
    (report_dir / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    actions = Counter(a["action"] for a in plan["work_actions"])
    print(json.dumps({"work_actions": dict(actions), "removed_notes": len(plan["removed"]),
                      "updated_notes": len(plan["updates"]), "validation": validation}, ensure_ascii=False), flush=True)
    if args.apply:
        backup = apply_changes(root, result, plan, report_dir)
        final = scan(root)
        validate_plan(root, {**plan, "updates": {}, "removed": set()})
        after = {"works": final["summary"]["works"], "releases": final["summary"]["releases"],
                 "empty_works": len(final["empty"])}
        report["after"] = after
        report["backup"] = str(backup)
        (report_dir / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"applied": after, "backup": str(backup)}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Add VNDB synopses to work notes. Standard library only; preview by default.

--fetch queries VNDB and caches results without changing notes.
--apply queries missing records, verifies a ZIP backup, then changes notes.
--refresh refreshes cached records and our managed blocks, never manual prose.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sys
import time
import urllib.error
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parents[2]
BEGIN = "<!-- AUTO:BEGIN synopsis -->"
END = "<!-- AUTO:END synopsis -->"
MANAGED = re.compile(re.escape(BEGIN) + r".*?" + re.escape(END), re.S)
HEADING = re.compile(r"(?m)^(#{2,6})[ \t]+([^\r\n]+)\r?$")
INTRO_TITLE = re.compile(
    r"^(?:简介|作品简介|游戏简介|故事简介|剧情简介|故事介绍|剧情介绍|故事梗概|剧情梗概|"
    r"あらすじ|synopsis|description|story|overview)(?:\s*[（(].*?[）)])?\s*$", re.I)
PLACEHOLDERS = {"暂无", "暂无简介", "暂无内容", "待补充", "待填写", "待添加", "未填写", "n/a", "none", "tbd", "-"}


def vndb_id(text: str) -> str | None:
    front = re.match(r"\A\ufeff?---\r?\n(.*?)\r?\n---(?:\r?\n|$)", text, re.S)
    if not front:
        return None
    properties = front[1]
    sources = re.search(r"(?m)^source_ids:\s*\r?\n((?:[ \t]+[^\n]*\n?)*)", properties)
    if sources:
        match = re.search(r'(?m)^\s+vndb:\s*["\']?(v\d+)\b', sources[1])
        if match:
            return match[1]
    match = re.search(r'(?m)^entity_id:.*?vndb:work:(v\d+)\b', properties)
    if match:
        return match[1]
    urls = re.search(r"(?m)^source_urls:\s*\r?\n((?:[ \t]+[^\n]*\n?)*)", properties)
    match = re.search(r"https://vndb\.org/(v\d+)\b", urls[1]) if urls else None
    return match[1] if match else None


def intro_section(text: str):
    headings = list(HEADING.finditer(text))
    for index, heading in enumerate(headings):
        if INTRO_TITLE.fullmatch(heading[2].strip()):
            stop = next((h.start() for h in headings[index + 1:] if len(h[1]) <= len(heading[1])), len(text))
            return heading.end(), stop, text[heading.end():stop]
    return None


def has_manual_intro(text: str) -> bool:
    remaining = MANAGED.sub("", text)
    while (section := intro_section(remaining)) is not None:
        prose = re.sub(r"<!--.*?-->", "", section[2], flags=re.S).strip()
        if prose and prose.casefold() not in PLACEHOLDERS:
            return True
        remaining = remaining[section[1]:]
    return False


def markdown_description(description: str) -> str:
    text = re.sub(r"\[url=([^\]]+)\](.*?)\[/url\]", r"[\2](\1)", description, flags=re.S)
    text = re.sub(r"\[url\](.*?)\[/url\]", r"<\1>", text, flags=re.S)
    for tag, mark in (("b", "**"), ("i", "*"), ("u", ""), ("s", "~~")):
        text = re.sub(rf"\[{tag}\](.*?)\[/{tag}\]", lambda m: mark + m[1] + mark, text, flags=re.S)
    text = re.sub(r"\[/?(?:spoiler|quote|raw)\]", "", text)
    # Description text must not terminate the importer-owned region.
    text = text.replace(BEGIN, "&lt;!-- AUTO:BEGIN synopsis --&gt;")
    text = text.replace(END, "&lt;!-- AUTO:END synopsis --&gt;")
    return "\n".join(line.rstrip() for line in text.replace("\r\n", "\n").strip().splitlines())


def add_synopsis(text: str, record: dict, refresh: bool = False) -> str:
    description = markdown_description(record.get("description") or "")
    if not description or has_manual_intro(text):
        return text
    if MANAGED.search(text) and not refresh:
        return text
    newline = "\r\n" if "\r\n" in text else "\n"
    block = (BEGIN + "\n" + description + "\n\n"
             + f"来源：[VNDB {record['id']}](https://vndb.org/{record['id']})；简介保留原文。\n" + END)
    block = block.replace("\n", newline)
    if MANAGED.search(text):
        if len(MANAGED.findall(text)) != 1:
            raise ValueError("Multiple managed synopsis blocks; refusing automatic replacement")
        return MANAGED.sub(lambda _: block, text, count=1)
    section = intro_section(text)
    if section:
        start, stop, _ = section
        return text[:start] + newline * 2 + block + newline * 2 + text[stop:]
    # Insert after the cover, before the relationship sections.
    headings = list(HEADING.finditer(text))
    target = next((h for h in headings if h[2].strip() not in {"封面", "Cover"}), None)
    position = target.start() if target else len(text)
    addition = "## 简介（VNDB 原文）" + newline * 2 + block + newline * 2
    prefix = text[:position]
    if prefix and not prefix.endswith(newline * 2):
        prefix += newline * (1 if prefix.endswith(newline) else 2)
    return prefix + addition + text[position:]


def api_fetch(ids: list[str], retries: int = 5, timeout: int = 30) -> list[dict]:
    filters = ["id", "=", ids[0]] if len(ids) == 1 else ["or", *[["id", "=", key] for key in ids]]
    payload = json.dumps({"filters": filters, "fields": "id,title,description", "results": 100}).encode("utf-8")
    for attempt in range(retries):
        request = urllib.request.Request("https://api.vndb.org/kana/vn", data=payload,
                                         headers={"Content-Type": "application/json",
                                                  "User-Agent": "GalVault-synopsis-importer/1.0"})
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                data = json.load(response)
                if not isinstance(data, dict) or not isinstance(data.get("results"), list):
                    raise ValueError("Malformed VNDB response; notes are retained")
                if data.get("more"):
                    raise RuntimeError("Unexpected truncated VNDB response; reduce --batch-size")
                for row in data["results"]:
                    if (not isinstance(row, dict) or row.get("id") not in ids
                            or not isinstance(row.get("description"), (str, type(None)))):
                        raise ValueError("Invalid VNDB record; notes are retained")
                return data["results"]
        except urllib.error.HTTPError as error:
            if error.code != 429 and error.code < 500:
                raise RuntimeError(f"VNDB HTTP {error.code}") from error
            try:
                delay = max(1, float(error.headers.get("Retry-After", 10)))
            except ValueError:
                delay = 10
        except (urllib.error.URLError, TimeoutError, OSError):
            delay = min(30, 2 ** (attempt + 1))
        if attempt + 1 == retries:
            raise RuntimeError("VNDB request failed after retries; notes are retained")
        time.sleep(delay)
    raise RuntimeError("VNDB request failed")


def save_json(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def load_cache(root: Path) -> dict:
    path = root / "系统/简介导入/cache.json"
    cached = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    previous = root / "系统/内容清理/vndb-empty-work-records.json"
    if previous.exists():
        for key, value in json.loads(previous.read_text(encoding="utf-8")).items():
            if key not in cached and value.get("status") == "found":
                cached[key] = {"status": "found", "record": {k: value["record"].get(k)
                                                             for k in ("id", "title", "description")}}
    return cached


def import_synopses(root: Path, *, fetch=False, apply=False, refresh=False, batch_size=50,
                    delay=1.0, limit=None) -> dict:
    root = root.resolve()
    folder = root / "作品"
    if not folder.is_dir():
        raise ValueError(f"作品 folder does not exist: {folder}")
    cache = load_cache(root)
    entries = []
    actions = []
    for path in sorted(folder.glob("*.md")):
        raw = path.read_bytes()
        text = raw.decode("utf-8")
        key = vndb_id(text)
        if has_manual_intro(text) or (MANAGED.search(text) and not refresh):
            actions.append({"path": str(path.relative_to(root)), "status": "existing_intro"})
        elif key is None:
            actions.append({"path": str(path.relative_to(root)), "status": "no_vndb_id"})
        else:
            entries.append((path, raw, text, key))
    if limit is not None:
        excluded = entries[limit:]
        actions.extend({"path": str(p.relative_to(root)), "id": key, "status": "outside_limit"}
                       for p, _, _, key in excluded)
        entries = entries[:limit]
    keys = list(dict.fromkeys(item[3] for item in entries))
    needed = keys if refresh else [key for key in keys if key not in cache]
    failed = {}
    if fetch or apply:
        for offset in range(0, len(needed), batch_size):
            batch = needed[offset:offset + batch_size]
            try:
                rows = {row["id"]: row for row in api_fetch(batch)}
                for key in batch:
                    cache[key] = {"status": "found", "record": rows[key]} if key in rows else {"status": "not_found"}
                # Each successful batch is durable; failures are retried on the next run.
                save_json(root / "系统/简介导入/cache.json", cache)
                print(f"VNDB {min(offset + len(batch), len(needed))}/{len(needed)}", flush=True)
            except (RuntimeError, ValueError, KeyError) as error:
                failed.update({key: str(error) for key in batch})
                print(f"Batch failed; pages retained: {error}", flush=True)
            if offset + batch_size < len(needed):
                time.sleep(delay)
    updates = {}
    originals = {}
    for path, raw, text, key in entries:
        info = cache.get(key)
        if key in failed:
            status = "request_failed"
        elif info is None:
            status = "needs_fetch"
        elif info["status"] == "not_found":
            status = "not_found"
        else:
            row = info["record"]
            if row.get("id") != key:
                raise ValueError(f"Cache ID mismatch for {key}")
            if not markdown_description(row.get("description") or ""):
                status = "no_description"
            else:
                after = add_synopsis(text, row, refresh)
                if after == text:
                    status = "unchanged"
                else:
                    updates[path] = after.encode("utf-8")
                    originals[path] = raw
                    status = "added" if apply else "would_add"
        action = {"path": str(path.relative_to(root)), "id": key, "status": status}
        if key in failed:
            action["error"] = failed[key]
        actions.append(action)
    backup = None
    if apply and updates:
        # Check every candidate before modifying any note.
        for path, raw in originals.items():
            if path.read_bytes() != raw:
                raise RuntimeError(f"Concurrent edit detected; rerun: {path}")
        backup_folder = root.parent / "GalVault-maintenance-backups"
        backup_folder.mkdir(exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        backup = backup_folder / f"synopses-{stamp}.zip"
        with zipfile.ZipFile(backup, "x", compression=zipfile.ZIP_DEFLATED) as archive:
            for path, raw in originals.items():
                archive.writestr(path.relative_to(root).as_posix(), raw)
        with zipfile.ZipFile(backup) as archive:
            if archive.testzip() is not None:
                raise RuntimeError("Backup verification failed; no notes modified")
        for path, raw in updates.items():
            if path.read_bytes() != originals[path]:
                raise RuntimeError(f"Concurrent edit detected; remaining notes retained: {path}")
            temporary = path.with_name(path.name + ".tmp")
            temporary.write_bytes(raw)
            os.replace(temporary, path)
    report = {"mode": "apply" if apply else "preview", "summary": dict(Counter(a["status"] for a in actions)),
              "backup": str(backup) if backup else None, "actions": actions}
    # The default offline preview does not write even a report or cache.
    if fetch or apply:
        save_json(root / "系统/简介导入/report.json", report)
    return report


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--fetch", action="store_true", help="query and cache; do not modify notes")
    parser.add_argument("--apply", action="store_true", help="query, back up, and add synopses")
    parser.add_argument("--refresh", action="store_true", help="refresh cached data and importer-owned blocks only")
    parser.add_argument("--batch-size", type=int, default=50)
    parser.add_argument("--delay", type=float, default=1)
    parser.add_argument("--limit", type=int, help="process at most this many eligible notes")
    args = parser.parse_args()
    if not 1 <= args.batch_size <= 100 or args.delay < 0 or (args.limit is not None and args.limit < 1):
        parser.error("batch-size must be 1..100, delay nonnegative, limit positive")
    try:
        report = import_synopses(args.root, fetch=args.fetch, apply=args.apply, refresh=args.refresh,
                                 batch_size=args.batch_size, delay=args.delay, limit=args.limit)
        print(json.dumps({"mode": report["mode"], "summary": report["summary"], "backup": report["backup"]},
                         ensure_ascii=False, indent=2))
        if report["summary"].get("request_failed"):
            return 1
        return 0
    except (RuntimeError, ValueError, OSError, UnicodeError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

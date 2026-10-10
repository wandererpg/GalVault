#!/usr/bin/env python3
"""Merge only release pages that are byte-for-byte equivalent apart from VNDB IDs.

The default mode is a read-only preview. Pass ``--apply`` to merge verified
duplicates, preserve their source records and aliases, retarget links, and remove
the duplicate notes.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path


LINK_RE = re.compile(r"\[\[([^\]]+)\]\]")
AUTO_BLOCK_RE = re.compile(
    r"(<!-- AUTO:BEGIN ([^>]+) -->)(.*?)(<!-- AUTO:END \2 -->)", re.DOTALL
)
FRONTMATTER_RE = re.compile(r"\A---\r?\n(.*?)\r?\n---(\r?\n|\Z)", re.DOTALL)
ENTITY_RE = re.compile(
    r'(?m)^entity_id:\s*["\']?release:vndb:(r\d+):work:vndb:work:(v\d+)["\']?\s*$'
)


@dataclass(frozen=True)
class ReleasePage:
    path: Path
    text: str
    release_id: str
    work_id: str
    normalized_text: str


@dataclass(frozen=True)
class MergePlan:
    keeper: ReleasePage
    duplicates: tuple[ReleasePage, ...]
    scores: dict[Path, tuple[int, int]]


def normalized_release_text(text: str) -> str:
    """Normalize only source-record IDs; every other byte must match."""
    text = re.sub(
        r'(?m)^(entity_id:\s*["\']?release:vndb:)r\d+',
        r"\1<release-id>",
        text,
    )
    text = re.sub(
        r'(?m)^(\s+vndb:\s*["\'])r\d+(["\']\s*)$',
        r"\1<release-id>\2",
        text,
    )
    text = re.sub(r"https://vndb\.org/r\d+", "https://vndb.org/<release-id>", text)
    text = re.sub(r"vndb:r\d+", "vndb:<release-id>", text)
    return text


def read_release_pages(root: Path) -> list[ReleasePage]:
    pages: list[ReleasePage] = []
    for path in sorted((root / "发行版").glob("*.md")):
        text = path.read_text(encoding="utf-8")
        match = ENTITY_RE.search(text)
        if not match:
            continue
        pages.append(
            ReleasePage(
                path=path,
                text=text,
                release_id=match.group(1),
                work_id=match.group(2),
                normalized_text=normalized_release_text(text),
            )
        )
    return pages


def find_exact_duplicate_groups(root: Path) -> list[tuple[ReleasePage, ...]]:
    groups: dict[tuple[str, str], list[ReleasePage]] = defaultdict(list)
    for page in read_release_pages(root):
        groups[(page.work_id, page.normalized_text)].append(page)
    return [tuple(group) for group in groups.values() if len(group) > 1]


def link_target(link_contents: str) -> str:
    target = link_contents.split("|", 1)[0].split("#", 1)[0].strip()
    return target.replace("\\", "/").rsplit("/", 1)[-1]


def link_counts(root: Path, groups: list[tuple[ReleasePage, ...]]) -> dict[Path, tuple[int, int]]:
    """Return (distinct incident links, inbound mentions) for candidate pages."""
    candidates = {page.path.stem: page.path for group in groups for page in group}
    incoming_pages: dict[Path, set[Path]] = defaultdict(set)
    incoming_mentions: Counter[Path] = Counter()
    outgoing: dict[Path, set[str]] = defaultdict(set)

    for source in root.rglob("*.md"):
        try:
            text = source.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            continue
        targets = [link_target(match) for match in LINK_RE.findall(text)]
        for target in targets:
            candidate = candidates.get(target)
            if candidate is not None and source != candidate:
                incoming_pages[candidate].add(source)
                incoming_mentions[candidate] += 1
        if source in candidates.values():
            outgoing[source].update(target for target in targets if target)

    return {
        path: (len(incoming_pages[path]) + len(outgoing[path]), incoming_mentions[path])
        for path in candidates.values()
    }


def keeper_sort_key(page: ReleasePage, scores: dict[Path, tuple[int, int]]) -> tuple[int, int, int, int, str]:
    connections, mentions = scores[page.path]
    stem = page.path.stem
    disambiguators = stem.count("（") + stem.count("(")
    return (-connections, -mentions, disambiguators, len(stem), stem.casefold())


def make_merge_plans(root: Path) -> list[MergePlan]:
    groups = find_exact_duplicate_groups(root)
    scores = link_counts(root, groups)
    plans: list[MergePlan] = []
    for group in groups:
        ordered = sorted(group, key=lambda page: keeper_sort_key(page, scores))
        plans.append(MergePlan(ordered[0], tuple(ordered[1:]), scores))
    return plans


def add_merge_metadata(text: str, keeper: ReleasePage, duplicates: tuple[ReleasePage, ...]) -> str:
    frontmatter = FRONTMATTER_RE.match(text)
    if not frontmatter:
        raise ValueError(f"Missing frontmatter: {keeper.path}")

    all_pages = (keeper, *duplicates)
    record_lines = ["merged_source_records:"]
    for page in all_pages:
        record_lines.extend(
            [
                f'  - id: "{page.release_id}"',
                f'    url: "https://vndb.org/{page.release_id}"',
            ]
        )

    aliases = sorted({page.path.stem for page in duplicates}, key=str.casefold)
    alias_lines = ["aliases:", *(f"  - {json.dumps(alias, ensure_ascii=False)}" for alias in aliases)]
    addition = "\n".join(record_lines + alias_lines)

    front = frontmatter.group(1)
    if re.search(r"(?m)^(merged_source_records|aliases):", front):
        raise ValueError(f"Merge metadata already exists: {keeper.path}")
    new_front = f"{front}\n{addition}"
    return f"---\n{new_front}\n---{frontmatter.group(2)}{text[frontmatter.end():]}"


def rewrite_wikilinks(text: str, redirects: dict[str, str]) -> str:
    def replace(match: re.Match[str]) -> str:
        contents = match.group(1)
        target, separator, alias = contents.partition("|")
        base, hash_mark, heading = target.partition("#")
        old_stem = link_target(base)
        new_stem = redirects.get(old_stem)
        if new_stem is None:
            return match.group(0)
        prefix = f"{new_stem}{hash_mark}{heading}"
        return f"[[{prefix}{separator}{alias}]]"

    return LINK_RE.sub(replace, text)


def deduplicate_auto_block_lines(text: str) -> str:
    def replace_block(match: re.Match[str]) -> str:
        seen: set[str] = set()
        kept: list[str] = []
        for line in match.group(3).splitlines(keepends=True):
            key = line.strip()
            if key and key in seen:
                continue
            if key:
                seen.add(key)
            kept.append(line)
        return f"{match.group(1)}{''.join(kept)}{match.group(4)}"

    return AUTO_BLOCK_RE.sub(replace_block, text)


def apply_merge_plans(root: Path, plans: list[MergePlan]) -> tuple[int, int]:
    redirects: dict[str, str] = {}
    deleted_paths: set[Path] = set()
    updates: dict[Path, str] = {}

    for plan in plans:
        if normalized_release_text(plan.keeper.text) != normalized_release_text(plan.duplicates[0].text):
            raise ValueError(f"Pages no longer match: {plan.keeper.path}")
        updates[plan.keeper.path] = add_merge_metadata(plan.keeper.text, plan.keeper, plan.duplicates)
        for duplicate in plan.duplicates:
            redirects[duplicate.path.stem] = plan.keeper.path.stem
            deleted_paths.add(duplicate.path)

    changed_count = 0
    for path in root.rglob("*.md"):
        if path in deleted_paths:
            continue
        original = updates.get(path)
        if original is None:
            try:
                original = path.read_text(encoding="utf-8")
            except (OSError, UnicodeError):
                continue
        redirected = rewrite_wikilinks(original, redirects)
        rewritten = (
            deduplicate_auto_block_lines(redirected)
            if redirected != original
            else original
        )
        if rewritten != original:
            updates[path] = rewritten
            changed_count += 1

    for path, content in updates.items():
        if path not in deleted_paths:
            path.write_text(content, encoding="utf-8", newline="")
    for path in deleted_paths:
        path.unlink()
    return changed_count, len(deleted_paths)


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--apply", action="store_true", help="perform the merge; preview is the default")
    args = parser.parse_args()

    plans = make_merge_plans(args.root.resolve())
    print(f"Found {len(plans)} exact-duplicate groups ({sum(len(p.duplicates) for p in plans)} pages to remove).")
    for plan in plans:
        score = plan.scores[plan.keeper.path]
        print(
            f"KEEP {plan.keeper.path.name} [connections={score[0]}, inbound mentions={score[1]}]"
            f" <- {', '.join(page.path.name for page in plan.duplicates)}"
        )
    if args.apply and plans:
        changed, removed = apply_merge_plans(args.root.resolve(), plans)
        print(f"Updated {changed} linked notes; removed {removed} exact duplicate release pages.")


if __name__ == "__main__":
    main()

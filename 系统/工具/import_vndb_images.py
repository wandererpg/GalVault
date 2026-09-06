#!/usr/bin/env python3
"""Import VNDB cover and character images into the GalVault Obsidian vault.

The importer is resumable: image indexes and converted WebP files are reused on
subsequent runs. Notes are only updated after the corresponding image exists.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import io
import json
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from PIL import Image, ImageOps


API_BASE = "https://api.vndb.org/kana"
USER_AGENT = "GalVaultImageImporter/1.0"
BATCH_SIZE = 100
API_RETRIES = 7
DOWNLOAD_RETRIES = 5


@dataclass(frozen=True)
class Entry:
    vndb_id: str
    note_path: Path


@dataclass(frozen=True)
class ImageJob:
    entry: Entry
    source_url: str
    target_path: Path
    relative_path: str
    kind: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Download VNDB covers and character portraits as WebP files."
    )
    parser.add_argument(
        "--vault",
        type=Path,
        default=Path(__file__).resolve().parents[2],
        help="Vault root; defaults to the root containing this script.",
    )
    parser.add_argument(
        "--kind",
        choices=("all", "works", "characters"),
        default="all",
        help="Which image groups to import.",
    )
    parser.add_argument("--limit", type=int, default=0, help="Limit each group for testing.")
    parser.add_argument("--workers", type=int, default=12, help="Concurrent downloads.")
    parser.add_argument("--quality", type=int, default=80, help="WebP quality (1-100).")
    parser.add_argument(
        "--refresh-index",
        action="store_true",
        help="Query VNDB again even when a cached image index exists.",
    )
    parser.add_argument(
        "--no-update-notes",
        action="store_true",
        help="Download images without adding properties and embeds to notes.",
    )
    return parser.parse_args()


def batched(items: list[str], size: int) -> Iterable[list[str]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


def discover_entries(folder: Path, prefix: str) -> list[Entry]:
    entries: list[Entry] = []
    id_pattern = re.compile(r'^\s+vndb:\s*"?(' + re.escape(prefix) + r'\d+)"?\s*$', re.M)
    for note_path in sorted(folder.glob("*.md"), key=lambda p: p.name.casefold()):
        try:
            text = note_path.read_text(encoding="utf-8-sig")
        except UnicodeDecodeError:
            print(f"WARN unable to decode: {note_path}", flush=True)
            continue
        match = id_pattern.search(text)
        if match:
            entries.append(Entry(match.group(1), note_path))
    return entries


def api_request(endpoint: str, ids: list[str], fields: str) -> list[dict]:
    filters: list | str
    if len(ids) == 1:
        filters = ["id", "=", ids[0]]
    else:
        filters = ["or", *[["id", "=", item_id] for item_id in ids]]
    payload = json.dumps(
        {"filters": filters, "fields": fields, "results": 100},
        ensure_ascii=False,
    ).encode("utf-8")
    request = urllib.request.Request(
        f"{API_BASE}/{endpoint}",
        data=payload,
        headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
        method="POST",
    )
    for attempt in range(API_RETRIES):
        try:
            with urllib.request.urlopen(request, timeout=45) as response:
                return json.load(response).get("results", [])
        except urllib.error.HTTPError as exc:
            if exc.code == 429:
                delay = int(exc.headers.get("Retry-After", "10"))
            elif 500 <= exc.code < 600:
                delay = min(60, 2 ** attempt)
            else:
                raise
        except (urllib.error.URLError, TimeoutError):
            delay = min(60, 2 ** attempt)
        if attempt == API_RETRIES - 1:
            raise RuntimeError(f"VNDB API failed after {API_RETRIES} attempts")
        time.sleep(delay)
    return []


def load_or_build_index(
    entries: list[Entry],
    endpoint: str,
    cache_path: Path,
    refresh: bool,
) -> dict[str, dict]:
    cached: dict[str, dict] = {}
    if cache_path.exists():
        try:
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            print(f"WARN rebuilding invalid cache: {cache_path}", flush=True)
            cached = {}

    ids = [entry.vndb_id for entry in entries]
    pending = ids if refresh else [item_id for item_id in ids if item_id not in cached]
    total_batches = (len(pending) + BATCH_SIZE - 1) // BATCH_SIZE
    fields = "image{id,url,thumbnail}" if endpoint == "vn" else "image{id,url}"

    for batch_number, id_batch in enumerate(batched(pending, BATCH_SIZE), start=1):
        results = api_request(endpoint, id_batch, fields)
        returned = {item["id"]: item.get("image") for item in results}
        for item_id in id_batch:
            cached[item_id] = returned.get(item_id)
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(
            json.dumps(cached, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        print(
            f"INDEX {endpoint} {batch_number}/{total_batches} "
            f"({min(batch_number * BATCH_SIZE, len(pending))}/{len(pending)})",
            flush=True,
        )
    return cached


def choose_url(image_info: dict | None, kind: str) -> str | None:
    if not image_info:
        return None
    if kind == "cover":
        return image_info.get("thumbnail") or image_info.get("url")
    return image_info.get("url")


def download_bytes(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    for attempt in range(DOWNLOAD_RETRIES):
        try:
            with urllib.request.urlopen(request, timeout=45) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            if exc.code == 429 or 500 <= exc.code < 600:
                delay = min(30, 2 ** attempt)
            else:
                raise
        except (urllib.error.URLError, TimeoutError):
            delay = min(30, 2 ** attempt)
        if attempt == DOWNLOAD_RETRIES - 1:
            raise RuntimeError(f"Download failed after {DOWNLOAD_RETRIES} attempts: {url}")
        time.sleep(delay)
    raise RuntimeError(f"Download failed: {url}")


def convert_to_webp(data: bytes, target_path: Path, kind: str, quality: int) -> tuple[int, int]:
    with Image.open(io.BytesIO(data)) as source:
        source = ImageOps.exif_transpose(source)
        max_size = (360, 520) if kind == "cover" else (320, 480)
        source.thumbnail(max_size, Image.Resampling.LANCZOS)
        has_alpha = source.mode in ("RGBA", "LA") or (
            source.mode == "P" and "transparency" in source.info
        )
        converted = source.convert("RGBA" if has_alpha else "RGB")
        target_path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = target_path.with_suffix(".webp.part")
        converted.save(
            temporary_path,
            format="WEBP",
            quality=quality,
            method=6,
            exact=has_alpha,
        )
        temporary_path.replace(target_path)
        return converted.size


def run_download(job: ImageJob, quality: int) -> dict:
    if job.target_path.exists() and job.target_path.stat().st_size > 0:
        with Image.open(job.target_path) as existing:
            width, height = existing.size
        status = "existing"
    else:
        data = download_bytes(job.source_url)
        width, height = convert_to_webp(data, job.target_path, job.kind, quality)
        status = "downloaded"
    return {
        "id": job.entry.vndb_id,
        "kind": job.kind,
        "source_url": job.source_url,
        "path": job.relative_path,
        "width": width,
        "height": height,
        "bytes": job.target_path.stat().st_size,
        "status": status,
    }


def yaml_property(text: str, key: str, value: str, newline: str) -> str:
    pattern = re.compile(rf"^{re.escape(key)}:\s*.*$", re.M)
    replacement = f'{key}: "{value}"'
    if pattern.search(text):
        return pattern.sub(replacement, text, count=1)
    frontmatter_end = text.find(f"{newline}---", 3)
    if frontmatter_end == -1:
        return text
    return text[:frontmatter_end] + newline + replacement + text[frontmatter_end:]


def image_section(text: str, heading: str, embed: str, newline: str) -> str:
    begin = "<!-- AUTO:BEGIN image -->"
    end = "<!-- AUTO:END image -->"
    block = f"{begin}{newline}{embed}{newline}{end}"
    pattern = re.compile(
        re.escape(begin) + r".*?" + re.escape(end),
        flags=re.S,
    )
    if pattern.search(text):
        return pattern.sub(block, text, count=1)
    h1_match = re.search(r"^# .+$", text, flags=re.M)
    if not h1_match:
        return text + newline + newline + f"## {heading}" + newline + block + newline
    insert_at = h1_match.end()
    addition = newline + newline + f"## {heading}" + newline + block
    return text[:insert_at] + addition + text[insert_at:]


def update_note(job: ImageJob) -> bool:
    raw = job.entry.note_path.read_bytes()
    has_bom = raw.startswith(b"\xef\xbb\xbf")
    text = raw.decode("utf-8-sig")
    newline = "\r\n" if "\r\n" in text else "\n"
    if job.kind == "cover":
        key, heading, width = "cover_image", "封面", 300
    else:
        key, heading, width = "portrait_image", "角色画像", 250
    updated = yaml_property(text, key, job.relative_path, newline)
    updated = image_section(
        updated,
        heading,
        f"![[{job.relative_path}|{width}]]",
        newline,
    )
    if updated == text:
        return False
    encoded = updated.encode("utf-8")
    if has_bom:
        encoded = b"\xef\xbb\xbf" + encoded
    job.entry.note_path.write_bytes(encoded)
    return True


def make_jobs(
    entries: list[Entry],
    index: dict[str, dict],
    vault: Path,
    kind: str,
) -> tuple[list[ImageJob], int]:
    folder = "covers" if kind == "cover" else "characters"
    jobs: list[ImageJob] = []
    missing = 0
    for entry in entries:
        source_url = choose_url(index.get(entry.vndb_id), kind)
        if not source_url:
            missing += 1
            continue
        relative = f"assets/{folder}/{entry.vndb_id}.webp"
        jobs.append(
            ImageJob(
                entry=entry,
                source_url=source_url,
                target_path=vault / Path(relative),
                relative_path=relative,
                kind=kind,
            )
        )
    return jobs, missing


def process_jobs(
    jobs: list[ImageJob],
    workers: int,
    quality: int,
    update_notes: bool,
) -> tuple[list[dict], list[dict], int]:
    records: list[dict] = []
    errors: list[dict] = []
    updated_notes = 0
    lock = threading.Lock()
    total = len(jobs)

    def worker(job: ImageJob) -> tuple[ImageJob, dict]:
        return job, run_download(job, quality)

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(worker, job): job for job in jobs}
        for completed, future in enumerate(concurrent.futures.as_completed(futures), start=1):
            job = futures[future]
            try:
                finished_job, record = future.result()
                if update_notes and update_note(finished_job):
                    with lock:
                        updated_notes += 1
                records.append(record)
            except Exception as exc:  # Continue so a later run can retry failures.
                errors.append(
                    {"id": job.entry.vndb_id, "url": job.source_url, "error": str(exc)}
                )
            if completed % 100 == 0 or completed == total:
                print(
                    f"IMAGES {completed}/{total} ok={len(records)} errors={len(errors)}",
                    flush=True,
                )
    return records, errors, updated_notes


def write_reports(
    vault: Path,
    records: list[dict],
    errors: list[dict],
    discovered: dict[str, int],
    missing: dict[str, int],
    updated_notes: int,
) -> None:
    report_dir = vault / "系统" / "图片导入"
    report_dir.mkdir(parents=True, exist_ok=True)
    records.sort(key=lambda item: (item["kind"], item["id"]))
    manifest = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": "VNDB Kana API",
        "images": records,
    }
    (report_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (report_dir / "errors.json").write_text(
        json.dumps(errors, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    total_bytes = sum(item["bytes"] for item in records)
    downloaded = sum(item["status"] == "downloaded" for item in records)
    existing = sum(item["status"] == "existing" for item in records)
    report = f"""# 图片导入报告

- 数据来源：VNDB Kana API
- 生成时间：{datetime.now().astimezone().isoformat(timespec='seconds')}
- 发现作品条目：{discovered.get('works', 0)}
- 发现角色条目：{discovered.get('characters', 0)}
- 成功记录图片：{len(records)}
- 本次下载：{downloaded}
- 已存在并复用：{existing}
- 无图片数据的作品：{missing.get('covers', 0)}
- 无图片数据的角色：{missing.get('characters', 0)}
- 更新页面：{updated_notes}
- 下载失败：{len(errors)}
- 图片总体积：{total_bytes / 1024 / 1024:.1f} MiB

角色图片来自 VNDB 的单张人物画像，并非游戏资源中的完整立绘差分。
图片版权归原权利人所有；公开发布或上传第三方平台前，请确认相应授权和平台规则。
"""
    (report_dir / "导入报告.md").write_text(report, encoding="utf-8")


def main() -> int:
    args = parse_args()
    vault = args.vault.resolve()
    work_entries = discover_entries(vault / "作品", "v") if args.kind in ("all", "works") else []
    character_entries = (
        discover_entries(vault / "角色", "c") if args.kind in ("all", "characters") else []
    )
    if args.limit > 0:
        work_entries = work_entries[: args.limit]
        character_entries = character_entries[: args.limit]

    cache_dir = vault / "系统" / "图片导入"
    all_jobs: list[ImageJob] = []
    missing = {"covers": 0, "characters": 0}

    if work_entries:
        work_index = load_or_build_index(
            work_entries,
            "vn",
            cache_dir / "vn-image-index.json",
            args.refresh_index,
        )
        jobs, missing["covers"] = make_jobs(work_entries, work_index, vault, "cover")
        all_jobs.extend(jobs)

    if character_entries:
        character_index = load_or_build_index(
            character_entries,
            "character",
            cache_dir / "character-image-index.json",
            args.refresh_index,
        )
        jobs, missing["characters"] = make_jobs(
            character_entries, character_index, vault, "character"
        )
        all_jobs.extend(jobs)

    print(
        f"DISCOVER works={len(work_entries)} characters={len(character_entries)} "
        f"jobs={len(all_jobs)} missing={sum(missing.values())}",
        flush=True,
    )
    records, errors, updated_notes = process_jobs(
        all_jobs,
        max(1, args.workers),
        max(1, min(100, args.quality)),
        not args.no_update_notes,
    )
    write_reports(
        vault,
        records,
        errors,
        {"works": len(work_entries), "characters": len(character_entries)},
        missing,
        updated_notes,
    )
    print(
        f"DONE images={len(records)} errors={len(errors)} updated_notes={updated_notes}",
        flush=True,
    )
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())

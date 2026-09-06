#!/usr/bin/env python3
"""Fetch VNDB character names and add machine-generated Chinese display titles.

The script deliberately keeps filenames and wikilink targets unchanged.  Only
frontmatter display fields and the first H1 are updated, so existing vault links
remain stable.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
CHARACTER_DIR = ROOT / "角色"
WORK_DIR = ROOT / "系统" / "角色中文化"
CACHE_PATH = WORK_DIR / "vndb-character-names.json"
OVERRIDE_PATH = WORK_DIR / "中文名修订.csv"
REPORT_PATH = WORK_DIR / "生成报告.md"
VENDOR_DIR = Path(__file__).resolve().parent / "vendor"
API_URL = "https://api.vndb.org/kana/character"

sys.path.insert(0, str(VENDOR_DIR))
from opencc import OpenCC  # type: ignore  # noqa: E402
from pykakasi import kakasi  # type: ignore  # noqa: E402


HAN_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
KANA_RE = re.compile(r"[\u3040-\u30ffー]")
SOURCE_ID_RE = re.compile(r'^\s*vndb:\s*"(c\d+)"\s*$', re.MULTILINE)
FRONTMATTER_RE = re.compile(r"\A---\r?\n(.*?)\r?\n---\r?\n", re.DOTALL)

ROLE_GLOSSARY = {
    "protagonist": "主人公", "main character": "主人公", "hero": "男主角",
    "heroine": "女主角", "teacher": "老师", "sensei": "老师",
    "mother": "母亲", "mom": "妈妈", "mama": "妈妈", "father": "父亲",
    "dad": "爸爸", "papa": "爸爸", "parents": "父母", "sister": "姐妹",
    "older sister": "姐姐", "elder sister": "姐姐", "younger sister": "妹妹",
    "brother": "兄弟", "older brother": "哥哥", "elder brother": "哥哥",
    "younger brother": "弟弟", "grandmother": "祖母", "grandfather": "祖父",
    "aunt": "阿姨", "uncle": "叔叔", "daughter": "女儿", "son": "儿子",
    "classmate": "同学", "friend": "朋友", "doctor": "医生", "nurse": "护士",
    "manager": "经理", "master": "主人", "maid": "女仆", "butler": "管家",
    "principal": "校长", "president": "会长", "captain": "队长",
    "shopkeeper": "店主", "waitress": "女服务员", "waiter": "男服务员",
    "girl": "少女", "boy": "少年", "woman": "女性", "man": "男性",
    "unknown": "身份不明者", "narrator": "旁白", "announcer": "播音员",
}

WESTERN = {
    "alice": "爱丽丝", "alicia": "艾莉西亚", "anna": "安娜", "anne": "安妮",
    "aria": "艾莉亚", "arthur": "亚瑟", "bella": "贝拉", "charlotte": "夏洛特",
    "chris": "克里斯", "christina": "克里斯蒂娜", "claire": "克莱尔",
    "diana": "黛安娜", "elena": "艾琳娜", "elizabeth": "伊丽莎白",
    "emilia": "艾米莉亚", "emily": "艾米丽", "erica": "艾莉卡",
    "eva": "伊娃", "flora": "芙洛拉", "francis": "弗朗西斯", "george": "乔治",
    "grace": "格蕾丝", "henry": "亨利", "iris": "伊莉丝", "jane": "简",
    "julia": "茱莉亚", "karen": "卡莲", "kate": "凯特", "laura": "劳拉",
    "lily": "莉莉", "lisa": "莉莎", "lucia": "露西亚", "lucy": "露西",
    "maria": "玛丽亚", "marie": "玛丽", "mary": "玛丽", "michael": "迈克尔",
    "nina": "妮娜", "noel": "诺艾尔", "olivia": "奥莉维亚", "paul": "保罗",
    "rebecca": "丽贝卡", "richard": "理查德", "robert": "罗伯特",
    "rose": "萝丝", "sara": "莎拉", "sarah": "莎拉", "sophia": "索菲娅",
    "stella": "斯黛拉", "victor": "维克托", "victoria": "维多利亚",
    "william": "威廉", "yuri": "尤里",
}

KANA_WORDS = {
    "さくら": "樱", "サクラ": "樱", "ひなた": "日向", "ヒナタ": "日向",
    "あかり": "明里", "アカリ": "明里", "あおい": "葵", "アオイ": "葵",
    "あやか": "彩香", "アヤカ": "彩香", "かおり": "香织", "カオリ": "香织",
    "かなで": "奏", "カナデ": "奏", "ことり": "小鸟", "コトリ": "小鸟",
    "しおり": "栞", "シオリ": "栞", "つばさ": "翼", "ツバサ": "翼",
    "ななみ": "七海", "ナナミ": "七海", "のぞみ": "望", "ノゾミ": "望",
    "はるか": "遥", "ハルカ": "遥", "ひかり": "光", "ヒカリ": "光",
    "ほたる": "萤", "ホタル": "萤", "みどり": "绿", "ミドリ": "绿",
    "みなみ": "南", "ミナミ": "南", "みらい": "未来", "ミライ": "未来",
    "ゆかり": "紫", "ユカリ": "紫", "ゆき": "雪", "ユキ": "雪",
    "アリス": "爱丽丝", "エリカ": "艾莉卡", "エミリア": "艾米莉亚",
    "クララ": "克拉拉", "サラ": "莎拉", "ソフィア": "索菲娅",
    "マリア": "玛丽亚", "マリー": "玛丽", "リリア": "莉莉娅",
    "リリス": "莉莉丝", "ルナ": "露娜", "ローズ": "萝丝",
}

MORA = {
    "a": "阿", "i": "伊", "u": "乌", "e": "惠", "o": "奥",
    "ka": "加", "ki": "希", "ku": "久", "ke": "惠", "ko": "子",
    "ga": "雅", "gi": "吉", "gu": "古", "ge": "格", "go": "悟",
    "sa": "沙", "shi": "诗", "su": "寿", "se": "濑", "so": "索",
    "za": "扎", "ji": "吉", "zu": "兹", "ze": "泽", "zo": "藏",
    "ta": "太", "chi": "千", "tsu": "津", "te": "特", "to": "斗",
    "da": "达", "de": "德", "do": "堂", "na": "奈", "ni": "尼",
    "nu": "努", "ne": "音", "no": "乃", "ha": "羽", "hi": "日",
    "fu": "芙", "he": "惠", "ho": "穗", "ba": "巴", "bi": "比",
    "bu": "布", "be": "贝", "bo": "博", "pa": "帕", "pi": "皮",
    "pu": "普", "pe": "佩", "po": "波", "ma": "真", "mi": "美",
    "mu": "梦", "me": "芽", "mo": "莫", "ya": "雅", "yu": "优",
    "yo": "代", "ra": "拉", "ri": "莉", "ru": "露", "re": "蕾",
    "ro": "洛", "wa": "和", "wo": "奥", "n": "恩",
    "kya": "佳", "kyu": "久", "kyo": "京", "sha": "夏", "shu": "修",
    "sho": "翔", "cha": "恰", "chu": "秋", "cho": "绪", "nya": "妮娅",
    "nyu": "纽", "nyo": "妮奥", "hya": "希雅", "hyu": "休", "hyo": "晓",
    "mya": "米娅", "myu": "缪", "myo": "妙", "rya": "莉娅",
    "ryu": "龙", "ryo": "凉", "gya": "佳", "gyu": "久", "gyo": "乔",
    "ja": "嘉", "ju": "朱", "jo": "乔", "bya": "比娅", "pya": "皮娅",
}


def q(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def character_files() -> list[tuple[Path, str]]:
    records: list[tuple[Path, str]] = []
    for path in sorted(CHARACTER_DIR.glob("*.md")):
        text = path.read_text(encoding="utf-8")
        match = SOURCE_ID_RE.search(text)
        if not match:
            raise RuntimeError(f"Missing VNDB character id: {path}")
        records.append((path, match.group(1)))
    return records


def api_request(ids: list[str]) -> dict:
    filters: list = ["or", *[["id", "=", cid] for cid in ids]]
    payload = json.dumps({
        "filters": filters,
        "fields": "id, name, original, aliases",
        "results": 100,
    }).encode("utf-8")
    request = urllib.request.Request(
        API_URL,
        data=payload,
        headers={"Content-Type": "application/json", "User-Agent": "GalVault/1.0"},
        method="POST",
    )
    for attempt in range(7):
        try:
            with urllib.request.urlopen(request, timeout=45) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            if exc.code not in (429, 500, 502, 503, 504) or attempt == 6:
                raise
            delay = float(exc.headers.get("Retry-After", 2 ** attempt))
            time.sleep(max(1.0, delay))
        except (TimeoutError, urllib.error.URLError):
            if attempt == 6:
                raise
            time.sleep(2 ** attempt)
    raise AssertionError("unreachable")


def fetch_names(records: list[tuple[Path, str]], refresh: bool) -> dict[str, dict]:
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    cache: dict[str, dict] = {}
    if CACHE_PATH.exists() and not refresh:
        cache = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    missing = [cid for _, cid in records if cid not in cache]
    for offset in range(0, len(missing), 100):
        batch = missing[offset:offset + 100]
        response = api_request(batch)
        for item in response.get("results", []):
            cache[item["id"]] = {
                "name": item.get("name") or "",
                "original": item.get("original") or "",
                "aliases": item.get("aliases") or [],
            }
        CACHE_PATH.write_text(
            json.dumps(cache, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        done = min(offset + len(batch), len(missing))
        print(f"Fetched {done}/{len(missing)}")
        time.sleep(0.35)
    return cache


def load_overrides() -> dict[str, str]:
    if not OVERRIDE_PATH.exists():
        return {}
    result: dict[str, str] = {}
    with OVERRIDE_PATH.open("r", encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            cid = (row.get("entity_id") or "").removeprefix("vndb:character:").strip()
            title = (row.get("title_zh") or "").strip()
            if cid and title:
                result[cid] = title
    return result


TRAD_TO_SIMP = OpenCC("t2s")
KAKASI = kakasi()
MORA_KEYS = sorted(MORA, key=len, reverse=True)


def load_japanese_variants() -> dict[int, str]:
    """Reverse OpenCC's traditional-to-Japanese character table."""
    path = VENDOR_DIR / "opencc" / "dictionary" / "JPVariants.txt"
    mapping: dict[int, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("#") or "\t" not in line:
            continue
        traditional, japanese = line.split("\t", 1)
        # The table is character based. Prefer the first canonical traditional form.
        if len(japanese) == 1 and len(traditional) == 1:
            mapping.setdefault(ord(japanese), traditional)
    return mapping


JP_TO_TRAD_TABLE = load_japanese_variants()


def simplify_japanese(text: str) -> str:
    return TRAD_TO_SIMP.convert(text.translate(JP_TO_TRAD_TABLE))


def roman_word_to_zh(word: str) -> str:
    key = re.sub(r"[^a-z]", "", word.lower())
    if not key:
        return word
    if key in WESTERN:
        return WESTERN[key]
    output: list[str] = []
    pos = 0
    while pos < len(key):
        matched = False
        for mora in MORA_KEYS:
            if key.startswith(mora, pos):
                output.append(MORA[mora])
                pos += len(mora)
                matched = True
                break
        if not matched:
            # Foreign consonants and other unmatched fragments stay searchable.
            output.append(key[pos].upper())
            pos += 1
    return "".join(output)


def kana_to_zh(text: str) -> str:
    if text in KANA_WORDS:
        return KANA_WORDS[text]
    roman = "".join(item["hepburn"] for item in KAKASI.convert(text))
    return roman_word_to_zh(roman)


def mixed_japanese_to_zh(text: str) -> str:
    text = simplify_japanese(text.strip())
    chunks = re.findall(r"[\u3040-\u30ffー]+|[A-Za-z]+|\s+|・|.|\n", text)
    output: list[str] = []
    has_han = bool(HAN_RE.search(text))
    for chunk in chunks:
        if KANA_RE.search(chunk):
            output.append(kana_to_zh(chunk))
        elif chunk.isspace():
            output.append("" if has_han else "·")
        elif chunk == "・":
            output.append("·")
        elif re.fullmatch(r"[A-Za-z]+", chunk):
            output.append(roman_word_to_zh(chunk))
        else:
            output.append(chunk)
    return re.sub(r"·+", "·", "".join(output)).strip("· ")


def generated_title(item: dict) -> tuple[str, str]:
    original = (item.get("original") or "").strip()
    roman = (item.get("name") or "").strip()
    role = ROLE_GLOSSARY.get(roman.casefold())
    if role:
        return role, "glossary"
    if original:
        if HAN_RE.search(original):
            return mixed_japanese_to_zh(original), "converted_original"
        if KANA_RE.search(original):
            return mixed_japanese_to_zh(original), "phonetic_transliteration"
    words = [part for part in re.split(r"[\s._·-]+", roman) if part]
    title = "·".join(roman_word_to_zh(part) for part in words)
    return title or roman, "phonetic_transliteration"


def unique_aliases(*groups) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for group in groups:
        values = group if isinstance(group, list) else [group]
        for value in values:
            value = str(value or "").strip()
            if value and value not in seen:
                seen.add(value)
                result.append(value)
    return result


def replace_frontmatter(text: str, cid: str, item: dict, title: str, quality: str) -> str:
    match = FRONTMATTER_RE.match(text)
    if not match:
        raise RuntimeError(f"Invalid frontmatter for {cid}")
    front = match.group(1)
    roman = (item.get("name") or "").strip()
    original = (item.get("original") or "").strip()
    aliases = unique_aliases(roman, original, item.get("aliases") or [])

    front = re.sub(r'^display_title:\s*.*$', f"display_title: {q(title)}", front, flags=re.MULTILINE)
    # Idempotently remove fields managed by this script.
    front = re.sub(r'^title_zh:\s*.*\n?', "", front, flags=re.MULTILINE)
    front = re.sub(r'^title_ja:\s*.*\n?', "", front, flags=re.MULTILINE)
    front = re.sub(r'^title_zh_quality:\s*.*\n?', "", front, flags=re.MULTILINE)
    front = re.sub(r'^title_status:\s*.*\n?', "", front, flags=re.MULTILINE)
    front = re.sub(r'^aliases:\s*\n(?:\s+-\s+.*\n?)*', "", front, flags=re.MULTILINE)

    insertion = [f"title_zh: {q(title)}"]
    if original:
        insertion.append(f"title_ja: {q(original)}")
    insertion.append(f"title_zh_quality: {q(quality)}")
    insertion.append('title_status: "machine_generated"')
    insertion.append("aliases:")
    insertion.extend(f"  - {q(alias)}" for alias in aliases if alias != title)
    block = "\n".join(insertion)
    front = re.sub(
        r'^(display_title:\s*.*)$', rf"\1\n{block}", front, count=1, flags=re.MULTILINE
    )

    body = text[match.end():]
    body = re.sub(
        r"\A(\s*)# [^\r\n]*(\r?\n)",
        lambda found: f"{found.group(1)}# {title}{found.group(2)}",
        body,
        count=1,
    )
    return f"---\n{front}\n---\n{body}"


def write_report(stats: dict[str, int], total: int) -> None:
    lines = [
        "# 角色中文名生成报告", "",
        f"- 处理角色：{total:,}",
        f"- 人工修订：{stats.get('manual_override', 0):,}",
        f"- 常用称谓词典：{stats.get('glossary', 0):,}",
        f"- 日文汉字转简体：{stats.get('converted_original', 0):,}",
        f"- 假名或罗马音音译：{stats.get('phonetic_transliteration', 0):,}",
        "- 文件名与内部链接：未改动", "",
        "> [!warning] 自动生成内容", "",
        "> 中文名是机器生成的候选标题，不等同于官方中文译名。发现不准确时，请在 `中文名修订.csv` 中添加修订后重新运行脚本。", "",
    ]
    REPORT_PATH.write_text("\n".join(lines), encoding="utf-8")


def apply_titles(records: list[tuple[Path, str]], cache: dict[str, dict]) -> None:
    overrides = load_overrides()
    stats: dict[str, int] = {}
    missing: list[str] = []
    for index, (path, cid) in enumerate(records, 1):
        item = cache.get(cid)
        if not item:
            missing.append(cid)
            continue
        if cid in overrides:
            title, quality = overrides[cid], "manual_override"
        else:
            title, quality = generated_title(item)
        stats[quality] = stats.get(quality, 0) + 1
        old = path.read_text(encoding="utf-8")
        new = replace_frontmatter(old, cid, item, title, quality)
        if new != old:
            path.write_text(new, encoding="utf-8", newline="\n")
        if index % 1000 == 0:
            print(f"Updated {index}/{len(records)}")
    if missing:
        raise RuntimeError(f"Missing {len(missing)} cached characters: {', '.join(missing[:10])}")
    write_report(stats, len(records))
    print(json.dumps(stats, ensure_ascii=False, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fetch-only", action="store_true", help="fetch/cache source names without editing pages")
    parser.add_argument("--refresh", action="store_true", help="ignore the existing VNDB name cache")
    args = parser.parse_args()
    records = character_files()
    print(f"Found {len(records)} character pages")
    cache = fetch_names(records, args.refresh)
    if not args.fetch_only:
        apply_titles(records, cache)


if __name__ == "__main__":
    main()

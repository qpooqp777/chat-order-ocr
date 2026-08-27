#!/usr/bin/env python3
"""Parse pasted/OCR chat orders, optionally OCRing a batch of image files.

OCR uses the locally installed Tesseract executable. The parser remains
conservative: uncertain OCR rows and unmatched catalog aliases are marked for
human review instead of being silently changed.
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import re
import subprocess
import sys
import unicodedata
from collections import defaultdict
from pathlib import Path
from typing import Iterable

CHINESE_NUMBERS = {"一": 1, "二": 2, "兩": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff"}


def chinese_number(value: str) -> int:
    if value.isdigit():
        return max(1, int(value))
    if value == "十":
        return 10
    if len(value) == 2 and value.startswith("十"):
        return 10 + CHINESE_NUMBERS.get(value[1], 0)
    if len(value) == 2 and value.endswith("十"):
        return CHINESE_NUMBERS.get(value[0], 1) * 10
    return max(1, sum(CHINESE_NUMBERS.get(char, 0) for char in value))


def normalize_line(value: str) -> str:
    return re.sub(r"\s+", " ", value.replace("\u200b", "")).replace("•", "").strip(" \t-*·")


def clean_person_candidate(line: str) -> str:
    """Remove emoji/symbol decorations from a person candidate only."""
    cleaned = "".join(char for char in line if unicodedata.category(char) not in {"So", "Sk", "Me"})
    cleaned = re.sub(r"[\ufe0e\ufe0f\u200d]", "", cleaned)
    cleaned = re.sub(r"^[\[【(（{][^\]】)）}]{1,8}[\]】)）}]\s*", "", cleaned)
    return re.sub(r"\s+", " ", cleaned).strip(" \\t[]【】()（）{}")


def clean_item_decorations(item: str) -> str:
    """Remove decorative emoji at item edges while preserving the item text."""
    item = item.strip()
    item = re.sub(r"^[^\w\u3400-\u9fff]+", "", item, flags=re.UNICODE)
    item = re.sub(r"[^\w\u3400-\u9fff]+$", "", item, flags=re.UNICODE)
    return item.strip()


def looks_like_person(line: str) -> bool:
    candidate = clean_person_candidate(line)
    if not 1 <= len(candidate) <= 12:
        return False
    if re.search(r"\d|[+＋:：/]|下午|上午|已讀|圖片|貼圖|回覆|公斤|粒|盒|包|斤|份|個", candidate):
        return False
    return not re.fullmatch(r"[哈呵啊嗯喔哦笑]+", candidate)


def inline_person_item(prefix: str) -> tuple[str, str] | None:
    """Split a same-line person/item prefix without guessing arbitrary phrases."""
    prefix = prefix.strip(" \t，,。；;：:—–-")
    if not prefix:
        return None
    # Explicit colon is the strongest same-line boundary: 姓名：品項。
    colon = re.match(r"^([^：:]{1,12})[：:]\s*(.+)$", prefix)
    if colon and looks_like_person(colon.group(1).strip()):
        return clean_person_candidate(colon.group(1)), colon.group(2).strip()
    # Measurement/packaging tokens belong to the item, not to a person's name.
    if re.search(r"\d|公斤|公克|克|斤|粒|盒|包|袋|份|個", prefix):
        return None
    # Whitespace boundary: accept a compact name token before the item.
    space = re.match(r"^([^\s]{1,12})\s+(.+)$", prefix)
    if space and looks_like_person(space.group(1)):
        return clean_person_candidate(space.group(1)), space.group(2).strip()
    return None


def coalesce_quantity_lines(text: str) -> list[str]:
    """Join a quantity split across adjacent chat/OCR lines, e.g. `item+` + `1`."""
    lines = [normalize_line(raw) for raw in text.splitlines()]
    merged: list[str] = []
    for line in lines:
        if merged and re.search(r"(?:\+|＋)\s*$", merged[-1]) and re.fullmatch(r"[0-9０-９一二三四五六七八九十兩]+", line):
            merged[-1] += line
        else:
            merged.append(line)
    return merged


def quantity_match(line: str) -> tuple[int, int, int] | None:
    plus = re.search(r"(?:\+|＋)\s*(\d+)", line)
    if plus:
        return int(plus.group(1)), plus.start(), plus.end() - plus.start()
    unit = re.search(r"(?:—|-|–)?\s*([一二三四五六七八九十兩\d]+)\s*(?:份|個|盒|包|袋)", line)
    if unit:
        return chinese_number(unit.group(1)), unit.start(), unit.end() - unit.start()
    return None


def load_catalog(path: Path | None) -> dict[str, str]:
    if not path:
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, dict) and "items" in payload:
        payload = payload["items"]
    if not isinstance(payload, dict):
        raise ValueError("品項字典必須是 JSON 物件，例如 {\"櫻桃\": [\"櫻桃2公斤\"]}")
    catalog: dict[str, str] = {}
    for standard, aliases in payload.items():
        values = aliases if isinstance(aliases, list) else [aliases]
        catalog[str(standard).strip()] = str(standard).strip()
        for alias in values:
            catalog[str(alias).strip()] = str(standard).strip()
    return catalog


def match_catalog(item: str, catalog: dict[str, str]) -> tuple[str, str]:
    if not catalog:
        return item, "未設定字典"
    normalized = re.sub(r"\s+", "", item).lower()
    candidates = sorted(catalog.items(), key=lambda pair: len(pair[0]), reverse=True)
    for alias, standard in candidates:
        alias_normalized = re.sub(r"\s+", "", alias).lower()
        if normalized == alias_normalized or alias_normalized in normalized:
            return standard, "已對應"
    return item, "未對應，請確認"


def parse_lines(text: str, source: str = "text", source_file: str | None = None, catalog: dict[str, str] | None = None) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    current_person = "未標註"
    catalog = catalog or {}
    person_needs_review = False
    for line in coalesce_quantity_lines(text):
        if not line or re.fullmatch(r"(?:下午|上午)?\s*\d{1,2}:\d{2}", line) or re.fullmatch(r"\d{1,3}", line):
            continue
        match = quantity_match(line)
        if not match:
            pending_person = re.fullmatch(r"([^：:]{1,12})[：:]", line)
            if pending_person and looks_like_person(pending_person.group(1)):
                cleaned_person = clean_person_candidate(pending_person.group(1))
                person_needs_review = cleaned_person != pending_person.group(1)
                current_person = cleaned_person
            elif looks_like_person(line):
                cleaned_person = clean_person_candidate(line)
                person_needs_review = cleaned_person != line
                current_person = cleaned_person
            continue
        qty, start, length = match
        prefix = re.sub(r"[，,。；;：:—–-]\s*$", "", line[:start]).strip()
        person = current_person
        inline_split = inline_person_item(prefix)
        if inline_split:
            person, item = inline_split
        else:
            item = prefix
        if not item:
            item = (line[:start] + line[start + length:]).strip()
        item = clean_item_decorations(item)
        if not item:
            continue
        matched_item, match_status = match_catalog(item, catalog)
        review_reasons: list[str] = []
        if source == "ocr":
            review_reasons.append("OCR 來源")
        if person == "未標註":
            review_reasons.append("未辨識訂購人")
        if match_status == "未對應，請確認":
            review_reasons.append("品項未對應")
        if inline_split:
            review_reasons.append("姓名與品項同列")
        if person_needs_review:
            review_reasons.append("姓名含裝飾字元")
        confidence = "check" if review_reasons else "high"
        note = "需人工查驗：" + "、".join(review_reasons) if review_reasons else ""
        rows.append({
            "person": person,
            "item": item,
            "matched_item": matched_item,
            "match_status": match_status,
            "qty": max(1, qty),
            "source": source,
            "source_file": source_file or "",
            "confidence": confidence,
            "備注": note,
        })
    return rows


def discover_images(images: list[Path], image_dir: Path | None) -> list[Path]:
    result = list(images)
    if image_dir:
        result.extend(sorted(path for path in image_dir.iterdir() if path.is_file() and path.suffix.lower() in IMAGE_SUFFIXES))
    seen: set[Path] = set()
    return [path for path in result if not (path in seen or seen.add(path))]


def ocr_image(path: Path, lang: str, psm: int) -> str:
    command = ["tesseract", str(path), "stdout", "-l", lang, "--psm", str(psm)]
    completed = subprocess.run(command, check=True, capture_output=True, text=True, encoding="utf-8", errors="replace")
    return completed.stdout.strip()


def ocr_images(paths: list[Path], lang: str, psm: int, catalog: dict[str, str]) -> tuple[list[dict[str, object]], list[dict[str, str]]]:
    rows: list[dict[str, object]] = []
    reports: list[dict[str, str]] = []
    for path in paths:
        try:
            text = ocr_image(path, lang, psm)
            image_rows = parse_lines(text, "ocr", str(path), catalog)
            rows.extend(image_rows)
            reports.append({"file": str(path), "status": "完成", "text_chars": str(len(text)), "rows": str(len(image_rows))})
        except (OSError, subprocess.CalledProcessError) as error:
            reports.append({"file": str(path), "status": "失敗", "error": str(error)})
    return rows, reports


def sort_rows(rows: Iterable[dict[str, object]], mode: str) -> list[dict[str, object]]:
    result = list(rows)
    if mode == "original":
        return result
    reverse = mode == "name-desc"
    return sorted(result, key=lambda row: str(row.get("person") or "未標註"), reverse=reverse)


def make_summary(rows: list[dict[str, object]]) -> dict[str, object]:
    people = {str(row["person"]).strip() for row in rows if str(row["person"]).strip()}
    products: defaultdict[str, int] = defaultdict(int)
    for row in rows:
        products[str(row["matched_item"] or row["item"]).strip()] += int(row["qty"])
    return {
        "people": len(people),
        "lines": len(rows),
        "units": sum(int(row["qty"]) for row in rows),
        "products": [{"item": item, "qty": qty} for item, qty in sorted(products.items(), key=lambda pair: (-pair[1], pair[0]))],
    }


def write_csv(rows: list[dict[str, object]], stream: io.TextIOBase) -> None:
    writer = csv.writer(stream)
    writer.writerow(["姓名", "原始品項", "對應品項", "數量", "來源", "來源檔案", "對應狀態", "信心", "備注"])
    for row in rows:
        writer.writerow([row["person"], row["item"], row["matched_item"], row["qty"], row["source"], row["source_file"], row["match_status"], row["confidence"], row["備注"]])


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="將聊天訂單訊息或批次截圖整理成可檢查、可分組排序的名單")
    parser.add_argument("--input", "-i", type=Path, help="輸入文字檔；未指定時讀取 stdin")
    parser.add_argument("--images", nargs="*", type=Path, default=[], help="批次圖片路徑，可同時指定多張")
    parser.add_argument("--images-dir", type=Path, help="讀取資料夾內的 JPG、PNG、WEBP、BMP、TIFF")
    parser.add_argument("--ocr-lang", default="chi_tra+eng", help="Tesseract 語言，例如 chi_tra+eng")
    parser.add_argument("--psm", type=int, default=6, help="Tesseract page segmentation mode，預設 6")
    parser.add_argument("--catalog", type=Path, help="品項字典 JSON：標準品項為 key，別名陣列為 value")
    parser.add_argument("--source", choices=["text", "ocr"], default="text", help="文字輸入來源")
    parser.add_argument("--format", choices=["json", "csv"], default="json", help="輸出格式，預設 JSON")
    parser.add_argument("--sort", choices=["original", "name-asc", "name-desc"], default="original", help="排序：原始順序、姓名 A→Z、姓名 Z→A")
    parser.add_argument("--group-by", choices=["none", "person"], default="none", help="依姓名分組；等同於姓名 A→Z")
    parser.add_argument("--output", "-o", type=Path, help="輸出檔；未指定時輸出到 stdout")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        catalog = load_catalog(args.catalog)
        rows: list[dict[str, object]] = []
        ocr_reports: list[dict[str, str]] = []
        if args.input:
            rows.extend(parse_lines(args.input.read_text(encoding="utf-8"), args.source, str(args.input), catalog))
        elif not args.images and not args.images_dir:
            rows.extend(parse_lines(sys.stdin.read(), args.source, None, catalog))
        image_paths = discover_images(args.images, args.images_dir)
        if image_paths:
            image_rows, ocr_reports = ocr_images(image_paths, args.ocr_lang, args.psm, catalog)
            rows.extend(image_rows)
        mode = "name-asc" if args.group_by == "person" and args.sort == "original" else args.sort
        rows = sort_rows(rows, mode)
        if args.format == "csv":
            buffer = io.StringIO(newline="")
            write_csv(rows, buffer)
            output = "\ufeff" + buffer.getvalue()
        else:
            payload = {"rows": rows, "summary": make_summary(rows), "sort": mode, "group_by": args.group_by, "ocr": ocr_reports}
            output = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
        if args.output:
            args.output.write_text(output, encoding="utf-8", newline="")
        else:
            sys.stdout.write(output)
        return 0
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(f"order-ocr: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

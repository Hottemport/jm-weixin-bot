#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 JM 相册目录合并成单个 PDF。

示例：
    python merge_pdf.py --id 350234
    python merge_pdf.py --id 350234 --root D:/AI/JM_Downloads --max-width 1600
    python merge_pdf.py --id 350234 --list
    python merge_pdf.py --id 350234 --clean

约定：相册目录是 <root>/<id>/，图片可以平铺，也可以放在分章子目录里；
输出固定为 <root>/<id>/JM_<id>.pdf。成功时往 stdout 打一行 JSON。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import List

IMAGE_SUFFIXES = {
    ".jpg",
    ".jpeg",
    ".jpe",
    ".png",
    ".webp",
    ".bmp",
    ".gif",
    ".tif",
    ".tiff",
}

DEFAULT_ROOT = r"D:\AI\JM_Downloads"
EXIF_ORIENTATION = 0x0112


def _emit(payload: dict) -> None:
    """输出一行 UTF-8 JSON，方便被 PowerShell 直接读取。"""
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    print(json.dumps(payload, ensure_ascii=False), flush=True)


def _fail(message: str, code: int = 2) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    print(json.dumps({"error": message}, ensure_ascii=False), flush=True)
    return code


def _natural_key(text: str):
    """数字感知排序：'9.jpg' < '10.jpg'，'2/1.jpg' < '10/1.jpg'。"""
    return [int(part) if part.isdigit() else part for part in re.split(r"(\d+)", text.lower())]


def collect_images(folder: Path) -> List[Path]:
    files = [
        path
        for path in folder.rglob("*")
        if path.is_file() and path.suffix.lower() in IMAGE_SUFFIXES
    ]
    files.sort(key=lambda path: _natural_key(path.relative_to(folder).as_posix()))
    return files


def _open_page(path: Path, max_width: int):
    """打开一张图并统一成 RGB；普通 RGB 图保持懒加载以压住内存。"""
    from PIL import Image, ImageOps

    image = Image.open(path)

    try:
        if image.getexif().get(EXIF_ORIENTATION, 1) != 1:
            rotated = ImageOps.exif_transpose(image)
            if rotated is not image:
                image.close()
                image = rotated
    except Exception:
        pass

    if image.mode == "RGB" and max_width <= 0:
        return image

    if image.mode in ("RGBA", "LA", "PA") or (
        image.mode == "P" and "transparency" in image.info
    ):
        rgba = image.convert("RGBA")
        canvas = Image.new("RGB", rgba.size, (255, 255, 255))
        canvas.paste(rgba, mask=rgba.split()[-1])
        rgba.close()
        if image is not rgba:
            image.close()
        page = canvas
    elif image.mode != "RGB":
        page = image.convert("RGB")
        image.close()
    else:
        page = image

    if max_width and page.width > max_width:
        ratio = max_width / float(page.width)
        resized = page.resize((max_width, max(1, int(round(page.height * ratio)))), Image.LANCZOS)
        page.close()
        page = resized

    return page


def delete_images(images: List[Path]) -> int:
    removed = 0
    for path in images:
        try:
            path.unlink()
            removed += 1
        except OSError:
            pass
    return removed


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Merge a JM album folder into one PDF.")
    parser.add_argument("--id", required=True, help="JM 相册 id，例如 350234")
    parser.add_argument(
        "--root",
        default=os.environ.get("JM_DOWNLOAD_ROOT", DEFAULT_ROOT),
        help="库根目录，默认 %s" % DEFAULT_ROOT,
    )
    parser.add_argument("--resolution", type=float, default=100.0, help="PDF DPI，默认 100")
    parser.add_argument("--max-width", type=int, default=0, help="超过该宽度先等比缩小，0 表示不缩")
    parser.add_argument("--force", action="store_true", help="PDF 已存在时覆盖")
    parser.add_argument("--list", action="store_true", help="只打印排好序的图片清单")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--clean", action="store_true", help="合并成功后删除原图")
    group.add_argument("--clean-only", action="store_true", help="只删原图，不合并")
    args = parser.parse_args(argv)

    folder = Path(args.root).expanduser() / str(args.id)
    pdf_path = folder / ("JM_%s.pdf" % args.id)

    if not folder.is_dir():
        return _fail("相册目录不存在: %s" % folder)

    if args.clean_only:
        images = collect_images(folder)
        if not images:
            return _fail("目录里没有图片可清理: %s" % folder)
        removed = delete_images(images)
        _emit(
            {
                "folder": str(folder),
                "removed": removed,
                "pdf": str(pdf_path) if pdf_path.exists() else None,
            }
        )
        return 0

    images = collect_images(folder)

    if args.list:
        _emit(
            {
                "folder": str(folder),
                "count": len(images),
                "images": [path.relative_to(folder).as_posix() for path in images],
            }
        )
        return 0

    if not images:
        return _fail("目录里没有图片: %s" % folder)

    if pdf_path.exists() and not args.force:
        _emit(
            {
                "skipped": True,
                "reason": "PDF 已存在，需要重新生成请加 --force",
                "pdf": str(pdf_path),
                "images": len(images),
            }
        )
        return 0

    pages = []
    tmp_path = None
    try:
        for path in images:
            pages.append(_open_page(path, args.max_width))
        if not pages:
            return _fail("没有可合并的图片")

        fd, tmp_name = tempfile.mkstemp(prefix=".merge-", suffix=".pdf", dir=str(folder))
        os.close(fd)
        tmp_path = Path(tmp_name)
        pages[0].save(
            tmp_path,
            "PDF",
            resolution=args.resolution,
            save_all=True,
            append_images=pages[1:],
        )
        os.replace(str(tmp_path), str(pdf_path))
        tmp_path = None
    finally:
        for page in pages:
            try:
                page.close()
            except Exception:
                pass
        if tmp_path is not None and tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass

    removed = delete_images(images) if args.clean else 0
    _emit(
        {
            "pdf": str(pdf_path),
            "pages": len(pages),
            "images": len(images),
            "bytes": pdf_path.stat().st_size,
            "removed": removed,
        }
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

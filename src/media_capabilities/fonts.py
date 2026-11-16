"""Shared local font discovery and factual metadata for image/video lettering."""
from functools import lru_cache
import hashlib
from pathlib import Path

from PIL import ImageFont


from .catalogs import catalog_info, read_catalog

REFERENCES = read_catalog("fonts")["references"]
SYSTEM_FONTS = tuple(read_catalog("fonts")["system_fonts"])

def discover(directory, explicit=None):
    candidates = [Path(explicit)] if explicit else []
    project = Path(directory)
    if project.is_dir():
        candidates += sorted(path for path in project.rglob("*")
                             if path.suffix.lower() in {".ttf", ".otf", ".ttc"}
                             and not any(p.is_symlink() for p in (path, *path.parents)))
    candidates += [Path(path) for path in SYSTEM_FONTS]
    result = {}
    for path in candidates:
        if not path.is_file() or path.is_symlink():
            continue
        resolved = str(path.resolve())
        if resolved in result.values():
            continue
        key = path.name
        if key in result:
            key = path.stem[:95] + "-" + hashlib.sha256(resolved.encode()).hexdigest()[:12] + path.suffix
        result[key] = resolved
    if result:
        result["default"] = next(iter(result.values()))
    return result


@lru_cache(maxsize=256)
def _metadata(path, modified_ns, size):
    del modified_ns, size
    try:
        font = ImageFont.truetype(path, 24)
        family, face = font.getname()
        return {"family": family, "face": face}
    except (OSError, ValueError):
        return {"family": None, "face": None, "decode_error": "当前渲染器不能读取此字体"}


def supports(font, character):
    missing, glyph = font.getmask(chr(0x10FFFF)), font.getmask(character)
    return character.isspace() or (missing.size, bytes(missing)) != (glyph.size, bytes(glyph))


def missing_characters(path, text):
    font = ImageFont.truetype(str(path), 24)
    return [character for character in sorted(set(text)) if not supports(font, character)]


def catalog(fonts, text=""):
    entries = []
    for key, value in fonts.items():
        path = Path(value)
        stat = path.stat()
        entry = {"font_id": key, "path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                 **_metadata(str(path), stat.st_mtime_ns, stat.st_size),
                 "license_status": "local_file_not_a_license_receipt"}
        if text and not entry.get("decode_error"):
            entry["missing_characters"] = missing_characters(path, text)
        entries.append(entry)
    return {
        "installed": entries, "references": read_catalog("fonts")["references"],
        "reference_catalog": catalog_info("fonts"),
        "instructions": (
            "只能选择installed里的font_id；references是官方获取渠道，不代表本地已安装或当前文件已核授权。"
            "先按目标语言和实际译文验字，再匹配原字形、字重、字宽；无法确定原字体时明确视觉近似。"
            "字体文件与描边、阴影、排版和动画分开。原字体不支持译文时选对应语言的视觉近似，"
            "不伪造字体身份、不使用缺字方框。TTC当前只使用第0字面，不宣称整集合均已支持。"),
    }

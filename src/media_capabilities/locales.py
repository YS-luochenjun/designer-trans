"""Locale normalization independent of translation workflows."""
import re

from .catalogs import read_catalog

GROUPS = {entry["locale"]: " ".join(entry["aliases"])
          for entry in read_catalog("locales")["entries"]}
ALIASES = {alias.casefold(): code for code, aliases in GROUPS.items() for alias in aliases.split()}


def normalize_locale(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError("语言不能为空")
    value = value.strip().replace("_", "-")
    if "-" not in value and value.casefold() in ALIASES:
        return ALIASES[value.casefold()]
    if not re.fullmatch(r"[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*", value):
        raise ValueError(f"无法识别语言：{value}；请使用语言名或规范标签")
    parts = value.split("-")
    parts[0] = ALIASES.get(parts[0].casefold(), parts[0].lower())
    return "-".join([parts[0], *[
        part.upper() if len(part) == 2 else part.title() if len(part) == 4 else part
        for part in parts[1:]
    ]])


def normalize_locales(values):
    return list(dict.fromkeys(normalize_locale(value) for value in values))

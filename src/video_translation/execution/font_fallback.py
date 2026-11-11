"""Render mixed-script lettering with explicit, glyph-checked fallback fonts."""
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


def supports(font, character):
    missing, glyph = font.getmask(chr(0x10FFFF)), font.getmask(character)
    return character.isspace() or (missing.size, bytes(missing)) != (glyph.size, bytes(glyph))


def render_fallback(text, output, size, primary, fallbacks, style):
    fonts = [ImageFont.truetype(str(path), style["font_size"]) for path in [primary, *fallbacks]]
    paths = [str(path) for path in [primary, *fallbacks]]
    measure = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    lines, used = [], set()
    stroke = style.get("outline_width", 0)
    shadow_x, shadow_y = style.get("shadow_x", 0), style.get("shadow_y", 0)
    for line in text.split("\n"):
        runs = []
        for character in line:
            index = next((i for i, font in enumerate(fonts) if supports(font, character)), None)
            if index is None:
                raise ValueError("Configured fonts lack a required glyph")
            used.add(index)
            if runs and runs[-1][0] == index:
                runs[-1] = (index, runs[-1][1] + character)
            else:
                runs.append((index, character))
        cursor, left, top, right, bottom = 0.0, 0.0, 0.0, 0.0, 0.0
        positioned = []
        for index, value in runs:
            bounds = measure.textbbox((cursor, 0), value, font=fonts[index], anchor="ls", stroke_width=stroke)
            left, top = min(left, bounds[0]), min(top, bounds[1])
            right, bottom = max(right, bounds[2]), max(bottom, bounds[3])
            positioned.append((index, value, cursor))
            cursor += measure.textlength(value, font=fonts[index])
        left += min(0, shadow_x)
        right += max(0, shadow_x)
        top += min(0, shadow_y)
        bottom += max(0, shadow_y)
        lines.append((positioned, left, top, right, max(bottom - top, style["font_size"])))
    height = sum(line[4] for line in lines) + max(0, len(lines) - 1) * 4
    if height > size[1] or any(right - left > size[0] for _, left, _, right, _ in lines):
        raise ValueError("Text does not fit its region; choose a smaller font or another layout")
    image = Image.new("RGBA", size)
    draw, y = ImageDraw.Draw(image), 0
    for runs, left, top, right, line_height in lines:
        width = right - left
        offset = {"left": 0, "center": (size[0] - width) / 2, "right": size[0] - width}[style.get("align", "center")]
        for index, value, x in runs:
            origin = (offset - left + x, y - top)
            options = {"font": fonts[index], "anchor": "ls", "stroke_width": stroke}
            if shadow_x or shadow_y:
                draw.text((origin[0] + shadow_x, origin[1] + shadow_y), value,
                          fill=style.get("shadow_color", "#000000"), **options)
            draw.text(origin, value, fill=style.get("color", "#FFFFFF"),
                      stroke_fill=style.get("outline_color", "#000000"), **options)
        y += line_height + 4
    image.save(Path(output))
    return [paths[i] for i in sorted(used) if i]

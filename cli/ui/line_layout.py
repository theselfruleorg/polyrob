"""Single-row terminal text measured in display cells, including wide Unicode."""
from prompt_toolkit.utils import get_cwidth


def fit_fragments(fragments, width):
    """Preserve styles and append an ellipsis when a single row is clipped."""
    width = max(0, width)
    clean = [(style, "".join(
        " " if char in "\n\r\t" else char
        for char in text if char in "\n\r\t" or not (ord(char) < 32 or 127 <= ord(char) < 160)
    )) for style, text in fragments]
    if sum(get_cwidth(text) for _, text in clean) <= width:
        return clean
    if not width:
        return []
    result = []
    remaining = width - 1
    for style, text in clean:
        part = ""
        for char in text:
            cells = get_cwidth(char)
            if cells > remaining:
                if part:
                    result.append((style, part))
                result.append((style, "…"))
                return result
            part += char
            remaining -= cells
        result.append((style, part))
    return result

"""Strict boundaries between response content and leaked tool-call syntax."""
import json


def structured_prefix(text: str) -> bool:
    """Only a leading brain JSON object may precede a leaked call.

    Prose, Markdown quotes and code examples are content, never tool syntax.
    """
    text = text.strip()
    if not text:
        return True
    try:
        obj, end = json.JSONDecoder().raw_decode(text)
        return isinstance(obj, dict) and not text[end:].strip()
    except ValueError:
        return False



def textual_envelope(content: str, found: list) -> bool:
    prefix = content[:found[0][0]].replace("<function_calls>", "")
    remainder = "".join(content[a[1]:b[0]] for a, b in zip(found, found[1:]))
    remainder += content[found[-1][1]:]
    return structured_prefix(prefix) and not remainder.replace("</function_calls>", "").strip()

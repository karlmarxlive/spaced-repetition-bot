"""Telegram message size limits for transactional delivery."""

def text_parts(text, limit=4096):
    """Preserve every character; count UTF-16 units conservatively for Telegram."""
    part, units = [], 0
    for char in text:
        size = 2 if ord(char) > 0xFFFF else 1
        if units + size > limit:
            yield "".join(part)
            part, units = [], 0
        part.append(char)
        units += size
    if part:
        yield "".join(part)

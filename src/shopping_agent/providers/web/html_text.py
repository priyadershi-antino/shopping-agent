"""Readable text from HTML using only the standard library."""

import re
from html.parser import HTMLParser

_SKIP = {
    "script",
    "style",
    "noscript",
    "svg",
    "nav",
    "footer",
    "header",
    "form",
    "iframe",
    "template",
}
_BLOCK = {
    "p", "div", "section", "article", "li", "ul", "ol", "tr", "table", "br", "h1", "h2", "h3",
    "h4", "h5", "h6", "dt", "dd", "td", "th",
}  # fmt: skip


class _Extractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.title = ""
        self._skip_depth = 0
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _SKIP:
            self._skip_depth += 1
        elif tag == "title":
            self._in_title = True
        elif tag in _BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP and self._skip_depth:
            self._skip_depth -= 1
        elif tag == "title":
            self._in_title = False
        elif tag in _BLOCK:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title += data
        elif not self._skip_depth:
            self.parts.append(data)


def html_to_text(html: str) -> tuple[str, str]:
    """Return (title, text)."""
    parser = _Extractor()
    parser.feed(html)
    parser.close()
    text = "".join(parser.parts)
    lines = [re.sub(r"[ \t\r\f\v]+", " ", line).strip() for line in text.split("\n")]
    text = "\n".join(line for line in lines if line)
    return re.sub(r"\s+", " ", parser.title).strip(), text

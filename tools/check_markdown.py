"""Validate the restricted Markdown subset accepted by catalog descriptions."""

import re
from collections.abc import Callable
from html.parser import HTMLParser
from urllib.parse import urlsplit

from markdown import Markdown
from markdown.extensions import Extension

ALLOWED_TAGS = frozenset({"a", "br", "em", "h1", "h2", "li", "ol", "p", "strong", "ul"})
ALLOWED_LINK_SCHEMES = frozenset({"http", "https"})


class MarkdownViolation(ValueError):
    """Represent one disallowed Markdown construct and its source line."""

    def __init__(self, element: str, line: int) -> None:
        """
        Store the construct name and a one-based source line number.

        The exception message is ready for a CI annotation or PR comment.
        """
        self.element = element
        self.line = line
        super().__init__(f"line {line}: Markdown element '{element}' is not allowed")


REJECTED_INLINE = (
    "backtick",
    "image_link",
    "image_reference",
    "short_image_ref",
    "html",
    "entity",
    "automail",
)
REJECTED_BLOCKS = (
    ("setextheader", "Setext header"),
    ("code", "Code block"),
    ("hr", "Horizontal line"),
    ("quote", "Quote"),
)


class BasicFormattingExtension(Extension):
    """Reject Markdown features outside headings, emphasis, lists, and links."""

    def __init__(self, source: str) -> None:
        """
        Keep the original text so parser offsets can map back to source lines.

        The parser instance is local to a single description or changelog.
        """
        self.source = source
        super().__init__()

    def source_line(self, fragment: str, offset: int = 0) -> int:
        """
        Map parser text back to its source line, falling back to line one.

        Markdown block parsing may trim indentation before invoking processors.
        """
        start = self.source.find(fragment) if fragment else -1
        position = max(0, start + offset) if start >= 0 else 0
        return self.source.count("\n", 0, position) + 1

    def reject_inline(self, element: str) -> Callable[..., None]:
        """
        Create an inline processor handler that reports a source location.

        The handler is installed only on this Markdown parser instance.
        """

        def handle_match(match, data):
            """
            Fail when the Markdown parser recognizes a forbidden inline token.

            The matched token offset is translated to the original input line.
            """
            raise MarkdownViolation(
                element,
                self.source_line(data, match.start()),
            )

        return handle_match

    def reject_block(self, element: str) -> Callable[..., None]:
        """
        Create a block processor that reports the first source line in its block.

        This handler replaces only the selected Markdown processor instance.
        """

        def run(parent, blocks):
            """
            Fail when the Markdown parser recognizes a forbidden block token.

            The first block line is mapped back to its position in the source.
            """
            lines = blocks[0].splitlines()
            patterns = {
                "Code block": lambda line: line.startswith(("    ", "\t")),
                "Horizontal line": lambda line: bool(
                    re.fullmatch(r"(?:[-*_]\s*){3,}", line.strip())
                ),
                "Quote": lambda line: line.lstrip().startswith(">"),
                "Setext header": lambda line: bool(
                    re.fullmatch(r"\s*(?:=+|-+)\s*", line)
                ),
            }
            marker = patterns.get(element)
            source_fragment = (
                next((line for line in lines if marker and marker(line)), lines[0])
                if lines
                else ""
            )
            raise MarkdownViolation(element, self.source_line(source_fragment))

        return run

    def limit_header_depth(self, md) -> None:
        """
        Reject headings deeper than H2, while retaining allowed headings.

        H1 and H2 continue through the original Markdown processor.
        """
        header = md.parser.blockprocessors["hashheader"]
        original_header = header.run

        def run_header(parent, blocks):
            match = header.RE.match(blocks[0])
            if match and len(match.group("level")) > 2:
                raise MarkdownViolation("Header depth", self.source_line(blocks[0]))
            return original_header(parent, blocks)

        header.run = run_header

    def reject_reference_definitions(self, md) -> None:
        """
        Reject reference definitions and leave ordinary link parsing intact.

        Explicit inline Markdown links remain available to authors.
        """
        reference = md.parser.blockprocessors["reference"]
        original_reference = reference.run

        def reject_reference(parent, blocks):
            block = blocks[0]
            result = original_reference(parent, blocks)
            if result:
                raise MarkdownViolation("Reference link", self.source_line(block))
            return False

        reference.run = reject_reference

    def extendMarkdown(self, md) -> None:
        """
        Install the allowlist without changing Python-Markdown global state.

        Inline and block processors reject unsupported constructs during parsing.
        """
        md.preprocessors["html_block"].run = lambda lines: lines
        for token in REJECTED_INLINE:
            md.inlinePatterns[token].handleMatch = self.reject_inline(token)
        self.limit_header_depth(md)
        for token, label in REJECTED_BLOCKS:
            md.parser.blockprocessors[token].run = self.reject_block(label)
        self.reject_reference_definitions(md)


class RenderedMarkdownPolicy(HTMLParser):
    """Check rendered tags and links after the Markdown parser applies its grammar."""

    def __init__(self) -> None:
        """
        Prepare a parser that records the first disallowed rendered element.

        Generated tags are checked after Markdown syntax has been interpreted.
        """
        super().__init__(convert_charrefs=True)
        self.violation: tuple[str, int] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        """
        Reject unsupported output tags, attributes, or non-HTTP links.

        Only explicit HTTP(S) anchors and the basic formatting tags are accepted.
        """
        line = self.getpos()[0]
        attributes = dict(attrs)
        if self.violation is not None:
            return
        if tag not in ALLOWED_TAGS:
            element = (
                "Header depth" if tag.startswith("h") and tag[1:].isdigit() else tag
            )
            self.violation = (element, line)
            return
        if tag == "a":
            href = attributes.get("href")
            parsed = urlsplit(href or "")
            if (
                set(attributes) - {"href", "title"}
                or parsed.scheme not in ALLOWED_LINK_SCHEMES
                or not parsed.netloc
            ):
                self.violation = ("link URL", line)
        elif attributes:
            self.violation = (f"{tag} attributes", line)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        """
        Apply the same element and link checks to self-closing output tags.

        This prevents self-closing image and HTML elements from bypassing checks.
        """
        self.handle_starttag(tag, attrs)


def validate_markdown(value: str) -> list[str]:
    """
    Return line-specific violations for unsupported catalog Markdown.

    The Markdown library parses syntax; a second pass restricts rendered tags.
    """
    extension = BasicFormattingExtension(value)
    try:
        rendered = Markdown(extensions=[extension]).convert(value)
    except MarkdownViolation as exc:
        return [str(exc)]
    policy = RenderedMarkdownPolicy()
    policy.feed(rendered)
    if policy.violation is None:
        return []
    element, line = policy.violation
    return [f"line {line}: Markdown element '{element}' is not allowed"]

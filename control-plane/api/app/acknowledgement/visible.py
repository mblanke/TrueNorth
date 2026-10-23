"""Does a rules-of-use text show anything to read once rendered?

A version that renders blank (spaces, an HTML comment, ``---``, an image only) would leave
"I agree" with nothing to agree to, and every user, the platform administrator included,
stuck behind it. Drafts and publishes are refused unless at least one letter or digit is
left once Markdown and HTML markup is stripped. Deliberately conservative: a false
"blank" only asks the author for some words.
"""

from __future__ import annotations

import re

_COMMENT = re.compile(r"<!--.*?(?:-->|$)", re.S)
_TAG = re.compile(r"<[^>]*>")
_ENTITY = re.compile(r"&#?\w+;")
_IMAGE = re.compile(r"!\[[^\]]*\](?:\([^)]*\)|\[[^\]]*\])")
_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_LINK_DEF = re.compile(r"^\s{0,3}\[[^\]]+\]:.*$", re.M)
_WORD = re.compile(r"[^\W_]")


def has_visible_text(markdown: str | None) -> bool:
    text = markdown or ""
    for pattern in (_COMMENT, _TAG, _ENTITY, _IMAGE, _LINK_DEF):
        text = pattern.sub(" ", text)
    text = _LINK.sub(r"\1", text)
    return _WORD.search(text) is not None

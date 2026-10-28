"""The content port (owner decision 2026-10-27, v1.2.0 GA; docs/content-port.md).

Courses, range templates and scenarios move from the test site to a new site **only once
proven**: a range built from them reached Certified (ADR 0013 §6), a class or exercise using
them finished with passing evidence, and the content clock does not call them stale (ADR
0014 §22-25). ``proven.py`` computes that; ``bundle.py`` writes and reads the signed bundle
(``signing.py``: Ed25519, its own key and context); ``importer.py`` previews and applies it
on the new site, where everything lands **not yet proven**. Golden images are ported their
own way (OVF export plus build record, S2 promotion); a bundle only names the builds it needs.

Importing this package registers its tables on ``Base.metadata``.
"""

from .models import ContentPortImport, ContentPortItem

__all__ = ["ContentPortImport", "ContentPortItem"]

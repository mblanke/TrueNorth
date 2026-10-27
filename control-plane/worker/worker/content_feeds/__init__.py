"""The content feeds (ADR 0014 decision 25, issue #287): release, end-of-life and
vulnerability data that make content due for review.

One pipeline, two ways in:

* **Online** (``TN_FEEDS_MODE=online``): ``pull.pull_content_feeds``, a daily beat task on
  worker-provision (the worker with egress), reads endoflife.date, CISA KEV and the NVD
  CVE API through the feed sources (``feed_backends/``, GET only, through
  ``TN_FEEDS_HTTP_PROXY`` when set) and stores what they say (``store.py``).
* **Air-gapped** (``TN_FEEDS_MODE=bundle``): an admin imports a signed feed bundle (the same
  entries) through the API (``app/content_feeds``), which verifies it and stores them.

Either way the entries land in ``content_feed_entries``. The content clock's sync
(``content_review_sync.py``) then matches them against what each golden template is built
from (``catalogue.py``, ``content/catalogue/feed_products.yaml``; ``match.py``, pure) and
flags it stale; the flag flows down the dependency edges like any other. Each (item, entry)
is flagged once (``content_feed_flags``), so Mark reviewed clears it for good.
"""

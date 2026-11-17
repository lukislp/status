"""The footer's privacy link must open the status page's own section of the lktec.org privacy
policy: the page is served by GitHub Pages, not by Cloudflare like the rest of lktec.org, and only
the #status section describes that."""

import json
import unittest
from pathlib import Path

CONFIG = Path(__file__).resolve().parent.parent / "config.json"


class LegalLinkTests(unittest.TestCase):
    def test_privacy_links_point_at_the_status_section(self):
        site = json.loads(CONFIG.read_text(encoding="utf-8"))["site"]
        self.assertEqual(site["privacy_url"], "https://lktec.org/en/imprint#status")
        self.assertEqual(site["privacy_url_de"], "https://lktec.org/impressum#status")


if __name__ == "__main__":
    unittest.main()

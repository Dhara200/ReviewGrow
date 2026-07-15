import unittest
from xml.etree import ElementTree

from flask import Flask, render_template

from app.routes.seo import seo_bp


class SeoRouteTests(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__, template_folder="../app/templates")
        self.app.config.update(
            TESTING=True,
            SECRET_KEY="test",
            PUBLIC_BASE_URL="https://reviewgrow.in",
        )
        self.app.register_blueprint(seo_bp)

        @self.app.route("/")
        def homepage():
            return render_template(
                "index.html",
                landing_hero_images=[],
                subscription_price=1999,
                original_subscription_price=2999,
            )

        self.client = self.app.test_client()

    def test_sitemap_is_public_valid_xml_with_expected_urls(self):
        response = self.client.get("/sitemap.xml")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "application/xml")
        root = ElementTree.fromstring(response.data)
        namespace = {"sitemap": "http://www.sitemaps.org/schemas/sitemap/0.9"}
        locations = {
            element.text
            for element in root.findall("sitemap:url/sitemap:loc", namespace)
        }

        self.assertEqual(root.tag, "{http://www.sitemaps.org/schemas/sitemap/0.9}urlset")
        self.assertTrue({
            "https://reviewgrow.in/",
            "https://reviewgrow.in/privacy-policy",
            "https://reviewgrow.in/terms-of-service",
            "https://reviewgrow.in/data-deletion",
        }.issubset(locations))

    def test_sitemap_excludes_private_and_development_urls(self):
        content = self.client.get("/sitemap.xml").get_data(as_text=True)

        self.assertNotIn("/dashboard", content)
        self.assertNotIn("/admin", content)
        self.assertNotIn("oauth/callback", content)
        self.assertNotIn("localhost", content)
        self.assertNotIn("127.0.0.1", content)

    def test_robots_is_public_plain_text_and_references_sitemap(self):
        response = self.client.get("/robots.txt")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "text/plain")
        self.assertIn("User-agent: *\nAllow: /", response.get_data(as_text=True))
        self.assertIn(
            "Sitemap: https://reviewgrow.in/sitemap.xml",
            response.get_data(as_text=True),
        )
        self.assertTrue(response.data.endswith(b"\n"))

    def test_footer_sitemap_link_follows_data_deletion(self):
        content = self.client.get("/").get_data(as_text=True)
        data_deletion_link = 'href="/data-deletion">Data Deletion</a>'
        sitemap_link = 'href="/sitemap.xml">Sitemap</a>'

        self.assertIn(data_deletion_link, content)
        self.assertIn(sitemap_link, content)
        self.assertLess(content.index(data_deletion_link), content.index(sitemap_link))


if __name__ == "__main__":
    unittest.main()

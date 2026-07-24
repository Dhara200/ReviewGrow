from xml.etree import ElementTree

from flask import Blueprint, Response, current_app


seo_bp = Blueprint("seo", __name__)

PUBLIC_SITEMAP_PATHS = (
    "/",
    "/pricing",
    "/privacy-policy",
    "/terms-of-service",
    "/data-deletion",
)


def _public_url(path):
    base_url = current_app.config.get(
        "PUBLIC_BASE_URL",
        "https://reviewgrow.in",
    ).rstrip("/")
    return f"{base_url}{path}"


@seo_bp.route("/sitemap.xml")
def sitemap():
    namespace = "http://www.sitemaps.org/schemas/sitemap/0.9"
    ElementTree.register_namespace("", namespace)
    urlset = ElementTree.Element(f"{{{namespace}}}urlset")

    for path in PUBLIC_SITEMAP_PATHS:
        url = ElementTree.SubElement(urlset, f"{{{namespace}}}url")
        location = ElementTree.SubElement(url, f"{{{namespace}}}loc")
        location.text = _public_url(path)

    xml = ElementTree.tostring(
        urlset,
        encoding="utf-8",
        xml_declaration=True,
    )
    return Response(xml, content_type="application/xml; charset=utf-8")


@seo_bp.route("/robots.txt")
def robots():
    content = "\n".join((
        "User-agent: *",
        "Allow: /",
        "",
        f"Sitemap: {_public_url('/sitemap.xml')}",
        "",
    ))
    return Response(content, content_type="text/plain; charset=utf-8")



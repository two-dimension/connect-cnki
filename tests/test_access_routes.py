from types import SimpleNamespace

from cnki_mcp.config import Institution


class Route:
    def __init__(self, url, resource_type, navigation=False):
        self.request = SimpleNamespace(url=url, method="GET", resource_type=resource_type,
                                       is_navigation_request=lambda: navigation)
        self.action = None

    async def fallback(self):
        self.action = "fallback"

    async def abort(self):
        self.action = "abort"


async def test_static_cdn_scope_does_not_allow_navigation_or_data_requests(app):
    profile = Institution(institution_id="fixture", display_names=["Fixture"],
        allowed_hosts=["fixture.example"], provider_hosts=["fixture.example"],
        resource_hosts=["static.fixture.example"], search_url="https://fixture.example/")
    script = Route("https://static.fixture.example/header.js", "script")
    await app.access.route_request("fixture", profile, script)
    assert script.action == "fallback"
    for resource_type, navigation in (("document", True), ("xhr", False), ("fetch", False)):
        route = Route("https://static.fixture.example/endpoint", resource_type, navigation)
        await app.access.route_request("fixture", profile, route)
        assert route.action == "abort"

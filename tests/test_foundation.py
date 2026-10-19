from datetime import UTC, datetime, timedelta

import pytest

from cnki_mcp.config import (
    ClientProfile,
    Institution,
    PolicyRecord,
    Settings,
    provider_https_upgrade,
    public_url,
    validate_url,
)
from cnki_mcp.core import AppError, Store
from cnki_mcp.library import Library
from cnki_mcp.models import PaperInput
from cnki_mcp.policy import PolicyGate, protected


def test_url_boundaries():
    assert validate_url("https://kns.cnki.net/", ["kns.cnki.net"])
    for url in ("https://kns.cnki.net.evil.org/", "http://kns.cnki.net/", "https://user:secret@kns.cnki.net/"):
        with pytest.raises(AppError):
            validate_url(url, ["kns.cnki.net"])
    assert public_url("https://kns.cnki.net/detail?filename=ABC&token=SECRET&SAMLResponse=SECRET#x") == (
        "https://kns.cnki.net/detail?filename=ABC"
    )


def test_human_challenge_return_upgrades_only_known_provider_search_paths():
    profile = Institution(institution_id="fixture", display_names=["Fixture"],
        allowed_hosts=["kns.cnki.net", "idp.fixture.example"], provider_hosts=["kns.cnki.net"],
        search_url="https://kns.cnki.net/kns8s/")
    original = "http://kns.cnki.net/kns8s/?captchaId=PRIVATE_TEST_NONCE&other=a%2Fb%3D"
    assert provider_https_upgrade(original, profile, "GET") == original.replace("http:", "https:", 1)
    assert provider_https_upgrade("http://kns.cnki.net:80/kns8s/", profile, "GET") == "https://kns.cnki.net/kns8s/"
    for url, method in (
        (original, "POST"), (original, "HEAD"), ("http://kns.cnki.net:8080/kns8s/", "GET"),
        ("http://kns.cnki.net.evil.example/kns8s/", "GET"), ("http://idp.fixture.example/login", "GET"),
        ("http://kns.cnki.net/Shibboleth.sso/Login", "GET"), ("http://u:p@kns.cnki.net/kns8s/", "GET"),
        ("http://kns.cnki.net/kns8s-evil/", "GET"),
    ):
        assert provider_https_upgrade(url, profile, method) is None


def test_verified_authentication_get_endpoint_upgrades_without_touching_post_or_callbacks():
    profile = Institution(institution_id="fixture", display_names=["Fixture"], allowed_hosts=["ids.fixture.example"],
        https_navigation_paths={"ids.fixture.example": ["/authserver/login"]})
    url = "http://ids.fixture.example/authserver/login?service=PRIVATE_OPAQUE_SERVICE"
    assert provider_https_upgrade(url, profile, "GET") == url.replace("http:", "https:", 1)
    assert provider_https_upgrade(url, profile, "POST") is None
    assert provider_https_upgrade("http://ids.fixture.example/authserver/callback?ticket=PRIVATE_TICKET", profile, "GET") is None


def test_gate_unknown_denied_expired_and_provider_scope(tmp_path):
    settings = Settings(data_dir=tmp_path, institutions=[Institution(institution_id="school", display_names=["学校"])])
    gate = PolicyGate(settings)
    with pytest.raises(AppError, match="许可尚未核验"):
        gate.check("school", "automated_search")
    settings.institutions[0].policy_records = [PolicyRecord(operation="automated_search", state="denied")]
    with pytest.raises(AppError, match="禁止"):
        gate.check("school", "automated_search")
    settings.institutions[0].policy_records = [PolicyRecord(
        operation="external_model_use", state="allowed", source="test fixture licence", verified_by="tester",
        verified_at=datetime.now(UTC) - timedelta(days=1), expires_at=datetime.now(UTC) + timedelta(days=1),
        resource_types=["journal"], providers=["openai"],
    )]
    settings.client = ClientProfile(location="external", provider="openai", allowed_content=["metadata", "abstract"])
    value = protected("restricted", "abstract", {"institution_id": "school", "resource_type": "journal"})
    assert gate.output(value) == "restricted"
    settings.client.provider = "another"
    assert gate.output(value)["withheld"]
    settings.client.location = "unknown"
    assert gate.output({"nested": [value]})["nested"][0]["withheld"]


def test_user_confirmation_without_invented_expiry_is_bound_to_personal_research(tmp_path):
    policy = PolicyRecord(operation="external_model_use", state="allowed", source="Synthetic user confirmation",
        basis_type="user_confirmation", permitted_purpose="personal_noncommercial_research",
        verified_by="fixture_user", verified_at=datetime.now(UTC) - timedelta(minutes=1),
        resource_types=["*"], providers=["openai"])
    settings = Settings(data_dir=tmp_path, usage_purpose="personal_noncommercial_research",
        client=ClientProfile(location="external", provider="openai", allowed_content=["metadata", "abstract"]),
        institutions=[Institution(institution_id="fixture", display_names=["Fixture"], policy_records=[policy])])
    gate = PolicyGate(settings)
    assert gate.check("fixture", "external_model_use", "journal").expires_at is None
    value = protected("Synthetic permitted abstract", "abstract", {"institution_id": "fixture", "resource_type": "journal"})
    assert gate.output(value) == "Synthetic permitted abstract"
    for purpose in ("commercial", "unspecified"):
        settings.usage_purpose = purpose
        with pytest.raises(AppError) as failure:
            gate.check("fixture", "external_model_use")
        assert failure.value.code == "OPERATION_NOT_PERMITTED"
        assert gate.output(value)["withheld"]


def test_documented_permission_keeps_expiry_requirement():
    with pytest.raises(AppError) as failure:
        PolicyRecord(operation="automated_search", state="allowed", source="Synthetic document",
            verified_by="test", verified_at=datetime.now(UTC), resource_types=["*"])
    assert failure.value.code == "CONFIG_INVALID"


def test_conservative_identity_and_restart(tmp_path):
    store = Store(tmp_path)
    library = Library(store)
    with store.transaction():
        project = store.put("project", {"question": "test"})
        paper = PaperInput(title="A study", authors=["A"], year=2024, doi="https://doi.org/10.1234/TEST")
        first = library.ingest(project["id"], paper, {}, "test")
        repeat = library.ingest(project["id"], paper, {}, "test")
        cleaned = library.ingest(project["id"], paper.model_copy(update={"authors": ["A", ""]}), {}, "test")
        conflict = library.ingest(project["id"], paper.model_copy(update={"authors": ["B"]}), {}, "test")
        store.put("job", {"state": "running"})
    assert repeat["version_id"] == first["version_id"]
    assert cleaned["version_id"] == first["version_id"]
    assert not conflict["matched_existing"] and conflict["duplicate_ids"]
    store.close()
    reopened = Store(tmp_path)
    assert reopened.all("job")[0]["state"] == "waiting_for_user"
    assert len(reopened.papers(project["id"])) == 2
    reopened.close()


def test_data_directory_has_single_owner(tmp_path):
    store = Store(tmp_path)
    with pytest.raises(AppError) as failure:
        Store(tmp_path)
    assert failure.value.code == "DATA_DIR_IN_USE"
    store.close()
    reopened = Store(tmp_path)
    reopened.close()

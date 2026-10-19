from datetime import UTC, datetime, timedelta

import pytest

from cnki_mcp.config import ClientProfile, Institution, PolicyRecord, Settings, public_url, validate_url
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


def test_conservative_identity_and_restart(tmp_path):
    store = Store(tmp_path)
    library = Library(store)
    with store.transaction():
        project = store.put("project", {"question": "test"})
        paper = PaperInput(title="A study", authors=["A"], year=2024, doi="https://doi.org/10.1234/TEST")
        first = library.ingest(project["id"], paper, {}, "test")
        repeat = library.ingest(project["id"], paper, {}, "test")
        conflict = library.ingest(project["id"], paper.model_copy(update={"authors": ["B"]}), {}, "test")
        store.put("job", {"state": "running"})
    assert repeat["version_id"] == first["version_id"]
    assert not conflict["matched_existing"] and conflict["duplicate_ids"]
    store.db.close()
    reopened = Store(tmp_path)
    assert reopened.all("job")[0]["state"] == "waiting_for_user"
    assert len(reopened.papers(project["id"])) == 2

import hashlib
import json
from pathlib import Path

import pytest

from cnki_mcp.citations import parse_records, serialize
from cnki_mcp.models import PaperInput


@pytest.mark.parametrize(("year", "expected"), [
    ("2025", "[1] 张三，李四. 智能电网负荷预测[J]. 测试期刊，2024，12（3）：1-8."),
    ("2015", "[1] 张三, 李四. 智能电网负荷预测[J]. 测试期刊, 2024, 12(3): 1-8."),
])
def test_chinese_journal_golden(year, expected):
    paper = PaperInput(title="智能电网负荷预测", authors=["张三", "李四"], year=2024,
                       source="测试期刊", volume="12", issue="3", pages="1-8").model_dump()
    paper["id"] = "version_fixture"
    folder = Path(__file__).parents[1] / "src" / "cnki_mcp" / "styles"
    actual, _ = serialize([paper], "text", folder / f"gbt-7714-{year}.csl")
    assert actual == expected


@pytest.mark.parametrize("format", ["ris", "bibtex", "csl-json"])
def test_roundtrip_metadata_and_no_invented_degree(format):
    paper = PaperInput(title="Synthetic thesis", authors=["Fixture Author"], year=2023,
                       paper_type="thesis", source="Fixture University").model_dump()
    paper["id"] = "version_fixture"
    text, _ = serialize([paper], format)
    assert "phdthesis" not in text and "doctoral" not in text
    parsed = parse_records(text, format)
    assert parsed[0].title == paper["title"] and parsed[0].paper_type == "thesis"
    assert parsed[0].doi is None


def test_vendored_style_integrity():
    folder = Path(__file__).parents[1] / "src" / "cnki_mcp" / "styles"
    sources = json.loads((folder / "SOURCES.json").read_text(encoding="utf-8"))
    for source in sources:
        assert hashlib.sha256((folder / source["file"]).read_bytes()).hexdigest() == source["sha256"]


@pytest.mark.parametrize(("start", "end", "expected"), [
    ("101", "109", "101-109"), ("101", "101", "101"), ("101", None, "101"),
    ("101-109", "109", "101-109"), ("S1–S9", "S9", "S1–S9"),
    (None, "109", "109"), (None, None, None),
])
def test_ris_start_end_pages_survive_all_exports(start, end, expected):
    text = "TY  - JOUR\nTI  - Synthetic page range\nAU  - Fixture Author\nPY  - 2024\nJO  - Fixture Journal\n"
    if start:
        text += f"SP  - {start}\n"
    if end:
        text += f"EP  - {end}\n"
    imported = parse_records(text + "ER  - \n", "ris")[0]
    assert imported.pages == expected
    record = {**imported.model_dump(), "id": "version_range"}
    for format in ("ris", "csl-json"):
        exported, _ = serialize([record], format)
        assert parse_records(exported, format)[0].pages == expected
    if expected:
        style = Path(__file__).parents[1] / "src/cnki_mcp/styles/gbt-7714-2025.csl"
        exported, _ = serialize([record], "text", style)
        assert expected.replace("–", "-").replace("—", "-") in exported


async def test_external_crossref_export_survives_local_import_and_all_page_exports(app, rights):
    fixture = Path(__file__).parent / "fixtures/crossref-shannon.ris"
    source = json.loads(fixture.with_suffix(".source.json").read_text(encoding="utf-8"))
    assert hashlib.sha256(fixture.read_bytes()).hexdigest() == source["sha256"]
    papers = parse_records(fixture.read_text(encoding="utf-8"), "ris")
    assert len(papers) == 1 and papers[0].pages == "379-423"
    assert papers[0].source == "Bell System Technical Journal"
    project = (await app.create_project("External RIS format compatibility only"))["data"]["project_id"]
    imported = await app.import_records(project, papers, rights=rights)
    assert imported["status"] == "succeeded", imported
    version = imported["data"]["imported"][0]["version_id"]
    assert app.store.get(version, "version")["pages"] == "379-423"
    for format in ("ris", "csl-json", "text"):
        result = await app.export_bibliography([version], format)
        assert result["status"] == "succeeded", result
        if format == "text":
            assert "379-423" in result["data"]["content"]
        else:
            assert parse_records(result["data"]["content"], format)[0].pages == "379-423"

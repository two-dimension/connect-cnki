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

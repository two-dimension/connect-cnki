from pathlib import Path

import pytest

from cnki_mcp.app import Application
from cnki_mcp.config import ClientProfile, Settings
from cnki_mcp.models import ImportRights, PaperInput


@pytest.fixture
async def app(tmp_path):
    imports = tmp_path / "imports"
    imports.mkdir()
    application = Application(Settings(
        data_dir=tmp_path / "data", import_roots=[imports],
        client=ClientProfile(location="local", allowed_content=["metadata", "abstract", "fulltext", "review"]),
    ))
    yield application
    await application.shutdown()


@pytest.fixture
def paper():
    return PaperInput(title="Fixture power prediction", authors=["Test Author"], year=2024,
                      source="Fixture Journal", doi="10.1234/fixture", volume="1", issue="2", pages="1-2",
                      abstract="PRIVATE_ABSTRACT_fixture")


@pytest.fixture
def rights():
    return ImportRights(basis="Synthetic test fixture created by test suite", allow_local_parse=True,
                        allowed_recipients=["local"])


def make_pdf(path: Path, text: str = "Prediction improved by 12 percent under test conditions."):
    from reportlab.pdfgen.canvas import Canvas

    canvas = Canvas(str(path))
    canvas.drawString(40, 750, text)
    canvas.showPage()
    canvas.drawString(40, 750, "Limitations include synthetic samples and a small experiment.")
    canvas.save()

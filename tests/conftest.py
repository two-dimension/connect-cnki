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


def make_duplicate_key_pdf(path: Path):
    text = b"BT /F1 12 Tf 40 750 Td (Synthetic duplicate dictionary PDF with readable text.) Tj ET"
    objects = [b"<< /Type /Catalog /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length " + str(len(text)).encode() + b" >>\nstream\n" + text + b"\nendstream"]
    body = b"%PDF-1.4\n"
    offsets = [0]
    for index, value in enumerate(objects, 1):
        offsets.append(len(body))
        body += f"{index} 0 obj\n".encode() + value + b"\nendobj\n"
    xref = len(body)
    body += b"xref\n0 6\n0000000000 65535 f \n"
    body += b"".join(f"{offset:010d} 00000 n \n".encode() for offset in offsets[1:])
    body += b"trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n" + str(xref).encode() + b"\n%%EOF\n"
    path.write_bytes(body)

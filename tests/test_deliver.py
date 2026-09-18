"""Delivery routes, offline.

The email route runs against an in-process aiosmtpd; the print route
against a fake IPP responder on a `http.server` thread. Nothing here
touches the network beyond 127.0.0.1.
"""
from __future__ import annotations

import email
import socket
import struct
import threading
from email.message import Message
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest
from aiosmtpd.controller import Controller
from pyipp.enums import IppOperation, IppTag
from pyipp.parser import parse as parse_ipp
from pyipp.serializer import construct_attribute

import deliver as deliver_package
from app.settings import Settings
from deliver import deliver
from deliver import email as email_route
from deliver import printer as printer_route


# ----------------------------------------------------------------- fixtures
@pytest.fixture
def pdf(tmp_path: Path) -> Path:
    """A file that is a PDF as far as any of this cares."""
    p = tmp_path / "2026-09-16.pdf"
    p.write_bytes(b"%PDF-1.7\n% Personal Paper\n%%EOF\n")
    return p


class _Sink:
    """Collects messages handed to the aiosmtpd controller."""

    def __init__(self) -> None:
        self.messages: list[tuple[str, list[str], bytes]] = []

    async def handle_DATA(self, server, session, envelope):  # noqa: N802, ANN001
        self.messages.append(
            (envelope.mail_from, list(envelope.rcpt_tos), envelope.content)
        )
        return "250 Message accepted for delivery"

    def parsed(self, index: int = 0) -> Message:
        return email.message_from_bytes(self.messages[index][2])


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def smtp_server():
    """aiosmtpd in-process; it needs a real port number, not 0."""
    sink = _Sink()
    controller = Controller(sink, hostname="127.0.0.1", port=_free_port())
    controller.start()
    try:
        yield sink, {
            "host": controller.hostname,
            "port": controller.port,
            "user": "ledger@example.com",
            "password": "app-password",  # the fake server offers neither TLS nor AUTH
        }
    finally:
        controller.stop()


# ---- fake IPP printer -------------------------------------------------------
def _ipp_response(status: int, groups: list[tuple[int, list[bytes]]]) -> bytes:
    out = struct.pack(">bb", 2, 0) + struct.pack(">h", status) + struct.pack(">i", 1)
    for tag, attributes in groups:
        out += struct.pack(">b", tag)
        for attribute in attributes:
            out += attribute
    return out + struct.pack(">b", IppTag.END.value)


def _resolution_attribute(name: str, resolutions: list[tuple[int, int]]) -> bytes:
    """An IPP resolution attribute: pyipp's serializer only does text and ints.

    A resolution value is nine octets -- cross-feed, feed, then the units (3
    is dots per inch) -- and repeated values carry an empty name, which is
    how a printer sends a list of them.
    """
    out = b""
    for index, (across, down) in enumerate(resolutions):
        out += struct.pack(">b", IppTag.RESOLUTION.value)
        if index == 0:
            out += struct.pack(">h", len(name)) + name.encode()
        else:
            out += struct.pack(">h", 0)
        out += struct.pack(">h", 9) + struct.pack(">iib", across, down, 3)
    return out


OPERATION_GROUP = (
    IppTag.OPERATION.value,
    [
        construct_attribute("attributes-charset", "utf-8", IppTag.CHARSET),
        construct_attribute("attributes-natural-language", "en-us", IppTag.LANGUAGE),
    ],
)

PRINT_JOB_OK = _ipp_response(
    0x0000,
    [
        OPERATION_GROUP,
        (
            IppTag.JOB.value,
            [
                construct_attribute("job-id", 42, IppTag.INTEGER),
                construct_attribute("job-uri", "ipp://printer/jobs/42", IppTag.URI),
                construct_attribute("job-state", 3, IppTag.ENUM),
            ],
        ),
    ],
)

# A canned Get-Printer-Attributes reply from a Brother HL-L2460DW.
PRINTER_ATTRIBUTES = _ipp_response(
    0x0000,
    [
        OPERATION_GROUP,
        (
            IppTag.PRINTER.value,
            [
                construct_attribute("printer-name", "HL-L2460DW", IppTag.NAME),
                construct_attribute(
                    "printer-make-and-model", "Brother HL-L2460DW series", IppTag.TEXT
                ),
                construct_attribute("printer-state", 3, IppTag.ENUM),
                construct_attribute("printer-state-reasons", "none", IppTag.KEYWORD),
                construct_attribute("printer-up-time", 12345, IppTag.INTEGER),
                construct_attribute(
                    "printer-uri-supported", "ipp://printer/ipp/print", IppTag.URI
                ),
                construct_attribute(
                    "document-format-supported",
                    [
                        "application/octet-stream",
                        "application/pdf",
                        "image/jpeg",
                        "image/urf",
                    ],
                    IppTag.MIME_TYPE,
                ),
                construct_attribute(
                    "sides-supported",
                    ["one-sided", "two-sided-long-edge", "two-sided-short-edge"],
                    IppTag.KEYWORD,
                ),
            ],
        ),
    ],
)


# What the owner's Brother HL-L2460DW actually answers: no PDF anywhere in
# its formats, raster instead.
PRINTER_PWG_ONLY = _ipp_response(
    0x0000,
    [
        OPERATION_GROUP,
        (
            IppTag.PRINTER.value,
            [
                construct_attribute("printer-name", "HL-L2460DW", IppTag.NAME),
                construct_attribute(
                    "printer-make-and-model", "Brother HL-L2460DW series", IppTag.TEXT
                ),
                construct_attribute("printer-state", 3, IppTag.ENUM),
                construct_attribute("printer-state-reasons", "none", IppTag.KEYWORD),
                construct_attribute("printer-up-time", 12345, IppTag.INTEGER),
                construct_attribute(
                    "printer-uri-supported", "ipp://printer/ipp/print", IppTag.URI
                ),
                construct_attribute(
                    "document-format-supported",
                    ["application/octet-stream", "image/urf", "image/pwg-raster"],
                    IppTag.MIME_TYPE,
                ),
                construct_attribute(
                    "sides-supported",
                    ["one-sided", "two-sided-long-edge", "two-sided-short-edge"],
                    IppTag.KEYWORD,
                ),
                _resolution_attribute(
                    "pwg-raster-document-resolution-supported", [(300, 300), (600, 600)]
                ),
                construct_attribute(
                    "pwg-raster-document-type-supported",
                    ["black_1", "sgray_8", "srgb_8"],
                    IppTag.KEYWORD,
                ),
                construct_attribute(
                    "pwg-raster-document-sheet-back", "normal", IppTag.KEYWORD
                ),
            ],
        ),
    ],
)


# A printer that speaks only Apple's raster: nothing the paper can make yet.
PRINTER_URF_ONLY = _ipp_response(
    0x0000,
    [
        OPERATION_GROUP,
        (
            IppTag.PRINTER.value,
            [
                construct_attribute("printer-name", "AirPrinter", IppTag.NAME),
                construct_attribute("printer-make-and-model", "AirPrinter", IppTag.TEXT),
                construct_attribute("printer-state", 3, IppTag.ENUM),
                construct_attribute("printer-state-reasons", "none", IppTag.KEYWORD),
                construct_attribute(
                    "printer-uri-supported", "ipp://printer/ipp/print", IppTag.URI
                ),
                construct_attribute(
                    "document-format-supported",
                    ["application/octet-stream", "image/urf"],
                    IppTag.MIME_TYPE,
                ),
            ],
        ),
    ],
)


class _FakeIPPPrinter:
    """A `http.server` thread that speaks just enough IPP.

    `attributes` is the Get-Printer-Attributes reply, so a test can hand it
    the printer it needs; None means the canned Brother reply above.
    """

    def __init__(self) -> None:
        self.requests: list[tuple[dict, bytes]] = []
        self.jobs: list[tuple[dict, bytes]] = []
        self.attributes: bytes | None = None
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self) -> None:  # noqa: N802
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                outer.requests.append((dict(self.headers), body))

                if self.path != "/ipp/print":
                    self.send_error(404)
                    return

                operation = struct.unpack_from(">h", body, 2)[0]
                if operation == IppOperation.PRINT_JOB.value:
                    outer.jobs.append((dict(self.headers), body))
                    payload = PRINT_JOB_OK
                elif operation == IppOperation.GET_PRINTER_ATTRIBUTES.value:
                    # The global is read here, not captured, so a test may
                    # swap it for another printer's answer.
                    payload = (
                        outer.attributes
                        if outer.attributes is not None
                        else PRINTER_ATTRIBUTES
                    )
                else:
                    self.send_error(400)
                    return

                self.send_response(200)
                self.send_header("Content-Type", "application/ipp")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args) -> None:  # noqa: ANN002
                return

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self) -> "_FakeIPPPrinter":
        self.thread.start()
        return self

    def __exit__(self, *exc) -> None:  # noqa: ANN002
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    @property
    def host(self) -> str:
        host, port = self.server.server_address[:2]
        return f"{host}:{port}"


@pytest.fixture
def fake_printer():
    with _FakeIPPPrinter() as printer:
        yield printer


# --------------------------------------------------------------- email route
def test_send_pdf_attachment_name_and_recipients(pdf, smtp_server):
    sink, smtp = smtp_server
    to = ["reader@example.com", "second@example.com"]

    email_route.send_pdf(
        pdf,
        to,
        "The Evening Ledger, 2026-09-16",
        smtp,
        paper_name="The Evening Ledger",
    )

    assert len(sink.messages) == 1
    mail_from, rcpt_tos, _ = sink.messages[0]
    assert mail_from == "ledger@example.com"
    assert rcpt_tos == to

    message = sink.parsed()
    assert message["Subject"] == "The Evening Ledger, 2026-09-16"
    assert message["To"] == ", ".join(to)

    attachments = [p for p in message.walk() if p.get_filename()]
    assert len(attachments) == 1
    assert attachments[0].get_filename() == "The Evening Ledger 2026-09-16.pdf"
    assert attachments[0].get_content_type() == "application/pdf"
    assert attachments[0].get_payload(decode=True) == pdf.read_bytes()


def test_send_test_reaches_the_recipient(smtp_server):
    sink, smtp = smtp_server

    email_route.send_test(["reader@example.com"], smtp)

    assert len(sink.messages) == 1
    assert sink.messages[0][1] == ["reader@example.com"]
    assert "test" in sink.parsed()["Subject"].lower()


def test_send_pdf_without_smtp_configuration_says_so(pdf):
    with pytest.raises(RuntimeError, match="SMTP not configured in container variables"):
        email_route.send_pdf(pdf, ["reader@example.com"], "subject", None)


def test_attachment_name_falls_back_to_today(tmp_path):
    from datetime import date

    other = tmp_path / "paper.pdf"
    other.write_bytes(b"%PDF-1.7\n")
    assert email_route.attachment_name(other) == f"Personal Paper {date.today()}.pdf"


def test_attachment_name_makes_the_paper_name_safe(pdf):
    # Spaces stay; the characters a filesystem would choke on do not.
    assert (
        email_route.attachment_name(pdf, 'The  Evening/Gull: "Extra"')
        == "The Evening Gull Extra 2026-09-16.pdf"
    )
    assert email_route.attachment_name(pdf, "   ") == "Personal Paper 2026-09-16.pdf"


# --------------------------------------------------------------- print route
def _parse_request(body: bytes) -> dict:
    return parse_ipp(body, contains_data=True)


def test_print_pdf_sends_the_document_duplex(pdf, fake_printer):
    printer_route.print_pdf(pdf, fake_printer.host, duplex=True)

    assert len(fake_printer.jobs) == 1
    headers, body = fake_printer.jobs[0]
    assert headers["Content-Type"] == "application/ipp"

    request = _parse_request(body)
    assert request["version"] == (2, 0)
    assert struct.unpack_from(">h", body, 2)[0] == IppOperation.PRINT_JOB.value

    operation = request["operation-attributes"]
    assert operation["document-format"] == "application/pdf"
    assert operation["job-name"] == "Personal Paper 2026-09-16"

    job = request["jobs"][0]
    assert job["sides"] == "two-sided-long-edge"
    assert job["media"] == "na_letter_8.5x11in"

    assert request["data"] == pdf.read_bytes()


def test_print_pdf_job_name_uses_the_configured_paper_name(pdf, fake_printer):
    printer_route.print_pdf(pdf, fake_printer.host, paper_name="The Evening Ledger")

    _, body = fake_printer.jobs[0]
    operation = _parse_request(body)["operation-attributes"]
    assert operation["job-name"] == "The Evening Ledger 2026-09-16"


def test_print_pdf_single_sided(pdf, fake_printer):
    printer_route.print_pdf(pdf, fake_printer.host, duplex=False)

    _, body = fake_printer.jobs[0]
    assert _parse_request(body)["jobs"][0]["sides"] == "one-sided"


# --------------------------------------------------------- choosing a format
def _real_pdf(path: Path, pages: int) -> Path:
    """A PDF with `pages` US Letter pages, as the render would write it."""
    import pymupdf

    with pymupdf.open() as doc:
        for _ in range(pages):
            doc.new_page(width=612, height=792)
        doc.save(path)
    return path


#: The attributes the owner's Brother answers with, as `pyipp` parses them:
#: no PDF, raster instead, resolutions as (cross-feed, feed, units) triples.
BROTHER = {
    "document-format-supported": [
        "application/octet-stream",
        "image/urf",
        "image/pwg-raster",
    ],
    "pwg-raster-document-resolution-supported": [(300, 300, 3), (600, 600, 3)],
    "pwg-raster-document-type-supported": ["black_1", "sgray_8", "srgb_8"],
    "pwg-raster-document-sheet-back": "normal",
}


def test_pick_format_prefers_pdf_when_the_printer_takes_one():
    document_format, options = printer_route.pick_format(
        {"document-format-supported": ["image/pwg-raster", "application/pdf"]}
    )
    assert document_format == "application/pdf"
    assert options == {}


def test_pick_format_falls_back_to_raster_for_the_brother():
    document_format, options = printer_route.pick_format(BROTHER)

    assert document_format == "image/pwg-raster"
    assert options == {"dpi": 600, "color": "black_1", "sheet_back": "normal"}


def test_pick_format_reads_resolutions_written_as_strings():
    """Some printers send resolutions as text, not as IPP resolution values."""
    attributes = dict(
        BROTHER, **{"pwg-raster-document-resolution-supported": ["300dpi", "600x600dpi"]}
    )
    assert printer_route.pick_format(attributes)[1]["dpi"] == 600

    # A single resolution arrives as one flat triple, not as a list of them.
    one = dict(BROTHER, **{"pwg-raster-document-resolution-supported": (300, 300, 3)})
    assert printer_route.pick_format(one)[1]["dpi"] == 300


def test_pick_format_stays_under_600_dpi():
    finer = dict(
        BROTHER,
        **{"pwg-raster-document-resolution-supported": [(300, 300, 3), (1200, 1200, 3)]},
    )
    assert printer_route.pick_format(finer)[1]["dpi"] == 300


def test_pick_format_defaults_when_the_printer_only_names_the_format():
    document_format, options = printer_route.pick_format(
        {"document-format-supported": "image/pwg-raster"}
    )
    assert document_format == "image/pwg-raster"
    assert options == {"dpi": 300, "color": "sgray_8", "sheet_back": "normal"}


def test_pick_format_refuses_a_printer_that_takes_neither():
    with pytest.raises(RuntimeError) as raised:
        printer_route.pick_format(
            {"document-format-supported": ["image/urf", "application/octet-stream"]}
        )

    said = str(raised.value)
    assert "image/urf" in said  # the formats it did list, named
    assert "not supported yet" in said


def test_print_pdf_sends_pwg_raster_when_the_printer_refuses_pdf(tmp_path, fake_printer):
    """The Brother's job: the same two pages, as raster the printer takes."""
    from deliver import pwg

    fake_printer.attributes = PRINTER_PWG_ONLY
    two = _real_pdf(tmp_path / "2026-09-20.pdf", 2)

    printer_route.print_pdf(two, fake_printer.host, duplex=True)

    _, body = fake_printer.jobs[0]
    request = _parse_request(body)
    assert request["operation-attributes"]["document-format"] == "image/pwg-raster"
    assert request["jobs"][0]["sides"] == "two-sided-long-edge"

    raster = request["data"]
    assert raster[:4] == b"RaS2"
    pages = pwg.decode_pages(raster)
    assert len(pages) == 2
    header = pages[0].header
    assert header.resolution == (600, 600)  # the finest it listed, up to 600
    assert header.bits_per_pixel == 1  # black_1, the first type it listed
    assert header.duplex is True and header.tumble is False
    assert (header.width, header.height) == (5100, 6600)  # US Letter at 600 dpi


def test_print_pdf_still_sends_a_pdf_to_a_pdf_printer(tmp_path, fake_printer):
    two = _real_pdf(tmp_path / "2026-09-21.pdf", 2)

    printer_route.print_pdf(two, fake_printer.host, duplex=True)

    _, body = fake_printer.jobs[0]
    request = _parse_request(body)
    assert request["operation-attributes"]["document-format"] == "application/pdf"
    assert request["data"] == two.read_bytes()


def test_a_one_page_raster_job_is_not_duplex(tmp_path, fake_printer):
    from deliver import pwg

    fake_printer.attributes = PRINTER_PWG_ONLY
    one = _real_pdf(tmp_path / "2026-09-22.pdf", 1)

    printer_route.print_pdf(one, fake_printer.host, duplex=True)

    _, body = fake_printer.jobs[0]
    request = _parse_request(body)
    assert request["jobs"][0]["sides"] == "one-sided"
    assert pwg.decode_pages(request["data"])[0].header.duplex is False


# ------------------------------------------------- the one-page paper
def test_a_one_page_paper_is_printed_one_sided_even_with_duplex_on(tmp_path, fake_printer):
    """The morning with nothing queued: one page, and the job says so."""
    one = _real_pdf(tmp_path / "2026-09-16.pdf", 1)

    printer_route.print_pdf(one, fake_printer.host, duplex=True)

    _, body = fake_printer.jobs[0]
    assert _parse_request(body)["jobs"][0]["sides"] == "one-sided"


def test_a_two_page_paper_is_still_a_duplex_job(tmp_path, fake_printer):
    two = _real_pdf(tmp_path / "2026-09-17.pdf", 2)

    printer_route.print_pdf(two, fake_printer.host, duplex=True)

    _, body = fake_printer.jobs[0]
    assert _parse_request(body)["jobs"][0]["sides"] == "two-sided-long-edge"


def test_the_page_count_the_render_reports_is_the_one_used(tmp_path, fake_printer):
    """`pages` comes from the render; the PDF is not re-counted for it."""
    two = _real_pdf(tmp_path / "2026-09-18.pdf", 2)

    printer_route.print_pdf(two, fake_printer.host, duplex=True, pages=1)

    _, body = fake_printer.jobs[0]
    assert _parse_request(body)["jobs"][0]["sides"] == "one-sided"


def test_a_pdf_that_cannot_be_counted_is_printed_as_asked(pdf, fake_printer):
    """The fixture is not a real PDF: an uncountable paper keeps duplex."""
    assert printer_route.page_count(pdf) is None

    printer_route.print_pdf(pdf, fake_printer.host, duplex=True)

    _, body = fake_printer.jobs[0]
    assert _parse_request(body)["jobs"][0]["sides"] == "two-sided-long-edge"


def test_deliver_passes_the_page_count_to_the_print_route(tmp_path, fake_printer):
    """`run.py` hands `deliver` what the render laid out, and it arrives."""
    one = _real_pdf(tmp_path / "2026-09-19.pdf", 1)
    settings = Settings()
    settings.output.print.enabled = True
    settings.output.print.duplex = True
    settings.output.print.printer_host = fake_printer.host

    assert deliver(one, settings, pages=1) == {"print": None}

    _, body = fake_printer.jobs[0]
    assert _parse_request(body)["jobs"][0]["sides"] == "one-sided"


def test_print_pdf_raises_when_the_printer_rejects_the_job(pdf, fake_printer, monkeypatch):
    rejected = _ipp_response(0x0400, [OPERATION_GROUP])  # ERROR_BAD_REQUEST
    monkeypatch.setitem(globals(), "PRINT_JOB_OK", rejected)

    with pytest.raises(RuntimeError, match="ERROR_BAD_REQUEST"):
        printer_route.print_pdf(pdf, fake_printer.host)


def test_printer_uri_normalises_what_the_settings_page_holds():
    assert printer_route.printer_uri("192.168.1.40") == "ipp://192.168.1.40:631/ipp/print"
    assert printer_route.printer_uri("printer.local:6310") == (
        "ipp://printer.local:6310/ipp/print"
    )
    assert printer_route.printer_uri("ipps://printer.local/ipp/print") == (
        "ipps://printer.local:443/ipp/print"
    )
    with pytest.raises(ValueError):
        printer_route.printer_uri("")


def test_test_printer_parses_get_printer_attributes(fake_printer):
    info = printer_route.test_printer(fake_printer.host)

    assert info.model == "HL-L2460DW series"
    assert "Brother" in info.name
    assert info.state == "idle"
    assert info.accepts_pdf is True
    assert "application/pdf" in info.formats

    _, body = fake_printer.requests[0]
    assert (
        struct.unpack_from(">h", body, 2)[0]
        == IppOperation.GET_PRINTER_ATTRIBUTES.value
    )



# ---------------------------------------------------------------- diagnosis
# A printer that has stopped with an empty paper tray and toner running low.
PRINTER_STOPPED = _ipp_response(
    0x0000,
    [
        OPERATION_GROUP,
        (
            IppTag.PRINTER.value,
            [
                construct_attribute("printer-name", "HL-L2460DW", IppTag.NAME),
                construct_attribute(
                    "printer-make-and-model", "Brother HL-L2460DW series", IppTag.TEXT
                ),
                construct_attribute("printer-state", 5, IppTag.ENUM),
                construct_attribute(
                    "printer-state-reasons",
                    ["media-empty", "marker-supply-low-warning"],
                    IppTag.KEYWORD,
                ),
                construct_attribute("printer-up-time", 12345, IppTag.INTEGER),
                construct_attribute(
                    "printer-uri-supported", "ipp://printer/ipp/print", IppTag.URI
                ),
                construct_attribute(
                    "document-format-supported",
                    ["application/octet-stream", "application/pdf"],
                    IppTag.MIME_TYPE,
                ),
            ],
        ),
    ],
)


def test_diagnose_walks_every_step_when_the_printer_answers(fake_printer):
    diagnosis = printer_route.diagnose(fake_printer.host)

    assert diagnosis.ok is True
    assert [step.name for step in diagnosis.steps] == [
        "resolve", "connect", "ipp", "format", "state"
    ]
    assert all(step.ok for step in diagnosis.steps)
    assert diagnosis.failed is None
    assert diagnosis.info is not None and diagnosis.info.accepts_pdf is True
    assert "HL-L2460DW series" in diagnosis.summary
    assert "idle" in diagnosis.summary and "accepts PDF" in diagnosis.summary

    format_step = next(s for s in diagnosis.steps if s.name == "format")
    assert format_step.detail == "Will send PDF"


def test_diagnose_says_it_will_send_raster_to_a_printer_without_pdf(fake_printer):
    fake_printer.attributes = PRINTER_PWG_ONLY

    diagnosis = printer_route.diagnose(fake_printer.host)

    assert diagnosis.ok is True
    format_step = next(s for s in diagnosis.steps if s.name == "format")
    assert format_step.ok is True
    assert format_step.detail == (
        "Will send PWG Raster at 600 dpi, black_1 "
        "(the printer does not take PDF directly)"
    )
    assert diagnosis.summary.endswith("idle, PWG Raster at 600 dpi")
    assert diagnosis.info is not None and diagnosis.info.accepts_pdf is False


def test_diagnose_stops_at_format_when_the_printer_takes_neither(fake_printer):
    fake_printer.attributes = PRINTER_URF_ONLY

    diagnosis = printer_route.diagnose(fake_printer.host)

    assert diagnosis.ok is False
    step = diagnosis.failed
    assert step is not None and step.name == "format"
    assert "image/urf" in step.detail  # what it did list
    assert step.hint == (
        "This printer only takes formats the paper cannot make yet; tell the "
        "maintainer which ones it listed."
    )
    # The state is never reached: there is nothing to send it.
    assert [s.name for s in diagnosis.steps] == ["resolve", "connect", "ipp", "format"]


def test_diagnose_stops_at_resolve_when_the_name_is_unknown(monkeypatch):
    def unknown(*args, **kwargs):
        raise socket.gaierror("Name or service not known")

    monkeypatch.setattr(printer_route.socket, "getaddrinfo", unknown)

    diagnosis = printer_route.diagnose("brother.local")

    assert diagnosis.ok is False
    assert [step.name for step in diagnosis.steps] == ["resolve"]
    step = diagnosis.failed
    assert step is not None
    assert "Could not resolve 'brother.local'" in step.detail
    assert "IP address" in step.hint
    assert diagnosis.summary == f"resolve: {step.detail}"
    assert diagnosis.info is None


def test_diagnose_stops_at_connect_when_the_port_is_closed():
    host = f"127.0.0.1:{_free_port()}"  # bound, then let go: nothing listens

    diagnosis = printer_route.diagnose(host, timeout=2.0)

    assert diagnosis.ok is False
    assert [step.name for step in diagnosis.steps] == ["resolve", "connect"]
    step = diagnosis.failed
    assert step is not None and step.name == "connect"
    assert "No answer from" in step.detail and "refused" in step.detail
    assert "same network" in step.hint


def test_diagnose_reports_a_stopped_printer_in_words(fake_printer, monkeypatch):
    monkeypatch.setitem(globals(), "PRINTER_ATTRIBUTES", PRINTER_STOPPED)

    diagnosis = printer_route.diagnose(fake_printer.host)

    assert diagnosis.ok is False
    step = diagnosis.failed
    assert step is not None and step.name == "state"
    assert "Stopped." in step.detail
    assert "paper" in step.detail.lower()
    assert "Toner low." in step.detail  # marker-supply-low-warning, in English
    assert "Clear the printer's error" in step.hint
    assert diagnosis.summary.startswith("state: ")
    # The steps before it all passed, so the reader knows how far it got.
    assert [s.name for s in diagnosis.steps] == [
        "resolve", "connect", "ipp", "format", "state"
    ]
    assert diagnosis.info is not None and diagnosis.info.state == "stopped"


def test_diagnose_never_raises_on_a_host_it_cannot_parse():
    diagnosis = printer_route.diagnose("")

    assert diagnosis.ok is False
    assert diagnosis.failed is not None and diagnosis.failed.name == "resolve"


def test_reason_words_keeps_unknown_reasons_readable():
    assert printer_route.reason_words("media-empty") == "Out of paper."
    assert printer_route.reason_words("cover-open") == "Cover open."
    assert printer_route.reason_words("none") == ""
    assert printer_route.reason_words("wumpus-jammed-error") == "Wumpus jammed."


def test_record_printer_check_writes_the_state_file():
    import state

    printer_route.record_printer_check(
        printer_route.Diagnosis(True, [], None, "Brother: idle, accepts PDF")
    )

    check = state.load_state()["printer_check"]
    assert check["ok"] is True
    assert check["summary"] == "Brother: idle, accepts PDF"
    assert check["when"]


def test_print_route_failure_says_what_the_diagnosis_found(pdf, monkeypatch):
    settings = Settings()
    settings.output.print.enabled = True
    settings.output.print.printer_host = "192.168.1.40"

    def boom(*args, **kwargs):
        raise OSError("Connection aborted")

    step = printer_route.Step(
        "connect",
        False,
        "No answer from 192.168.1.40:631 (timed out)",
        "Is the printer on and on the same network as the Unraid box?",
    )
    monkeypatch.setattr(deliver_package, "print_pdf", boom)
    monkeypatch.setattr(
        deliver_package,
        "diagnose",
        lambda host, **kwargs: printer_route.Diagnosis(
            False, [step], None, f"connect: {step.detail}"
        ),
    )

    with pytest.raises(RuntimeError) as raised:
        deliver_package._print_route(pdf, settings, test=False)

    message = str(raised.value)
    assert step.detail in message
    assert step.hint in message
    assert "Connection aborted" in message  # the original error, in parentheses

    # ... and the same sentence reaches the delivery report and the strip.
    assert step.detail in (deliver(pdf, settings)["print"] or "")

    import state

    assert state.load_state()["printer_check"]["summary"] == f"connect: {step.detail}"


def test_print_route_keeps_the_original_error_when_nothing_is_wrong(pdf, monkeypatch):
    settings = Settings()
    settings.output.print.enabled = True
    settings.output.print.printer_host = "192.168.1.40"

    def boom(*args, **kwargs):
        raise RuntimeError("Printer rejected the job: ERROR_BAD_REQUEST")

    monkeypatch.setattr(deliver_package, "print_pdf", boom)
    monkeypatch.setattr(
        deliver_package,
        "diagnose",
        lambda host, **kwargs: printer_route.Diagnosis(True, [], None, "all well"),
    )

    with pytest.raises(RuntimeError, match="ERROR_BAD_REQUEST"):
        deliver_package._print_route(pdf, settings, test=False)


def test_print_route_records_a_successful_print(pdf, fake_printer):
    settings = Settings()
    settings.output.print.enabled = True
    settings.output.print.printer_host = fake_printer.host

    deliver_package._print_route(pdf, settings, test=False)

    import state

    check = state.load_state()["printer_check"]
    assert check["ok"] is True
    assert fake_printer.host in check["summary"]


# ----------------------------------------------------------------- discovery
def test_discover_returns_empty_without_zeroconf(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def no_zeroconf(name, *args, **kwargs):
        if name == "zeroconf":
            raise ImportError("no zeroconf in this container")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_zeroconf)
    assert printer_route.discover(timeout=0.01) == []


def test_discover_returns_empty_when_the_lan_is_unreachable(monkeypatch):
    import zeroconf

    def boom(*args, **kwargs):
        raise OSError("no multicast on a bridge network")

    monkeypatch.setattr(zeroconf, "Zeroconf", boom)
    assert printer_route.discover(timeout=0.01) == []


def test_discover_returns_empty_when_nothing_answers(monkeypatch):
    import zeroconf

    class _NoZeroconf:
        def get_service_info(self, *args, **kwargs):
            return None

        def close(self) -> None:
            return None

    monkeypatch.setattr(zeroconf, "Zeroconf", lambda *a, **k: _NoZeroconf())
    monkeypatch.setattr(zeroconf, "ServiceBrowser", lambda *a, **k: None)
    assert printer_route.discover(timeout=0.01) == []


# ------------------------------------------------------------------- deliver
def test_deliver_runs_only_enabled_routes(pdf, smtp_server, monkeypatch):
    sink, smtp = smtp_server
    monkeypatch.setattr("deliver.Env.smtp", classmethod(lambda cls: smtp))

    settings = Settings()
    settings.output.email.enabled = True
    settings.output.email.to = ["reader@example.com"]

    results = deliver(pdf, settings)

    assert results == {"email": None}
    assert len(sink.messages) == 1


def test_deliver_reports_one_failed_and_one_succeeded_route(pdf, smtp_server, monkeypatch):
    sink, smtp = smtp_server
    monkeypatch.setattr("deliver.Env.smtp", classmethod(lambda cls: smtp))

    settings = Settings()
    settings.output.email.enabled = True
    settings.output.email.to = ["reader@example.com"]
    settings.output.print.enabled = True
    settings.output.print.printer_host = "127.0.0.1:9"  # nothing is listening

    results = deliver(pdf, settings)

    assert set(results) == {"print", "email"}
    assert results["email"] is None
    assert results["print"]  # an error string, not None
    assert len(sink.messages) == 1, "the email route still ran"


def test_deliver_never_raises_and_marks_the_test_subject(pdf, smtp_server, monkeypatch):
    sink, smtp = smtp_server
    monkeypatch.setattr("deliver.Env.smtp", classmethod(lambda cls: smtp))

    settings = Settings()
    settings.output.email.enabled = True
    settings.output.email.to = ["reader@example.com"]

    assert deliver(pdf, settings, test=True) == {"email": None}
    assert sink.parsed()["Subject"] == "[test] Personal Paper, 2026-09-16"


def test_deliver_with_no_routes_enabled_is_empty(pdf):
    assert deliver(pdf, Settings()) == {}


# ------------------------------------------------------------- notification
def test_notify_failure_none_is_silent(monkeypatch):
    from deliver import notify

    settings = Settings()
    notify.notify_failure(settings, "render blew up")  # must not raise


def test_notify_failure_email(smtp_server, monkeypatch):
    from deliver import notify

    sink, smtp = smtp_server
    monkeypatch.setattr("deliver.notify.Env.smtp", classmethod(lambda cls: smtp))

    settings = Settings()
    settings.output.notify = "email"
    settings.output.notify_email = "reader@example.com"

    notify.notify_failure(settings, "the printer was off")

    assert len(sink.messages) == 1
    assert "the printer was off" in sink.messages[0][2].decode()


def test_notify_failure_swallows_errors(monkeypatch):
    from deliver import notify

    monkeypatch.setattr("deliver.notify.Env.smtp", classmethod(lambda cls: None))

    settings = Settings()
    settings.output.notify = "email"
    settings.output.notify_email = "reader@example.com"

    notify.notify_failure(settings, "no smtp anywhere")  # must not raise


def test_notify_failure_unraid_runs_the_script(monkeypatch, tmp_path):
    from deliver import notify

    script = tmp_path / "notify"
    script.write_text("#!/bin/sh\nprintf '%s\\n' \"$@\" > \"$0.args\"\n")
    script.chmod(0o755)
    monkeypatch.setattr(notify, "UNRAID_NOTIFY", script)

    settings = Settings()
    settings.output.notify = "unraid"

    notify.notify_failure(settings, "the paper did not come out")

    args = (tmp_path / "notify.args").read_text().splitlines()
    assert args == [
        "-e",
        "Personal Paper",
        "-s",
        "Paper failed",
        "-d",
        "the paper did not come out",
        "-i",
        "alert",
    ]


def test_notify_failure_unraid_without_the_script_is_silent(monkeypatch, tmp_path):
    from deliver import notify

    monkeypatch.setattr(notify, "UNRAID_NOTIFY", tmp_path / "missing")

    settings = Settings()
    settings.output.notify = "unraid"
    notify.notify_failure(settings, "no unraid here")  # must not raise

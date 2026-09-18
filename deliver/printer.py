"""The print route: direct IPP to an AirPrint-capable printer, no CUPS.

Three parts, each for a reason worth writing down:

* **Attributes** (`test_printer`) go through `pyipp`'s async client, which
  already knows how to ask for and parse `Get-Printer-Attributes`.
* **The format** (`pick_format`) is negotiated from those attributes.
  AirPrint does not oblige a printer to take PDF: the owner's Brother
  HL-L2460DW lists `image/pwg-raster` and `image/urf` and no PDF at all, so
  the paper is rendered to PWG Raster by `deliver/pwg.py` for printers like
  it, and sent as PDF to printers that say they take one.
* **The print job itself** (`print_pdf`) is built here and POSTed with
  `requests`. `pyipp` 0.17 can carry a document body, but its serializer
  looks every attribute name up in `pyipp.tags.ATTRIBUTE_TAG_MAP` and
  *silently drops* the ones it does not know — and `sides` is not in that
  map, so duplex would vanish without a word. So the Print-Job request is
  encoded here, using `pyipp`'s primitives (`construct_attribute` with an
  explicit tag, and `pyipp.parser.parse` for the reply) rather than a
  hand-rolled binary encoder.

IPP is a small binary encoding: version, operation, request id, then tagged
attribute groups, then the document bytes.
"""
from __future__ import annotations

import asyncio
import logging
import random
import re
import socket
import struct
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import requests
from pyipp import IPP
from pyipp.enums import IppOperation, IppStatus, IppTag
from pyipp.models import Printer
from pyipp.parser import parse as parse_ipp
from pyipp.serializer import construct_attribute

from . import pwg
from ._util import DEFAULT_PAPER_NAME, issue_label, slug

_LOGGER = logging.getLogger(__name__)

DEFAULT_PORT = 631
DEFAULT_PATH = "/ipp/print"
IPP_VERSION = (2, 0)
MEDIA = "na_letter_8.5x11in"
SIDES_DUPLEX = "two-sided-long-edge"
SIDES_SIMPLEX = "one-sided"
SERVICE_TYPES = ["_ipp._tcp.local.", "_ipps._tcp.local."]

#: The two document formats the paper can send.
PDF_FORMAT = "application/pdf"
PWG_FORMAT = "image/pwg-raster"

#: Raster defaults, for a printer that lists the format without the details.
DEFAULT_PWG_DPI = 300
#: 600 is as fine as a black-and-white sheet of newsprint needs to be, and
#: four times the bytes of 300 for nothing a reader would see.
MAX_PWG_DPI = 600
DEFAULT_PWG_TYPE = "sgray_8"
#: In order of preference: 1-bit black is what a laser wants, and an eighth
#: of the bytes of 8-bit gray.
PWG_TYPES = ("black_1", "sgray_8")

REQUESTED_ATTRIBUTES = [
    "printer-name",
    "printer-make-and-model",
    "printer-device-id",
    "printer-info",
    "printer-location",
    "printer-state",
    "printer-state-message",
    "printer-state-reasons",
    "printer-up-time",
    "printer-uri-supported",
    "document-format-supported",
    "sides-supported",
    "media-supported",
    "pwg-raster-document-resolution-supported",
    "pwg-raster-document-type-supported",
    "pwg-raster-document-sheet-back",
]


@dataclass
class PrinterInfo:
    """What the Output tab's "Verify connection" button shows."""

    name: str
    model: str
    state: str
    accepts_pdf: bool
    formats: list[str] = field(default_factory=list)


@dataclass
class DiscoveredPrinter:
    """One entry in the printer dropdown."""

    name: str
    host: str
    model: str


# ----------------------------------------------------------- format picking
#: "600dpi", "600x600dpi", "300", and the (x, y, units) triple pyipp parses a
#: RESOLUTION attribute into. Units 3 is dots per inch, 4 dots per cm.
_DPI_RE = re.compile(r"^(\d+)(?:\s*x\s*(\d+))?\s*(dpi|dpcm)?$")
_UNITS_DPCM = 4


def _dpi(value: Any) -> int | None:
    """One resolution, in dots per inch, or None when it makes no sense."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value) or None
    if isinstance(value, (list, tuple)):
        numbers = [v for v in value if isinstance(v, (int, float))]
        if len(numbers) < 2:
            return None
        across = int(numbers[0])
        units = int(numbers[2]) if len(numbers) > 2 else 3
        if units == _UNITS_DPCM:
            across = round(across * 2.54)
        return across or None

    match = _DPI_RE.match(str(value).strip().lower())
    if not match:
        return None
    across = int(match[1])
    if match[3] == "dpcm":
        across = round(across * 2.54)
    return across or None


def _resolutions(value: Any) -> list[int]:
    """Every resolution a printer listed, in dots per inch."""
    if value is None:
        return []
    if isinstance(value, (list, tuple)) and value:
        # One resolution arrives as a flat (x, y, units) triple; several
        # arrive as a list of triples or of strings.
        items = [value] if all(isinstance(v, (int, float)) for v in value) else list(value)
    else:
        items = [value]
    return [dpi for dpi in (_dpi(item) for item in items) if dpi]


def pick_format(attrs: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """What to send this printer, and how to make it.

    PDF when the printer takes one -- it is the paper as it was laid out.
    Otherwise PWG Raster at the finest resolution the printer lists up to
    600 dpi, in the plainest page type it takes. Raises when the printer
    lists neither, naming what it did list.
    """
    formats = [f.strip().lower() for f in _as_list(attrs.get("document-format-supported"))]

    if PDF_FORMAT in formats:
        return PDF_FORMAT, {}

    if PWG_FORMAT in formats:
        offered = _resolutions(attrs.get("pwg-raster-document-resolution-supported"))
        usable = [dpi for dpi in offered if dpi <= MAX_PWG_DPI]
        if usable:
            dpi = max(usable)
        elif offered:
            dpi = min(offered)  # a printer that only offers 1200 gets 1200
        else:
            dpi = DEFAULT_PWG_DPI

        offered_types = _as_list(attrs.get("pwg-raster-document-type-supported"))
        types = [t.strip().lower() for t in offered_types]
        color = next((t for t in PWG_TYPES if t in types), DEFAULT_PWG_TYPE)

        sheet_backs = _as_list(attrs.get("pwg-raster-document-sheet-back"))
        back = next(iter(sheet_backs), "").strip().lower()
        return PWG_FORMAT, {"dpi": dpi, "color": color, "sheet_back": back or "normal"}

    listed = ", ".join(formats) if formats else "no document format at all"
    said = (
        f"The printer lists {listed}; the paper can send "
        f"{PDF_FORMAT} or {PWG_FORMAT}"
    )
    if "image/urf" in formats:
        said += " (image/urf, Apple's raster, is not supported yet)"
    raise RuntimeError(said)


# --------------------------------------------------------------- addressing
def printer_uri(host: str) -> str:
    """Normalise whatever the settings page holds into an `ipp://` URI.

    Accepts `192.168.1.40`, `printer.local:631`, `ipp://host/ipp/print`,
    `ipps://host/ipp/print`.
    """
    host = (host or "").strip()
    if not host:
        raise ValueError("No printer host configured")

    if "://" not in host:
        host = f"ipp://{host}"

    parts = urlsplit(host)
    scheme = parts.scheme if parts.scheme in ("ipp", "ipps") else "ipp"
    if not parts.hostname:
        raise ValueError(f"Cannot parse printer host: {host!r}")

    port = parts.port or (443 if scheme == "ipps" else DEFAULT_PORT)
    path = parts.path or DEFAULT_PATH
    if not path.startswith("/"):
        path = "/" + path

    netloc = f"[{parts.hostname}]" if ":" in parts.hostname else parts.hostname
    return f"{scheme}://{netloc}:{port}{path}"


def _http_url(uri: str) -> str:
    parts = urlsplit(uri)
    scheme = "https" if parts.scheme == "ipps" else "http"
    return f"{scheme}://{parts.netloc}{parts.path}"


# ------------------------------------------------------------- the print job
def _encode_print_job(
    uri: str,
    document: bytes,
    *,
    job_name: str,
    duplex: bool,
    document_format: str = PDF_FORMAT,
) -> bytes:
    """Encode an IPP Print-Job request with the document as the body."""
    request_id = random.randint(1, 0x7FFF)  # noqa: S311  (not a security value)

    out = struct.pack(">bb", *IPP_VERSION)
    out += struct.pack(">h", IppOperation.PRINT_JOB.value)
    out += struct.pack(">i", request_id)

    # Operation attributes. The first three are required, in this order.
    out += struct.pack(">b", IppTag.OPERATION.value)
    out += construct_attribute("attributes-charset", "utf-8", IppTag.CHARSET)
    out += construct_attribute("attributes-natural-language", "en-us", IppTag.LANGUAGE)
    out += construct_attribute("printer-uri", uri, IppTag.URI)
    out += construct_attribute("requesting-user-name", "personal-paper", IppTag.NAME)
    out += construct_attribute("job-name", job_name, IppTag.NAME)
    out += construct_attribute("document-format", document_format, IppTag.MIME_TYPE)

    # Job attributes: the ones that make it a US Letter duplex newspaper.
    out += struct.pack(">b", IppTag.JOB.value)
    out += construct_attribute("copies", 1, IppTag.INTEGER)
    out += construct_attribute("media", MEDIA, IppTag.KEYWORD)
    out += construct_attribute(
        "sides", SIDES_DUPLEX if duplex else SIDES_SIMPLEX, IppTag.KEYWORD
    )
    out += construct_attribute("print-color-mode", "monochrome", IppTag.KEYWORD)

    out += struct.pack(">b", IppTag.END.value)
    return out + document


def page_count(pdf: Path) -> int | None:
    """How many pages the PDF has, or None when it cannot be counted.

    Never raises: the page count decides `sides`, and a paper that cannot be
    counted is still a paper to print.
    """
    try:
        import pymupdf

        with pymupdf.open(pdf) as doc:
            return int(doc.page_count)
    except Exception as exc:  # noqa: BLE001 - an unreadable count is not a failure
        # The reason, as text: neither the exception nor `exc_info` may go
        # into the record, because a log handler that keeps records would
        # then keep this call's frames -- and whatever their callers hold
        # open -- alive with them.
        _LOGGER.warning("could not count the pages of %s (%s)",
                        pdf, f"{type(exc).__name__}: {exc}")
        return None


def print_pdf(
    pdf: Path,
    host: str,
    *,
    duplex: bool = True,
    pages: int | None = None,
    paper_name: str = DEFAULT_PAPER_NAME,
) -> None:
    """Send `pdf` to the printer at `host` over IPP. Raises on failure.

    `pages` is how many pages the PDF has, from the render; when it is not
    given the PDF is counted here. A one-page paper -- the morning with no
    articles -- is sent `sides=one-sided` whatever the duplex setting: most
    printers would do the right thing with it anyway, but the sheet that
    comes out of a duplex queue is the reader's, so it is said explicitly.

    The printer is asked once, up front, what it takes: a PDF printer gets
    the PDF; one that takes only raster gets the same pages encoded as PWG
    Raster (`deliver/pwg.py`).
    """
    pdf = Path(pdf)
    uri = printer_uri(host)
    job_name = f"{slug(paper_name)} {issue_label(pdf)}"

    if pages is None:
        pages = page_count(pdf)
    two_sided = duplex and (pages is None or pages > 1)

    # One Get-Printer-Attributes per job: the answer decides the format and
    # is not worth asking for twice.
    document_format, options = pick_format(asyncio.run(_fetch_attributes(uri)))
    if document_format == PWG_FORMAT:
        data = pwg.encode(
            pdf,
            dpi=options["dpi"],
            color=options["color"],
            duplex=two_sided,
            tumble=False,  # `sides` is two-sided-*long*-edge
            sheet_back=options["sheet_back"],
        )
        made = f"PWG Raster at {options['dpi']} dpi, {options['color']}"
    else:
        data = pdf.read_bytes()
        made = "PDF"

    body = _encode_print_job(
        uri, data, job_name=job_name, duplex=two_sided, document_format=document_format
    )

    _LOGGER.info(
        "printing %s as %s (%d bytes, %s page(s)) to %s, %s",
        pdf.name,
        made,
        len(data),
        pages if pages is not None else "?",
        uri,
        "duplex" if two_sided else "single-sided",
    )

    # trust_env=False: the printer is on the LAN, and a proxy variable in the
    # container's environment must not be consulted for it.
    with requests.Session() as session:
        session.trust_env = False
        response = session.post(
            _http_url(uri),
            data=body,
            headers={
                "Content-Type": "application/ipp",
                "Accept": "application/ipp",
                "User-Agent": "PersonalPaper/1.0",
            },
            timeout=60,
            # Printers carry self-signed certificates; pyipp does the same.
            verify=urlsplit(uri).scheme != "ipps",
        )
        response.raise_for_status()
        content = response.content

    parsed = parse_ipp(content)
    status = parsed.get("status-code", -1)
    if status not in range(0x200):
        try:
            name = IppStatus(status).name
        except ValueError:
            name = hex(status)
        raise RuntimeError(f"Printer rejected the job: {name}")

    job = next(iter(parsed.get("jobs") or []), {})
    _LOGGER.info(
        "print job accepted: job-id=%s state=%s", job.get("job-id"), job.get("job-state")
    )


# ---------------------------------------------------------------- attributes
async def _fetch_attributes(uri: str, *, timeout: float = 10.0) -> dict[str, Any]:
    async with IPP(host=uri, request_timeout=timeout) as ipp:
        response = await ipp.execute(
            IppOperation.GET_PRINTER_ATTRIBUTES,
            {"operation-attributes-tag": {"requested-attributes": REQUESTED_ATTRIBUTES}},
        )
    return next(iter(response.get("printers") or []), {})


def test_printer(host: str) -> PrinterInfo:
    """Query the printer and report what the Output tab needs to show."""
    uri = printer_uri(host)
    attributes = asyncio.run(_fetch_attributes(uri))
    if not attributes:
        raise RuntimeError(f"No printer attributes returned by {uri}")

    printer = Printer.from_dict(attributes)

    formats = attributes.get("document-format-supported") or []
    if isinstance(formats, str):
        formats = [formats]
    formats = [str(f) for f in formats]

    info = PrinterInfo(
        name=printer.info.name,
        model=printer.info.model or printer.info.name,
        state=str(printer.state.printer_state),
        accepts_pdf="application/pdf" in formats,
        formats=formats,
    )
    _LOGGER.info("printer %s: %s, pdf=%s", uri, info.state, info.accepts_pdf)
    return info



# ---------------------------------------------------------------- diagnosis
@dataclass
class Step:
    """One question asked of the printer, and what came back."""

    name: str
    ok: bool
    detail: str
    hint: str = ""


@dataclass
class Diagnosis:
    """Every step tried, in order, stopping at the first failure."""

    ok: bool
    steps: list[Step]
    info: PrinterInfo | None
    summary: str

    @property
    def failed(self) -> Step | None:
        return next((step for step in self.steps if not step.ok), None)


HINT_RESOLVE = (
    "Use the printer's IP address from its network settings page, or enable "
    "host networking so .local names resolve."
)
HINT_CONNECT = (
    "Is the printer on and on the same network as the Unraid box? Check the "
    "IP on the printer's Network menu."
)
HINT_IPP = (
    "The printer answered on the port but not to IPP. Try the path "
    "/ipp/print, or ipps:// if it only allows TLS."
)
HINT_FORMAT = (
    "This printer only takes formats the paper cannot make yet; tell the "
    "maintainer which ones it listed."
)
HINT_STOPPED = "Clear the printer's error (paper, cover, toner) and test again."

#: IPP printer-state values, in the words the Output tab uses.
STATE_WORDS = {3: "idle", 4: "processing", 5: "stopped"}

#: IPP printer-state-reasons keywords in plain English. The `-warning`,
#: `-report` and `-error` suffix a printer may append is stripped first.
REASON_WORDS = {
    "connecting-to-device": "Connecting to the print engine.",
    "cover-open": "Cover open.",
    "developer-empty": "Developer empty.",
    "developer-low": "Developer low.",
    "door-open": "Door open.",
    "fuser-over-temp": "Fuser too hot.",
    "fuser-under-temp": "Fuser warming up.",
    "input-tray-missing": "Paper tray missing.",
    "interpreter-resource-unavailable": "The printer is out of memory.",
    "marker-supply-empty": "Out of toner.",
    "marker-supply-low": "Toner low.",
    "marker-waste-almost-full": "Waste toner box nearly full.",
    "marker-waste-full": "Waste toner box full.",
    "media-empty": "Out of paper.",
    "media-jam": "Paper jam.",
    "media-low": "Paper low.",
    "media-needed": "Out of paper.",
    "moving-to-paused": "The printer is pausing.",
    "offline": "The printer is offline.",
    "opc-life-over": "Drum worn out.",
    "opc-near-eol": "Drum near the end of its life.",
    "other": "Something else needs attention.",
    "output-area-almost-full": "Output tray nearly full.",
    "output-area-full": "Output tray full.",
    "output-tray-missing": "Output tray missing.",
    "paused": "The printer is paused.",
    "shutdown": "The printer is shut down.",
    "spool-area-full": "The printer's queue is full.",
    "stopped-partly": "The printer is partly stopped.",
    "stopping": "The printer is stopping.",
    "timed-out": "The print engine timed out.",
    "toner-empty": "Out of toner.",
    "toner-low": "Toner low.",
}
REASON_SEVERITIES = ("-error", "-warning", "-report")


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value]
    return [str(value)]


def reason_words(reason: str) -> str:
    """One IPP state reason as a sentence; unknown ones keep their own words."""
    reason = (reason or "").strip()
    base = reason
    for severity in REASON_SEVERITIES:
        if base.endswith(severity):
            base = base[: -len(severity)]
            break
    if not base or base == "none":
        return ""
    known = REASON_WORDS.get(base)
    if known:
        return known
    words = base.replace("-", " ").strip()
    return f"{words[:1].upper()}{words[1:]}." if words else ""


def _state_detail(state: str, reasons: list[str]) -> str:
    sentences = [f"{state[:1].upper()}{state[1:]}." if state else "Unknown state."]
    for reason in reasons:
        words = reason_words(reason)
        if words and words not in sentences:
            sentences.append(words)
    return " ".join(sentences)


def _connect_failure(err: Exception) -> str:
    if isinstance(err, socket.timeout) or isinstance(err, TimeoutError):
        return "timed out"
    if isinstance(err, ConnectionRefusedError):
        return "connection refused"
    if isinstance(err, OSError) and err.strerror:
        return str(err.strerror).lower()
    return str(err) or type(err).__name__


def diagnose(host: str, *, timeout: float = 5.0) -> Diagnosis:
    """Ask the printer, step by step, and say plainly what is wrong.

    Never raises: every failure becomes the last `Step` in the diagnosis,
    with a hint the reader can act on.
    """
    steps: list[Step] = []

    def done(info: PrinterInfo | None, summary: str) -> Diagnosis:
        ok = all(step.ok for step in steps)
        return Diagnosis(ok=ok, steps=steps, info=info, summary=summary)

    def failed(name: str, detail: str, hint: str) -> Diagnosis:
        steps.append(Step(name=name, ok=False, detail=detail, hint=hint))
        return done(None, f"{name}: {detail}")

    # 1. resolve -----------------------------------------------------------
    try:
        uri = printer_uri(host)
    except ValueError as err:
        return failed("resolve", str(err), HINT_RESOLVE)

    parts = urlsplit(uri)
    hostname = parts.hostname or ""
    port = parts.port or DEFAULT_PORT
    try:
        addresses = socket.getaddrinfo(hostname, port, proto=socket.IPPROTO_TCP)
    except OSError:
        return failed("resolve", f"Could not resolve {hostname!r}", HINT_RESOLVE)
    if not addresses:
        return failed("resolve", f"Could not resolve {hostname!r}", HINT_RESOLVE)

    address = str(addresses[0][4][0])
    where = f"{hostname}:{port}"
    detail = where if address == hostname else f"{where} is {address}"
    steps.append(Step("resolve", True, detail))

    # 2. connect -----------------------------------------------------------
    try:
        with socket.create_connection((hostname, port), timeout=timeout):
            pass
    except Exception as err:  # noqa: BLE001 - every failure is a diagnosis
        return failed(
            "connect", f"No answer from {where} ({_connect_failure(err)})", HINT_CONNECT
        )
    steps.append(Step("connect", True, f"{where} answered"))

    # 3. ipp ---------------------------------------------------------------
    try:
        attributes = asyncio.run(_fetch_attributes(uri, timeout=max(timeout, 10.0)))
    except Exception as err:  # noqa: BLE001 - pyipp raises a family of errors
        _LOGGER.warning("IPP query to %s failed", uri, exc_info=True)
        text = str(err) or type(err).__name__
        return failed("ipp", f"{uri} did not answer IPP ({text})", HINT_IPP)
    if not attributes:
        return failed("ipp", f"No printer attributes returned by {uri}", HINT_IPP)

    printer = Printer.from_dict(attributes)
    formats = [str(f) for f in _as_list(attributes.get("document-format-supported"))]
    label = printer.info.name or printer.info.model or where
    steps.append(Step("ipp", True, f"{label} answered Get-Printer-Attributes"))

    # 4. format ------------------------------------------------------------
    try:
        document_format, options = pick_format(attributes)
    except RuntimeError as err:
        return failed("format", str(err), HINT_FORMAT)

    accepts_pdf = document_format == PDF_FORMAT
    if accepts_pdf:
        sending = "PDF"
        steps.append(Step("format", True, "Will send PDF"))
    else:
        sending = f"PWG Raster at {options['dpi']} dpi"
        steps.append(
            Step(
                "format",
                True,
                f"Will send PWG Raster at {options['dpi']} dpi, {options['color']} "
                "(the printer does not take PDF directly)",
            )
        )

    # 5. state -------------------------------------------------------------
    raw_state = attributes.get("printer-state")
    state = STATE_WORDS.get(
        raw_state if isinstance(raw_state, int) else -1,
        str(printer.state.printer_state or "unknown"),
    )
    reasons = [r for r in _as_list(attributes.get("printer-state-reasons")) if r]
    detail = _state_detail(state, reasons)
    info = PrinterInfo(
        name=printer.info.name,
        model=printer.info.model or printer.info.name,
        state=state,
        accepts_pdf=accepts_pdf,
        formats=formats,
    )

    in_error = any(r.strip().endswith("-error") for r in reasons)
    if state == "stopped" or in_error:
        steps.append(Step("state", False, detail, HINT_STOPPED))
        return done(info, f"state: {detail}")

    steps.append(Step("state", True, detail))
    _LOGGER.info("printer %s: %s (sending %s)", uri, detail, sending)
    return done(
        info,
        f"{label}: {state}, accepts PDF" if accepts_pdf else f"{label}: {state}, {sending}",
    )


# ------------------------------------------------------- remembering a check
def record_printer_check(diagnosis: Diagnosis) -> None:
    """Remember the last check for the status strip. Never raises."""
    try:
        import state as state_file

        state_file.update_state(
            printer_check={
                "ok": bool(diagnosis.ok),
                "when": datetime.now().astimezone().isoformat(timespec="seconds"),
                "summary": diagnosis.summary,
            }
        )
    except Exception:  # noqa: BLE001 - the strip is never worth an exception
        _LOGGER.warning("could not record the printer check", exc_info=True)


def record_print_result(host: str, error: str = "") -> None:
    """Remember a print attempt the same way a check is remembered."""
    summary = (
        f"print to {host} failed: {error}" if error else f"print job accepted by {host}"
    )
    record_printer_check(Diagnosis(ok=not error, steps=[], info=None, summary=summary))


# ----------------------------------------------------------------- discovery
def _service_model(properties: dict) -> str:
    def text(key: str) -> str:
        value = properties.get(key.encode()) or properties.get(key)
        if isinstance(value, bytes):
            value = value.decode("utf-8", "ignore")
        return (value or "").strip()

    return text("ty") or text("product").strip("()") or ""


def discover(timeout: float = 3.0) -> list[DiscoveredPrinter]:
    """Browse mDNS for IPP printers. Returns [] on any failure.

    mDNS only reaches the LAN when the container shares the host's broadcast
    domain (`network_mode: host` or macvlan); on bridge networking this
    returns nothing and the manual host field is the way in.
    """
    try:
        from zeroconf import ServiceBrowser, Zeroconf
    except Exception:  # pragma: no cover - zeroconf is a pinned dependency
        _LOGGER.warning("zeroconf unavailable; printer discovery disabled", exc_info=True)
        return []

    seen: list[tuple[str, str]] = []
    zeroconf = None
    browser = None

    class _Listener:
        def add_service(self, zc, type_, name) -> None:  # noqa: ANN001
            if (type_, name) not in seen:
                seen.append((type_, name))

        def update_service(self, zc, type_, name) -> None:  # noqa: ANN001
            self.add_service(zc, type_, name)

        def remove_service(self, zc, type_, name) -> None:  # noqa: ANN001
            return None

    try:
        zeroconf = Zeroconf()
        browser = ServiceBrowser(zeroconf, list(SERVICE_TYPES), _Listener())
        time.sleep(timeout)

        found: list[DiscoveredPrinter] = []
        for type_, name in seen:
            info = zeroconf.get_service_info(type_, name, timeout=1000)
            if info is None:
                continue
            addresses = info.parsed_addresses() or []
            address = addresses[0] if addresses else (info.server or "").rstrip(".")
            if not address:
                continue

            properties = info.properties or {}
            rp = properties.get(b"rp") or properties.get("rp") or b"ipp/print"
            if isinstance(rp, bytes):
                rp = rp.decode("utf-8", "ignore")
            port = info.port or DEFAULT_PORT
            scheme = "ipps" if type_.startswith("_ipps") else "ipp"

            if scheme == "ipp" and port == DEFAULT_PORT and rp.strip("/") == "ipp/print":
                host = address
            else:
                host = f"{scheme}://{address}:{port}/{rp.lstrip('/')}"

            label = name.split(f".{type_}")[0].replace("\\032", " ")
            entry = DiscoveredPrinter(
                name=label, host=host, model=_service_model(properties)
            )
            if not any(e.host == entry.host for e in found):
                found.append(entry)

        _LOGGER.info("discovered %d printer(s)", len(found))
        return found
    except Exception:
        _LOGGER.warning("printer discovery failed", exc_info=True)
        return []
    finally:
        try:
            if browser is not None:
                browser.cancel()
            if zeroconf is not None:
                zeroconf.close()
        except Exception:  # pragma: no cover - teardown must not raise
            pass

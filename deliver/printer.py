"""The print route: direct IPP to an AirPrint-capable printer, no CUPS.

Two halves, for one reason worth writing down:

* **Attributes** (`test_printer`) go through `pyipp`'s async client, which
  already knows how to ask for and parse `Get-Printer-Attributes`.
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
import struct
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import requests
from pyipp import IPP
from pyipp.enums import IppOperation, IppStatus, IppTag
from pyipp.models import Printer
from pyipp.parser import parse as parse_ipp
from pyipp.serializer import construct_attribute

from ._util import issue_label

_LOGGER = logging.getLogger(__name__)

DEFAULT_PORT = 631
DEFAULT_PATH = "/ipp/print"
IPP_VERSION = (2, 0)
MEDIA = "na_letter_8.5x11in"
SIDES_DUPLEX = "two-sided-long-edge"
SIDES_SIMPLEX = "one-sided"
SERVICE_TYPES = ["_ipp._tcp.local.", "_ipps._tcp.local."]

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
]


@dataclass
class PrinterInfo:
    """What the Output tab's "Test" button shows."""

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
def _encode_print_job(uri: str, pdf_bytes: bytes, *, job_name: str, duplex: bool) -> bytes:
    """Encode an IPP Print-Job request with the PDF as the document body."""
    request_id = random.randint(1, 0x7FFF)  # noqa: S311  (not a security value)

    out = struct.pack(">bb", *IPP_VERSION)
    out += struct.pack(">h", IppOperation.PRINT_JOB.value)
    out += struct.pack(">i", request_id)

    # Operation attributes. The first three are required, in this order.
    out += struct.pack(">b", IppTag.OPERATION.value)
    out += construct_attribute("attributes-charset", "utf-8", IppTag.CHARSET)
    out += construct_attribute("attributes-natural-language", "en-us", IppTag.LANGUAGE)
    out += construct_attribute("printer-uri", uri, IppTag.URI)
    out += construct_attribute("requesting-user-name", "molly-ledger", IppTag.NAME)
    out += construct_attribute("job-name", job_name, IppTag.NAME)
    out += construct_attribute("document-format", "application/pdf", IppTag.MIME_TYPE)

    # Job attributes: the ones that make it a US Letter duplex newspaper.
    out += struct.pack(">b", IppTag.JOB.value)
    out += construct_attribute("copies", 1, IppTag.INTEGER)
    out += construct_attribute("media", MEDIA, IppTag.KEYWORD)
    out += construct_attribute(
        "sides", SIDES_DUPLEX if duplex else SIDES_SIMPLEX, IppTag.KEYWORD
    )

    out += struct.pack(">b", IppTag.END.value)
    return out + pdf_bytes


def print_pdf(pdf: Path, host: str, *, duplex: bool = True) -> None:
    """Send `pdf` to the printer at `host` over IPP. Raises on failure."""
    pdf = Path(pdf)
    data = pdf.read_bytes()
    uri = printer_uri(host)
    job_name = f"Molly Ledger {issue_label(pdf)}"

    body = _encode_print_job(uri, data, job_name=job_name, duplex=duplex)

    _LOGGER.info(
        "printing %s (%d bytes) to %s, %s",
        pdf.name,
        len(data),
        uri,
        "duplex" if duplex else "single-sided",
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
                "User-Agent": "MollyLedger/1.0",
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
async def _fetch_attributes(uri: str) -> dict[str, Any]:
    async with IPP(host=uri, request_timeout=10) as ipp:
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

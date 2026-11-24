"""Open a Power BI file (.pbix / .pbit) or a tabular model file (.bim) and expose its parts.

A .pbix/.pbit is a zip archive:
  Report/Layout            legacy report definition (UTF-16 JSON)
  Report/definition/...    newer PBIR report definition (one JSON per page / visual)
  DataModel                VertiPaq model (XPress9-compressed backup), .pbix only
  DataModelSchema          model as TMSL JSON, .pbit only
  DataMashup               Power Query (M) package for older files
"""
from __future__ import annotations

import json
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

SUPPORTED_EXTENSIONS = (".pbix", ".pbit", ".bim")


class PackageError(Exception):
    """The file is not a Power BI file we can read."""


def decode_text(raw: bytes) -> str:
    """Decode the text files Power BI writes (UTF-16 LE, sometimes with a BOM, or UTF-8)."""
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return raw.decode("utf-16")
    if raw[:3] == b"\xef\xbb\xbf":
        return raw[3:].decode("utf-8")
    if len(raw) > 1 and raw[1:2] == b"\x00":
        return raw.decode("utf-16-le").lstrip("﻿")
    return raw.decode("utf-8", errors="replace").lstrip("﻿")


@dataclass
class Package:
    path: Path
    kind: str                                  # "pbix" | "pbit" | "bim"
    names: list[str] = field(default_factory=list)
    _zip: zipfile.ZipFile | None = None

    # ---- what is inside -------------------------------------------------------------------
    @property
    def has_vertipaq_model(self) -> bool:
        return "DataModel" in self.names

    @property
    def has_model_schema(self) -> bool:
        return self.kind == "bim" or "DataModelSchema" in self.names

    @property
    def has_legacy_layout(self) -> bool:
        return "Report/Layout" in self.names

    @property
    def has_pbir(self) -> bool:
        return any(n.startswith("Report/definition/") for n in self.names)

    # ---- readers ----------------------------------------------------------------------------
    def exists(self, name: str) -> bool:
        return name in self.names

    def read_bytes(self, name: str) -> bytes:
        if not self._zip:
            raise PackageError("Not a zip-based Power BI file")
        return self._zip.read(name)

    def read_text(self, name: str) -> str:
        return decode_text(self.read_bytes(name))

    def read_json(self, name: str):
        return json.loads(self.read_text(name))

    def files_under(self, prefix: str) -> list[str]:
        return [n for n in self.names if n.startswith(prefix)]

    def model_schema(self) -> dict | None:
        if self.kind == "bim":
            return json.loads(decode_text(self.path.read_bytes()))
        if "DataModelSchema" in self.names:
            return self.read_json("DataModelSchema")
        return None

    def close(self) -> None:
        if self._zip:
            self._zip.close()
            self._zip = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def open_package(path: str | Path) -> Package:
    p = Path(path)
    ext = p.suffix.lower()
    if ext not in SUPPORTED_EXTENSIONS:
        raise PackageError(f"Unsupported file type '{ext}'. Use one of: {', '.join(SUPPORTED_EXTENSIONS)}")
    if ext == ".bim":
        return Package(p, "bim")
    if not zipfile.is_zipfile(p):
        raise PackageError("This is not a valid Power BI file (it is not a zip archive).")
    z = zipfile.ZipFile(p)
    return Package(p, ext[1:], z.namelist(), z)

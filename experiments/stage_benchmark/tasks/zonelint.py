"""Task ``zonelint`` — two independent rule packages behind one preflight layer.

``zonelint.records`` (zone file syntax, TTL bounds, CNAME exclusivity) and ``zonelint.certs``
(certificate key strength, expiry window, hostname coverage) are complete, import nothing from each
other, and fail in different shapes. The shared ``preflight`` layer is a stub: it must merge both
finding sets, order them, and decide a status. Two substantial read-only investigations exist and
neither is required to understand the other.
"""

from __future__ import annotations

from .base import TaskSpec, WrongVariant

TASK_ID = "zonelint"

_README = """# zonelint

A pre-release check for one DNS zone and one TLS certificate.

* `zonelint.records` parses zone lines and reports record findings.
* `zonelint.certs` reports certificate findings.
* `zonelint.preflight` merges both into one report. It is incomplete.
"""

_PYTEST = """[pytest]
testpaths = tests
"""

_INIT = '''"""Zone and certificate preflight checks."""

from .certs import Certificate, check_certificate
from .errors import CertError, ZoneError
from .findings import Finding, Report
from .preflight import run_preflight
from .records import Record, check_zone, parse_zone_record

__all__ = [
    "Certificate",
    "CertError",
    "Finding",
    "Record",
    "Report",
    "ZoneError",
    "check_certificate",
    "check_zone",
    "parse_zone_record",
    "run_preflight",
]
'''

_ERRORS = '''"""The typed failures of the two rule packages.

A rule package never returns "something went wrong": every failure it reports is a
:class:`~zonelint.findings.Finding`. These exceptions exist for the parser boundaries, where a caller
asked for a value that cannot exist.
"""

from __future__ import annotations


class ZoneError(ValueError):
    """A zone line that cannot be parsed into a record."""

    def __init__(self, message: str, code: str = "malformed_record") -> None:
        super().__init__(message)
        self.code = code


class CertError(ValueError):
    """A certificate description that cannot exist."""

    def __init__(self, message: str, code: str = "malformed_certificate") -> None:
        super().__init__(message)
        self.code = code
'''

_FINDINGS = '''"""What a check can report, and how a report is shaped."""

from __future__ import annotations

from dataclasses import dataclass, field

SEVERITIES = ("error", "warning", "info")
_RANK = {name: index for index, name in enumerate(SEVERITIES)}


@dataclass(frozen=True, order=True)
class Finding:
    """One reported problem. Ordering is documented in :func:`~zonelint.preflight.run_preflight`."""

    severity: str
    subject: str
    code: str
    detail: str = ""

    def __post_init__(self) -> None:
        if self.severity not in _RANK:
            raise ValueError(f"unknown severity: {self.severity!r}")

    @property
    def sort_key(self) -> tuple[int, str, str]:
        return (_RANK[self.severity], self.subject, self.code)

    def as_dict(self) -> dict[str, str]:
        return {
            "severity": self.severity,
            "subject": self.subject,
            "code": self.code,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class Report:
    """The frozen outcome of one preflight run."""

    status: str
    findings: tuple[Finding, ...] = ()
    counts: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "counts": dict(self.counts),
            "findings": [item.as_dict() for item in self.findings],
        }
'''

_RECORDS = '''"""Zone file rules.

A zone line is ``name ttl TYPE value``. Supported types are ``A``, ``AAAA``, ``CNAME`` and ``TXT``.
A TTL must be between 60 and 604800 seconds inclusive. A name carrying a ``CNAME`` may carry no other
record: a CNAME is an alias and every other record at that name would be unreachable.

Every problem is reported, never raised, so one bad line does not hide the rest of the zone.
"""

from __future__ import annotations

from dataclasses import dataclass

from .errors import ZoneError
from .findings import Finding

SUPPORTED_TYPES = frozenset({"A", "AAAA", "CNAME", "TXT"})
TTL_MIN = 60
TTL_MAX = 604_800


@dataclass(frozen=True)
class Record:
    name: str
    ttl: int
    rtype: str
    value: str


def parse_zone_record(line: str) -> Record:
    """Parse one zone line into a record, or raise :class:`ZoneError`."""

    parts = line.split()
    if len(parts) < 4:
        raise ZoneError(f"expected 'name ttl TYPE value', got {line!r}")
    name, ttl_text, rtype, value = parts[0], parts[1], parts[2].upper(), " ".join(parts[3:])
    if not name or name.startswith("!"):
        raise ZoneError(f"invalid name {name!r}")
    try:
        ttl = int(ttl_text)
    except ValueError as error:
        raise ZoneError(f"ttl is not an integer: {ttl_text!r}") from error
    if rtype not in SUPPORTED_TYPES:
        raise ZoneError(f"unsupported type {rtype!r}", code="unsupported_type")
    return Record(name=name, ttl=ttl, rtype=rtype, value=value)


def check_zone(lines: tuple[str, ...] | list[str]) -> tuple[Finding, ...]:
    """Every finding of one zone, ordered by line number."""

    findings: list[tuple[int, Finding]] = []
    by_name: dict[str, list[tuple[int, Record]]] = {}
    for number, line in enumerate(lines, start=1):
        if not line.strip() or line.lstrip().startswith(";"):
            continue
        try:
            record = parse_zone_record(line)
        except ZoneError as error:
            findings.append((number, Finding("error", f"line:{number}", error.code, str(error))))
            continue
        if not TTL_MIN <= record.ttl <= TTL_MAX:
            findings.append(
                (
                    number,
                    Finding(
                        "error",
                        record.name,
                        "invalid_ttl",
                        f"ttl {record.ttl} outside [{TTL_MIN}, {TTL_MAX}]",
                    ),
                )
            )
        by_name.setdefault(record.name, []).append((number, record))
    for name, entries in sorted(by_name.items()):
        if "CNAME" in {record.rtype for _, record in entries} and len(entries) > 1:
            findings.append(
                (entries[0][0], Finding("error", name, "cname_coexists", "a CNAME name carries no other record"))
            )
    ordered = [
        item for _, item in sorted(findings, key=lambda pair: (pair[0], pair[1].sort_key))
    ]
    return tuple(ordered)
'''

_CERTS = '''"""Certificate rules.

A certificate is described by its hostname, its subject alternative names, its remaining lifetime in
days, and its key size in bits. A wildcard SAN ``*.example.com`` covers exactly one extra label and
never the bare name.
"""

from __future__ import annotations

from dataclasses import dataclass

from .findings import Finding

MIN_KEY_BITS = 2048
EXPIRY_WARNING_DAYS = 30


@dataclass(frozen=True)
class Certificate:
    hostname: str
    san: tuple[str, ...]
    days_to_expiry: int
    key_bits: int


def _covers(pattern: str, hostname: str) -> bool:
    if pattern == hostname:
        return True
    if not pattern.startswith("*."):
        return False
    suffix = pattern[2:]
    if not hostname.endswith("." + suffix):
        return False
    return "." not in hostname[: -len(suffix) - 1]


def check_certificate(cert: Certificate) -> tuple[Finding, ...]:
    """Every finding of one certificate, ordered by code."""

    findings: list[Finding] = []
    if cert.key_bits < MIN_KEY_BITS:
        findings.append(
            Finding("error", cert.hostname, "weak_key", f"key {cert.key_bits} bits < {MIN_KEY_BITS}")
        )
    if cert.days_to_expiry < 0:
        findings.append(Finding("error", cert.hostname, "expired", "certificate has expired"))
    elif cert.days_to_expiry <= EXPIRY_WARNING_DAYS:
        findings.append(
            Finding(
                "warning",
                cert.hostname,
                "expiring_soon",
                f"{cert.days_to_expiry} days left, warn at {EXPIRY_WARNING_DAYS}",
            )
        )
    if not any(_covers(pattern, cert.hostname) for pattern in cert.san):
        findings.append(
            Finding("error", cert.hostname, "hostname_mismatch", "no SAN covers the hostname")
        )
    return tuple(sorted(findings, key=lambda item: item.sort_key))
'''

_PREFLIGHT = '''"""The shared preflight.

``run_preflight`` is the only function that knows a zone and a certificate exist together. It must:

* collect the findings of both rule packages, in their own order and without re-deriving them;
* order the combined list by ``(severity, subject, code)`` with ``error`` before ``warning`` before
  ``info``;
* count findings per severity, always with all three keys present;
* return ``status="FAIL"`` when any error exists, ``"WARN"`` when only warnings do, else ``"PASS"``.

The report must be a deterministic function of its inputs and must not mutate them.
"""

from __future__ import annotations

from .certs import Certificate
from .findings import Report


def run_preflight(zone_lines: tuple[str, ...] | list[str], cert: Certificate) -> Report:
    """Merge both rule packages into one ordered report."""

    raise NotImplementedError("run_preflight is not implemented yet")
'''

_TEST_RECORDS = """import pytest

from zonelint.errors import ZoneError
from zonelint.records import check_zone, parse_zone_record


def test_a_record_parses() -> None:
    record = parse_zone_record("api 300 A 10.0.0.1")
    assert (record.name, record.ttl, record.rtype, record.value) == ("api", 300, "A", "10.0.0.1")


def test_txt_keeps_spaces() -> None:
    assert parse_zone_record('mail 60 TXT "v=spf1 -all"').value == '"v=spf1 -all"'


def test_a_short_line_is_rejected() -> None:
    with pytest.raises(ZoneError):
        parse_zone_record("api 300 A")


def test_an_unsupported_type_is_rejected_by_the_parser() -> None:
    with pytest.raises(ZoneError):
        parse_zone_record("api 300 MX mail.example.com")


def test_out_of_range_ttl_is_a_finding_not_an_exception() -> None:
    findings = check_zone(["api 5 A 10.0.0.1"])
    assert [item.code for item in findings] == ["invalid_ttl"]


def test_a_clean_zone_has_no_findings() -> None:
    assert check_zone(["api 300 A 10.0.0.1", "www 300 AAAA 2001:db8::1"]) == ()
"""

_TEST_CERTS = """from zonelint.certs import Certificate, check_certificate


def _cert(**overrides):
    values = {
        "hostname": "api.example.com",
        "san": ("api.example.com",),
        "days_to_expiry": 120,
        "key_bits": 2048,
    }
    values.update(overrides)
    return Certificate(**values)


def test_a_clean_certificate_has_no_findings() -> None:
    assert check_certificate(_cert()) == ()


def test_a_weak_key_is_an_error() -> None:
    assert [item.code for item in check_certificate(_cert(key_bits=1024))] == ["weak_key"]


def test_expiry_within_the_window_is_a_warning() -> None:
    findings = check_certificate(_cert(days_to_expiry=10))
    assert [(item.code, item.severity) for item in findings] == [("expiring_soon", "warning")]


def test_an_expired_certificate_is_an_error() -> None:
    assert [item.code for item in check_certificate(_cert(days_to_expiry=-1))] == ["expired"]


def test_a_hostname_outside_the_san_is_an_error() -> None:
    findings = check_certificate(_cert(hostname="api.other.com"))
    assert [item.code for item in findings] == ["hostname_mismatch"]
"""

_TEST_PUBLIC = """import pytest

from zonelint.certs import Certificate
from zonelint.preflight import run_preflight

GOOD_CERT = Certificate(
    hostname="api.example.com",
    san=("api.example.com",),
    days_to_expiry=120,
    key_bits=2048,
)


def test_a_clean_release_passes() -> None:
    report = run_preflight(["api 300 A 10.0.0.1"], GOOD_CERT)
    assert report.status == "PASS"
    assert report.findings == ()
    assert report.counts == {"error": 0, "warning": 0, "info": 0}


def test_a_bad_ttl_fails_the_release() -> None:
    report = run_preflight(["api 5 A 10.0.0.1"], GOOD_CERT)
    assert report.status == "FAIL"
    assert [item.code for item in report.findings] == ["invalid_ttl"]


def test_a_cname_conflict_is_reported() -> None:
    report = run_preflight(["www 300 CNAME api", "www 300 A 10.0.0.2"], GOOD_CERT)
    assert any(item.code == "cname_coexists" for item in report.findings)


def test_an_expiring_certificate_warns() -> None:
    cert = Certificate("api.example.com", ("api.example.com",), 7, 2048)
    report = run_preflight(["api 300 A 10.0.0.1"], cert)
    assert report.status == "WARN"
    assert [item.code for item in report.findings] == ["expiring_soon"]


def test_errors_are_ordered_before_warnings() -> None:
    cert = Certificate("api.example.com", ("api.example.com",), 7, 2048)
    report = run_preflight(["zzz 5 A 10.0.0.1"], cert)
    assert [item.severity for item in report.findings] == ["error", "warning"]


def test_counts_match_the_findings() -> None:
    cert = Certificate("api.example.com", ("api.example.com",), 7, 2048)
    report = run_preflight(["zzz 5 A 10.0.0.1"], cert)
    assert report.counts["error"] == len([i for i in report.findings if i.severity == "error"])
    assert report.counts["warning"] == 1


def test_the_report_is_repeatable() -> None:
    zone = ["api 300 A 10.0.0.1", "zzz 5 A 10.0.0.2"]
    first = run_preflight(zone, GOOD_CERT)
    second = run_preflight(zone, GOOD_CERT)
    assert first.as_dict() == second.as_dict()
"""

_REFERENCE = '''"""The shared preflight.

``run_preflight`` is the only function that knows a zone and a certificate exist together. It must:

* collect the findings of both rule packages, in their own order and without re-deriving them;
* order the combined list by ``(severity, subject, code)`` with ``error`` before ``warning`` before
  ``info``;
* count findings per severity, always with all three keys present;
* return ``status="FAIL"`` when any error exists, ``"WARN"`` when only warnings do, else ``"PASS"``.

The report must be a deterministic function of its inputs and must not mutate them.
"""

from __future__ import annotations

from .certs import Certificate, check_certificate
from .findings import SEVERITIES, Finding, Report
from .records import check_zone


def run_preflight(zone_lines: tuple[str, ...] | list[str], cert: Certificate) -> Report:
    """Merge both rule packages into one ordered report."""

    findings: tuple[Finding, ...] = tuple(
        sorted((*check_zone(zone_lines), *check_certificate(cert)), key=lambda item: item.sort_key)
    )
    counts = {name: 0 for name in SEVERITIES}
    for item in findings:
        counts[item.severity] += 1
    if counts["error"]:
        status = "FAIL"
    elif counts["warning"]:
        status = "WARN"
    else:
        status = "PASS"
    return Report(status=status, findings=findings, counts=counts)
'''

_ORACLE = """import traceback

from zonelint.certs import Certificate, check_certificate
from zonelint.findings import Finding
from zonelint.preflight import run_preflight

FAILURES = []


def check(condition, message):
    if not condition:
        FAILURES.append(message)


GOOD = Certificate("api.example.com", ("api.example.com",), 120, 2048)


try:
    # An unsupported type is a finding, not a crash, and the rest of the zone still counts.
    report = run_preflight(["api 300 MX mail", "www 5 A 10.0.0.1"], GOOD)
    codes = [item.code for item in report.findings]
    check(codes == ["unsupported_type", "invalid_ttl"], f"unsupported type handling: {codes}")
    check(report.status == "FAIL", "unsupported type did not fail the release")
    # A malformed line does not stop the analysis.
    report = run_preflight(["bad line", "www 300 A 10.0.0.1"], GOOD)
    check([item.code for item in report.findings] == ["malformed_record"], "malformed line collection")
    # Ordering is severity first, then subject, then code, across both packages.
    cert = Certificate("api.example.com", ("api.example.com",), 7, 1024)
    report = run_preflight(["zzz 300 A 10.0.0.1"], cert)
    kinds = [(item.severity, item.code) for item in report.findings]
    check(kinds[0][0] == "error", f"a warning was ordered before an error: {kinds}")
    check(kinds[-1][0] == "warning", f"ordering does not end with the warning: {kinds}")
    check(report.status == "FAIL", "FAIL did not outrank WARN")
    # A report carrying only warnings is a warning, not a blocked release.
    warning_only = Certificate("api.example.com", ("api.example.com",), 7, 2048)
    only_warnings = run_preflight(["api 300 A 10.0.0.1"], warning_only)
    check(only_warnings.status == "WARN", f"a warnings-only report is {only_warnings.status}")
    # Wildcards cover exactly one extra label and never the bare name.
    wildcard = Certificate("a.example.com", ("*.example.com",), 120, 2048)
    check(check_certificate(wildcard) == (), "wildcard did not cover one extra label")
    bare = Certificate("example.com", ("*.example.com",), 120, 2048)
    check([i.code for i in check_certificate(bare)] == ["hostname_mismatch"], "wildcard covered bare")
    deep = Certificate("a.b.example.com", ("*.example.com",), 120, 2048)
    check(
        [i.code for i in check_certificate(deep)] == ["hostname_mismatch"],
        "wildcard covered two extra labels",
    )
    # Key-size and TTL boundaries are inclusive exactly where documented.
    check(check_certificate(Certificate("x.io", ("x.io",), 120, 2048)) == (), "2048 rejected")
    check(
        [i.code for i in check_certificate(Certificate("x.io", ("x.io",), 120, 2047))] == ["weak_key"],
        "2047 accepted",
    )
    check(run_preflight(["a 60 A 10.0.0.1"], GOOD).status == "PASS", "ttl 60 rejected")
    check(run_preflight(["a 604800 A 10.0.0.1"], GOOD).status == "PASS", "ttl 604800 rejected")
    for ttl in (59, 604801):
        check(
            run_preflight([f"a {ttl} A 10.0.0.1"], GOOD).status == "FAIL",
            f"ttl {ttl} accepted",
        )
    # Counts always carry all three severities, even when two are zero.
    counts = run_preflight(["a 300 A 10.0.0.1"], GOOD).counts
    check(set(counts) == {"error", "warning", "info"}, f"counts keys: {sorted(counts)}")
    check(sum(counts.values()) == 0, "clean report counts are not zero")
    # Findigs are frozen values and the report does not alias its input.
    zone = ["a 5 A 10.0.0.1"]
    first = run_preflight(zone, GOOD)
    second = run_preflight(zone, GOOD)
    check(first.as_dict() == second.as_dict(), "report is not reproducible")
    check(zone == ["a 5 A 10.0.0.1"], "run_preflight mutated its input")
    check(isinstance(first.findings[0], Finding), "findings are not Finding values")
except BaseException:  # noqa: BLE001
    traceback.print_exc()
    print("HIDDEN_FAIL")
    raise SystemExit(1) from None

if FAILURES:
    for item in FAILURES:
        print(f"FAIL {item}")
    print("HIDDEN_FAIL")
    raise SystemExit(1)
print("HIDDEN_OK")
"""

SPEC = TaskSpec(
    task_id=TASK_ID,
    title="DNS and certificate preflight",
    structure="INDEPENDENT_INVESTIGATION",
    rationale=(
        "Two rule packages with disjoint domains, no import between them, and different failure "
        "shapes: records report findings by scanning, certificates classify one object. The shared "
        "layer needs both semantics. Either package can be summarised in a few hundred tokens without "
        "the other, and neither requires the implementing agent to hold the whole repository."
    ),
    goal=(
        "The zonelint project checks a DNS zone and a TLS certificate, but the shared preflight layer "
        "in zonelint/preflight.py is unimplemented. Implement run_preflight so it merges both rule "
        "packages into one ordered, counted report with the documented status. Keep the records and "
        "certificates packages unchanged and verify with the public tests."
    ),
    write_scope=("zonelint/preflight.py",),
    files={
        "README.md": _README,
        "pytest.ini": _PYTEST,
        "zonelint/__init__.py": _INIT,
        "zonelint/errors.py": _ERRORS,
        "zonelint/findings.py": _FINDINGS,
        "zonelint/records.py": _RECORDS,
        "zonelint/certs.py": _CERTS,
        "zonelint/preflight.py": _PREFLIGHT,
        "tests/test_records.py": _TEST_RECORDS,
        "tests/test_certs.py": _TEST_CERTS,
        "tests/test_public_preflight.py": _TEST_PUBLIC,
    },
    reference={"zonelint/preflight.py": _REFERENCE},
    wrong=(
        WrongVariant(
            name="warnings_fail",
            covers="a warning treated as a release blocker",
            file="zonelint/preflight.py",
            edits=(
                (
                    '    if counts["error"]:\n        status = "FAIL"\n    elif counts["warning"]:\n        status = "WARN"',
                    '    if counts["error"] or counts["warning"]:\n        status = "FAIL"\n    elif counts["warning"]:\n        status = "WARN"',
                ),
            ),
        ),
        WrongVariant(
            name="sorted_by_code_only",
            covers="findings ordered by code, not severity first",
            file="zonelint/preflight.py",
            edits=(
                (
                    "        sorted((*check_zone(zone_lines), *check_certificate(cert)), key=lambda item: item.sort_key)",
                    "        sorted((*check_zone(zone_lines), *check_certificate(cert)), key=lambda item: item.code)",
                ),
            ),
        ),
        WrongVariant(
            name="counts_omit_empty_severity",
            covers="a counts dict missing severities that did not occur",
            file="zonelint/preflight.py",
            edits=(
                (
                    "    counts = {name: 0 for name in SEVERITIES}",
                    "    counts = {item.severity: 0 for item in findings}",
                ),
            ),
        ),
        WrongVariant(
            name="input_is_aliased",
            covers="returning a report that shares state with its input",
            file="zonelint/preflight.py",
            edits=(
                (
                    "    return Report(status=status, findings=findings, counts=counts)",
                    "    report = Report(status=status, findings=findings, counts=counts)\n"
                    "    if zone_lines:\n"
                    "        zone_lines.clear()\n"
                    "    return report",
                ),
            ),
        ),
    ),
    compatibility_command=("tests/test_records.py", "tests/test_certs.py"),
    target_command=("tests/test_public_preflight.py",),
    oracle_source=_ORACLE,
    expectation={
        "compatibility_tests": 11,
        "target_tests": 7,
        "reference_tests": 18,
        "independent_investigations": 2,
        "acceptance_difficulty": "MEDIUM",
    },
    labels={
        "change_surface": "1 file, 1 function; reads two rule packages",
        "key_dependencies": "zonelint.records.check_zone, zonelint.certs.check_certificate, Finding.sort_key",
        "oracle_scope": "error accumulation, cross-package ordering, wildcard label depth, boundary values",
    },
    notes="Two independent investigations; the public suite covers none of the wildcard depth rules.",
)

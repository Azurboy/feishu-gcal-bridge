"""Read one Feishu calendar without ever writing to CalDAV."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from datetime import datetime
from urllib.parse import urljoin, urlsplit

from caldav import Calendar as DAVCalendar
from caldav import DAVClient
from caldav.elements import cdav
from icalendar import Calendar

from .events import Snapshot, parse_resource


class SourceError(RuntimeError):
    pass


def _host(url: str) -> str:
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise SourceError("caldav_url_must_be_https_without_embedded_credentials")
    return parsed.hostname.lower()


def _same_host(origin: str, candidate: str) -> str:
    if _host(origin) != _host(candidate):
        raise SourceError("caldav_cross_host_url_rejected")
    return candidate


def _check_multistatus(raw: bytes | str, *, report: bool) -> None:
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        raise SourceError("invalid_caldav_multistatus") from exc
    if root.tag != "{DAV:}multistatus":
        raise SourceError("unexpected_caldav_response")
    if not report:
        return
    for item in root.findall("{DAV:}response"):
        statuses = [x.text or "" for x in item.findall("{DAV:}status")]
        statuses += [x.text or "" for x in item.findall("{DAV:}propstat/{DAV:}status")]
        if not statuses or any(not re.search(r"\s2\d\d(?:\s|$)", status) for status in statuses):
            raise SourceError("partial_caldav_multistatus")


class FeishuSource:
    def __init__(self, base_url: str, username: str, password: str) -> None:
        self.base_url = base_url.rstrip("/") + "/"
        self.username = username
        _host(self.base_url)
        self.client = DAVClient(
            url=self.base_url, username=username, password=password,
            timeout=20, require_tls=True,
        )
        # Credentials must never accompany an automatic cross-host redirect.
        original_http_request = self.client.session.request

        def no_redirect(*args, **kwargs):
            kwargs["allow_redirects"] = False
            return original_http_request(*args, **kwargs)

        self.client.session.request = no_redirect
        original_dav_request = self.client.request

        def checked_request(url, method="GET", body="", headers=None, **kwargs):
            _same_host(self.base_url, str(urljoin(self.base_url, str(url))))
            response = original_dav_request(url, method=method, body=body, headers=headers, **kwargs)
            if response.status in {301, 302, 303, 307, 308}:
                raise SourceError("caldav_redirect_requires_manual_review")
            if method.upper() == "REPORT" and response.status == 207:
                _check_multistatus(response.raw, report=True)
            if method.upper() == "PROPFIND" and response.status == 207:
                _check_multistatus(response.raw, report=False)
            return response

        self.client.request = checked_request

    def calendars(self) -> list[tuple[str, str]]:
        found: list[DAVCalendar] = []
        try:
            found = list(self.client.principal().calendars())
        except Exception:
            # Feishu has historically omitted calendar-home discovery at /.
            pass
        if not found:
            collection = urljoin(self.base_url, "/calendars/")
            response = self.client.request(
                collection, method="PROPFIND",
                body="""<?xml version="1.0"?><d:propfind xmlns:d="DAV:"><d:allprop/></d:propfind>""",
                headers={"Depth": "1", "Content-Type": "application/xml"},
            )
            if response.status != 207:
                raise SourceError("caldav_calendar_list_unavailable")
            _check_multistatus(response.raw, report=False)
            root = ET.fromstring(response.raw)
            for item in root.findall("{DAV:}response"):
                href = item.findtext("{DAV:}href")
                if not href:
                    continue
                url = _same_host(self.base_url, urljoin(collection, href))
                path = urlsplit(url).path
                if re.fullmatch(r"/calendars/[^/]+/", path):
                    found.append(DAVCalendar(client=self.client, url=url))
        output: list[tuple[str, str]] = []
        seen: set[str] = set()
        for calendar in found:
            url = _same_host(self.base_url, str(calendar.url))
            if url in seen:
                continue
            seen.add(url)
            try:
                name = calendar.get_display_name() or "Feishu"
            except Exception:
                name = "Feishu"
            output.append((str(name), url))
        if not output:
            raise SourceError("no_caldav_calendar_found")
        return output

    def timezone(self, calendar_url: str) -> str | None:
        calendar = DAVCalendar(client=self.client, url=_same_host(self.base_url, calendar_url))
        try:
            properties = calendar.get_properties([cdav.CalendarTimezone()])
            text = "\n".join(str(value) for value in properties.values())
            match = re.search(r"(?m)^TZID:([^\r\n]+)", text)
            if match:
                return match.group(1).strip()
        except Exception:
            pass
        return None

    def scan(
        self,
        calendar_url: str,
        zone_name: str,
        start: datetime,
        end: datetime,
        mode: str,
    ) -> Snapshot:
        snapshot = Snapshot()
        try:
            calendar_url = _same_host(self.base_url, calendar_url)
            calendar = DAVCalendar(client=self.client, url=calendar_url)
            try:
                objects = calendar.search(
                    start=start, end=end, event=True, expand=False, split_expanded=False
                )
            except Exception:
                # Older Feishu installations may fail REPORT but allow collection/GET.
                objects = calendar.events()
            if len(objects) > 10_000:
                raise SourceError("source_resource_limit")
            grouped: dict[str, list] = {}
            zones: list = []
            for obj in objects:
                _same_host(self.base_url, str(obj.url))
                raw = obj.data
                if not raw:
                    obj.load()
                    raw = obj.data
                if not raw:
                    raise SourceError("missing_calendar_data")
                try:
                    parsed = Calendar.from_ical(raw)
                    components = list(parsed.walk("VEVENT"))
                    if not components:
                        raise ValueError("missing_vevent")
                    zones.extend(parsed.walk("VTIMEZONE"))
                    for component in components:
                        uid = str(component.get("UID", ""))
                        if not uid:
                            raise ValueError("missing_uid")
                        grouped.setdefault(uid, []).append(component)
                except Exception:
                    snapshot.complete = False
                    snapshot.errors.append("calendar_parse_failed")
            for components in grouped.values():
                combined = Calendar()
                combined.add("VERSION", "2.0")
                for tz in zones:
                    combined.add_component(tz)
                for component in components:
                    combined.add_component(component)
                snapshot.absorb(
                    parse_resource(
                        combined.to_ical(), calendar_url, zone_name, start, end, mode,
                        attendee_email=self.username,
                    )
                )
            if len(snapshot.instances) > 2_000:
                raise SourceError("source_instance_limit")
        except Exception as exc:
            snapshot.complete = False
            name = exc.__class__.__name__
            if isinstance(exc, SourceError):
                snapshot.errors.append(str(exc))
            elif name == "AuthorizationError":
                snapshot.errors.append("caldav_authorization_failed")
            elif name == "NotFoundError":
                snapshot.errors.append("caldav_calendar_not_found")
            else:
                snapshot.errors.append("caldav_read_failed")
        return snapshot

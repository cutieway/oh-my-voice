from __future__ import annotations

import datetime as dt
import email.utils
import hashlib
import json
import pathlib
import re
import time
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen
from typing import Any, Callable

from .cache import json_files, read_json, write_content_addressed
from .config import (
    DEFAULT_USER_AGENT,
    DEFAULT_WIKI_API,
    HTTP_TIMEOUT_SECONDS,
    MAX_RETRIES,
)
from .evidence import source_evidence
from .identity import normalize_name


class WikiApiError(RuntimeError):
    pass


def _utcnow() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def parse_infobox(wikitext: str) -> dict[str, str]:
    """Read the first Infobox template, respecting nested templates and links."""
    # The wiki uses both `{{Infobox ...}}` and `{{NPC infobox ...}}`.
    start_match = re.search(r"\{\{\s*[A-Za-z ]{0,24}?infobox\b", wikitext, re.IGNORECASE)
    if not start_match:
        return {}
    start = start_match.start()
    index = start + 2
    depth = 1
    chunks = []
    chunk_start = index
    link_depth = 0
    while index < len(wikitext) - 1:
        pair = wikitext[index:index + 2]
        if pair == "[[":
            link_depth += 1
            index += 2
            continue
        if pair == "]]" and link_depth:
            link_depth -= 1
            index += 2
            continue
        if pair == "{{":
            depth += 1
            index += 2
            continue
        if pair == "}}":
            depth -= 1
            if depth == 0:
                chunks.append(wikitext[chunk_start:index])
                break
            index += 2
            continue
        if wikitext[index] == "|" and depth == 1 and link_depth == 0:
            chunks.append(wikitext[chunk_start:index])
            chunk_start = index + 1
        index += 1
    fields = {}
    for chunk in chunks[1:]:
        if "=" not in chunk:
            continue
        key, value = chunk.split("=", 1)
        key = re.sub(r"\s+", " ", key.strip()).casefold().replace("_", " ")
        value = re.sub(r"<ref\b[^>]*>.*?</ref\s*>", "", value, flags=re.I | re.S)
        value = re.sub(r"<ref\b[^/]*/\s*>", "", value, flags=re.I)
        value = re.sub(r"\[\[([^]|]+)\|([^]]+)\]\]", r"\2", value)
        value = re.sub(r"\[\[([^]]+)\]\]", r"\1", value)
        value = re.sub(r"'''?", "", value)
        value = re.sub(r"<[^>]+>", " ", value)
        template_pattern = re.compile(r"\{\{([^{}]*)\}\}")
        while template_pattern.search(value):
            value = template_pattern.sub(
                lambda match: match.group(1).split("|")[-1], value
            )
        value = re.sub(r"\s+", " ", value).strip(" |{}")
        if key and value:
            fields[key] = value
    return fields


DISAMBIG_TEMPLATE_RE = re.compile(r"\{\{\s*(?:disambig|disambiguation|dab|dmbox|hndis)\b", re.IGNORECASE)
LINK_RE = re.compile(r"\[\[([^\]|#]+)(?:#[^\]|]*)?(?:\|([^\]]+))?\]\]")


def _revision_wikitext(page: dict) -> str:
    revision = (page.get("revisions") or [{}])[0]
    slots = revision.get("slots") or {}
    main = slots.get("main") or {}
    return main.get("content") or main.get("*") or revision.get("content") or ""


def is_disambiguation_page(page: dict) -> bool:
    """The wiki flags disambiguation with a template, not with pageprops."""
    if (page.get("pageprops") or {}).get("disambiguation") is not None:
        return True
    return bool(DISAMBIG_TEMPLATE_RE.search(_revision_wikitext(page)))


def plain_text(wikitext: str) -> str:
    """Render wikitext as prose when the wiki has no TextExtracts extension.

    ffxiv.consolegameswiki.com returns an empty `extract` for every page, so the
    quote and infobox paths have to read the wikitext itself. Tables, templates,
    refs, files and categories are dropped; link labels and prose survive.
    """
    text = re.sub(r"<!--.*?-->", " ", wikitext or "", flags=re.S)
    text = re.sub(r"<ref\b[^>]*>.*?</ref\s*>", " ", text, flags=re.I | re.S)
    text = re.sub(r"<ref\b[^/]*/\s*>", " ", text, flags=re.I)
    text = re.sub(r"\{\|.*?\|\}", " ", text, flags=re.S)
    previous = None
    while previous != text:
        previous = text
        text = re.sub(r"\{\{[^{}]*\}\}", " ", text)
    text = re.sub(r"\[\[(?:file|image|category):[^\]]*\]\]", " ", text, flags=re.I)
    text = LINK_RE.sub(lambda match: match.group(2) or match.group(1), text)
    text = re.sub(r"\[(?:https?:)?//[^\]\s]+\s+([^\]]+)\]", r"\1", text)
    text = re.sub(r"\[(?:https?:)?//[^\]]+\]", " ", text)
    text = re.sub(r"'{2,}", "", text)
    text = re.sub(r"^\s*=+\s*(.*?)\s*=+\s*$", r"\1", text, flags=re.M)
    text = re.sub(r"^\s*[*#:;]+\s*", "", text, flags=re.M)
    text = re.sub(r"<br\s*/?>", " ", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"__[A-Z]+__", " ", text)
    text = re.sub(r"\|\}|\{\|", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _page_list(payload: dict) -> list[dict]:
    query = payload.get("query") or {}
    pages = query.get("pages") or []
    if isinstance(pages, dict):
        return list(pages.values())
    return list(pages)


DISPOSITION_RE = re.compile(
    r"\b(?:demeanou?r|disposition|temperament|temper|personality|cheerful|calm|"
    r"stern|gentle|kind-hearted|brusque|gruff|anxious|nervous|timid|shy|"
    r"secretive|sly|mischievous|solemn|melancholic|gloomy|hot[- ]headed|"
    r"short[- ]tempered|soft[- ]spoken|good[- ]natured|stoic)\b",
    re.IGNORECASE,
)


class MediaWikiProvider:
    def __init__(
        self,
        api_url: str = DEFAULT_WIKI_API,
        *,
        cache_dir: pathlib.Path | str,
        user_agent: str = DEFAULT_USER_AGENT,
        requests_per_second: float = 1.0,
        timeout: float = HTTP_TIMEOUT_SECONDS,
        retries: int = MAX_RETRIES,
        opener: Callable[..., Any] | None = None,
        sleeper: Callable[[float], None] = time.sleep,
    ):
        if not user_agent.strip() or len(user_agent) < 12:
            raise ValueError("MediaWiki requests require a descriptive User-Agent")
        self.api_url = api_url
        self.cache_dir = pathlib.Path(cache_dir)
        self.user_agent = user_agent
        self.interval = 1.0 / max(0.1, float(requests_per_second))
        self.timeout = timeout
        self.retries = retries
        self.opener = opener or urlopen
        self.sleeper = sleeper
        self._last_request = 0.0

    def _cache_key(self, query: dict[str, str]) -> str:
        stable = urlencode(sorted(query.items()))
        return hashlib.sha256((self.api_url + "?" + stable).encode()).hexdigest()

    def _cached_response(self, key: str) -> dict | None:
        paths = json_files(self.cache_dir / "wiki", f"{key}.*.json")
        for path in reversed(paths):
            try:
                value = read_json(path)
            except (OSError, ValueError):
                continue
            if value.get("request_key") == key:
                return value
        return None

    def _wait_rate_limit(self) -> None:
        now = time.monotonic()
        delay = self.interval - (now - self._last_request)
        if delay > 0:
            self.sleeper(delay)
        self._last_request = time.monotonic()

    def _request(self, query: dict[str, str]) -> tuple[dict, dict]:
        key = self._cache_key(query)
        cached = self._cached_response(key)
        headers = {
            "User-Agent": self.user_agent,
            "Accept": "application/json",
        }
        if cached:
            if cached.get("etag"):
                headers["If-None-Match"] = cached["etag"]
            if cached.get("last_modified"):
                headers["If-Modified-Since"] = cached["last_modified"]
        parts = urlsplit(self.api_url)
        merged_query = dict(parse_qsl(parts.query, keep_blank_values=True))
        merged_query.update(query)
        url = urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(merged_query), parts.fragment))

        for attempt in range(self.retries + 1):
            self._wait_rate_limit()
            request = Request(url, headers=headers)
            try:
                response = self.opener(request, timeout=self.timeout)
                status = getattr(response, "status", 200)
                body = response.read().decode("utf-8")
                response_headers = getattr(response, "headers", {})
                if status == 304 and cached:
                    return cached["payload"], cached
                if status < 200 or status >= 300:
                    raise WikiApiError(f"MediaWiki returned HTTP {status}")
                try:
                    payload = json.loads(body)
                except json.JSONDecodeError as exc:
                    raise WikiApiError("MediaWiki returned invalid JSON") from exc
                record = {
                    "request_key": key,
                    "request": query,
                    "etag": response_headers.get("ETag"),
                    "last_modified": response_headers.get("Last-Modified"),
                    "status": status,
                    "payload": payload,
                }
                write_content_addressed(self.cache_dir / "wiki", key, record)
                return payload, record
            except HTTPError as exc:
                if exc.code == 304 and cached:
                    return cached["payload"], cached
                if exc.code == 429 or exc.code >= 500:
                    if attempt >= self.retries:
                        raise WikiApiError(f"MediaWiki request failed with HTTP {exc.code}") from exc
                    retry_after = exc.headers.get("Retry-After") if exc.headers else None
                    delay = self._retry_delay(attempt, retry_after)
                    self.sleeper(delay)
                    continue
                raise WikiApiError(f"MediaWiki request failed with HTTP {exc.code}") from exc
            except (URLError, TimeoutError, OSError) as exc:
                if attempt >= self.retries:
                    raise WikiApiError(f"MediaWiki request failed: {type(exc).__name__}") from exc
                self.sleeper(self._retry_delay(attempt, None))
        raise WikiApiError("MediaWiki request exhausted retries")

    @staticmethod
    def _retry_delay(attempt: int, retry_after: str | None) -> float:
        if retry_after:
            try:
                return max(0.0, min(120.0, float(retry_after)))
            except ValueError:
                try:
                    parsed = email.utils.parsedate_to_datetime(retry_after)
                    return max(0.0, min(120.0, (parsed - dt.datetime.now(dt.timezone.utc)).total_seconds()))
                except (TypeError, ValueError, OverflowError):
                    pass
        return min(30.0, 0.5 * (2 ** attempt))

    def _lookup_title(self, title: str) -> dict | None:
        payload, _ = self._request({
            "action": "query",
            "format": "json",
            "formatversion": "2",
            "titles": title,
            "prop": "pageprops|extracts|revisions|info",
            "inprop": "url",
            "explaintext": "1",
            "exchars": "30000",
            "rvprop": "ids|timestamp|content",
            "rvslots": "main",
            "redirects": "1",
        })
        pages = _page_list(payload)
        for page in pages:
            if "missing" not in page and not page.get("invalid"):
                return page
        return None

    def _search(self, query: str) -> list[dict]:
        payload, _ = self._request({
            "action": "query",
            "format": "json",
            "formatversion": "2",
            "list": "search",
            "srsearch": query,
            "srlimit": "10",
            "srprop": "snippet|titlesnippet|sectionsnippet",
        })
        return list((payload.get("query") or {}).get("search") or [])

    @staticmethod
    def _context_score(page: dict, context: list[str]) -> int:
        text = " ".join([
            str(page.get("title") or ""),
            str(page.get("extract") or ""),
        ]).casefold()
        return sum(1 for item in context if len(item) > 2 and item.casefold() in text)

    def resolve_page(self, dossier: dict) -> dict:
        identity = dossier.get("identity") or {}
        canonical_name = str(identity.get("name") or "").strip()
        aliases = [
            str(alias).strip() for alias in identity.get("aliases", [])
            if isinstance(alias, str) and alias.strip()
        ]
        context = [
            str(identity.get(key) or "")
            for key in ("race", "clan", "occupation", "faction", "location")
        ]
        exact_titles = list(dict.fromkeys([canonical_name] + aliases))
        exact_pages = []
        disambiguation_titles = []
        disambiguation_pages: list[dict] = []
        for title in exact_titles:
            if not title:
                continue
            page = self._lookup_title(title)
            if page is None:
                continue
            if is_disambiguation_page(page):
                disambiguation_titles.append(page.get("title", title))
                disambiguation_pages.append(page)
                continue
            title_norm = normalize_name(page.get("title") or "")
            if title_norm in {normalize_name(t) for t in exact_titles}:
                exact_pages.append(page)
        if len(exact_pages) == 1:
            return self._page_result(exact_pages[0], "exact_title")
        if len(exact_pages) > 1:
            unique = {page.get("pageid"): page for page in exact_pages}
            if len(unique) == 1:
                return self._page_result(next(iter(unique.values())), "exact_alias")
            ranked = sorted(
                ((self._context_score(page, context), page) for page in unique.values()),
                key=lambda item: (-item[0], str(item[1].get("title") or "")),
            )
            if ranked and ranked[0][0] >= 2 and (
                len(ranked) == 1 or ranked[0][0] - ranked[1][0] >= 2
            ):
                return self._page_result(ranked[0][1], "exact_context")
            return {
                "status": "ambiguous",
                "reason": "multiple exact canonical-name or alias pages resolved",
                "candidates": [
                    {"title": p.get("title"), "pageid": p.get("pageid")}
                    for p in unique.values()
                ],
            }

        # A disambiguation page is a link index, not an identity: follow its
        # links and accept a candidate only when the dossier context singles one
        # out. Gerolt is such a page (ARR/HW/SB/Eureka/ShB/EW/DT instances).
        if disambiguation_pages:
            linked = self._resolve_disambiguation_links(disambiguation_pages, context, canonical_name)
            if linked is not None:
                return linked

        search_candidates = []
        for query in exact_titles[:4]:
            if not query:
                continue
            search_candidates.extend(self._search(query + " FFXIV"))
        unique_titles = {}
        for result in search_candidates:
            title = result.get("title")
            if title:
                unique_titles.setdefault(str(title), result)
        resolved = []
        exact_norms = {normalize_name(title) for title in exact_titles if title}
        for title, result in sorted(unique_titles.items())[:10]:
            if title in disambiguation_titles:
                continue
            page = self._lookup_title(title)
            if page is None or is_disambiguation_page(page):
                continue
            score = self._context_score(page, context)
            if normalize_name(page.get("title") or title) in exact_norms:
                score += 10
            resolved.append((score, page, result))
        resolved.sort(key=lambda item: (-item[0], str(item[1].get("title") or "")))
        if resolved:
            top_score = resolved[0][0]
            plausible = [item for item in resolved if item[0] == top_score]
            if top_score >= 10 and len(plausible) == 1:
                return self._page_result(plausible[0][1], "search_exact_title")
            if top_score >= 2 and len(resolved) > 1 and top_score - resolved[1][0] >= 2:
                return self._page_result(resolved[0][1], "search_context")
        return {
            "status": "ambiguous" if resolved or disambiguation_titles else "missing",
            "reason": "multiple plausible pages or no safely matching page",
            "candidates": [
                {"title": page.get("title"), "pageid": page.get("pageid"), "context_score": score}
                for score, page, _ in resolved[:10]
            ],
            "disambiguation_pages": disambiguation_titles,
        }

    def _resolve_disambiguation_links(
        self, pages: list[dict], context: list[str], canonical_name: str
    ) -> dict | None:
        """Resolve a disambiguation page to one linked instance, or nothing."""
        links: list[str] = []
        for page in pages:
            for target, _display in LINK_RE.findall(_revision_wikitext(page)):
                target = target.strip()
                if not target or target.casefold().startswith(("file:", "image:", "category:")):
                    continue
                if target not in links:
                    links.append(target)
        canonical_norm = normalize_name(canonical_name)
        scored = []
        for target in links[:12]:
            page = self._lookup_title(target)
            if page is None or is_disambiguation_page(page):
                continue
            text = " ".join([
                str(page.get("title") or ""),
                page.get("extract") or plain_text(_revision_wikitext(page)),
            ])
            score = self._context_score({"title": page.get("title"), "extract": text}, context)
            if canonical_norm and normalize_name(str(page.get("title") or "")).startswith(canonical_norm):
                score += 5
            scored.append((score, page))
        if not scored:
            return None
        scored.sort(key=lambda item: (-item[0], str(item[1].get("title") or "")))
        top_score = scored[0][0]
        plausible = [item for item in scored if item[0] == top_score]
        # 5 for the canonical-name title prefix, 1 per context term matched in
        # the linked page's own prose (infobox values are stripped from it).
        if len(plausible) == 1 and top_score >= 6:
            return self._page_result(plausible[0][1], "disambiguation_link")
        return None

    @staticmethod
    def _page_result(page: dict, reason: str) -> dict:
        revision = ((page.get("revisions") or [{}])[0])
        wikitext = _revision_wikitext(page)
        return {
            "status": "resolved",
            "reason": reason,
            "title": page.get("title"),
            "pageid": page.get("pageid"),
            "url": page.get("fullurl"),
            "revision_id": revision.get("revid"),
            "revision_timestamp": revision.get("timestamp"),
            "extract": page.get("extract") or plain_text(wikitext),
            "wikitext": wikitext,
        }

    def enrich_dossier(self, dossier: dict) -> dict:
        result = dict(dossier)
        page = self.resolve_page(dossier)
        result["wiki_resolution"] = {key: value for key, value in page.items() if key not in {"extract", "wikitext"}}
        result["wiki_page"] = None
        if page.get("status") != "resolved":
            result["inventory"] = dict(result.get("inventory") or {})
            result["inventory"]["wiki_review_required"] = page.get("status") == "ambiguous"
            result["inventory"]["wiki_resolution_reason"] = page.get("reason")
            result["lore_quotes"] = list(result.get("lore_quotes") or [])
            result["lore_evidence"] = list(result.get("lore_evidence") or [])
            result["voice_evidence"] = list(result.get("voice_evidence") or [])
            result["fact_conflicts"] = list(result.get("fact_conflicts") or [])
            result["input_hash"] = None
            return result

        title = page["title"]
        api_host = urlsplit(self.api_url).scheme + "://" + urlsplit(self.api_url).netloc
        source_url = page.get("url") or f"{api_host}/wiki/{quote(str(title).replace(' ', '_'))}"
        revision_id = page.get("revision_id")
        extract = str(page.get("extract") or "")
        infobox = parse_infobox(str(page.get("wikitext") or ""))
        result["wiki_page"] = {
            "title": title,
            "url": source_url,
            "pageid": page.get("pageid"),
            "revision_id": revision_id,
            "revision_timestamp": page.get("revision_timestamp"),
            "infobox": infobox,
            "plain_text_excerpt": extract[:8000],
        }

        evidence = list(result.get("lore_evidence") or [])
        conflicts = list(result.get("fact_conflicts") or [])
        fields = {
            "race": {"race", "race / clan", "race/clan"},
            "clan": {"clan", "tribe", "race / clan", "race/clan"},
            "sex": {"gender", "sex"},
            "lore_age": {"age", "age (appearance)", "age (actual)"},
            "occupation": {"occupation", "profession", "title"},
            "faction": {"affiliation", "faction", "organization"},
            "location": {"location", "residence", "home"},
        }
        identity = dict(result.get("identity") or {})
        for field, accepted_keys in fields.items():
            match = next(
                ((key, value) for key, value in infobox.items() if key.casefold() in accepted_keys),
                None,
            )
            if match is None:
                continue
            key, value = match
            quote_text = f"{key}: {value}"[:240]
            record = source_evidence(
                f"identity.{field}", "wiki_infobox", source_url, revision_id, quote_text
            )
            record["value"] = value
            evidence.append(record)
            current = identity.get(field)
            if current not in (None, "", "unknown") and normalize_name(str(current)) != normalize_name(value):
                conflicts.append({
                    "field": field,
                    "game_value": current,
                    "wiki_value": value,
                    "game_source": f"ENpcResident/{identity.get('npc_id')}",
                    "wiki_source": source_url,
                    "revision_id": revision_id,
                    "quote": quote_text,
                })
            elif current in (None, "", "unknown") and field in {"lore_age", "occupation", "faction", "location"}:
                identity[field] = value

        voice_terms = re.compile(
            r"\b(?:voice|speaks?|spoken|soft[- ]spoken|loud[- ]spoken|raspy|husky|"
            r"gravelly|breathy|deep[- ]voiced|high[- ]pitched|accent|drawl)\b",
            re.IGNORECASE,
        )
        # The wiki's infobox description is the editors' own one-line
        # characterization, so it counts as lore alongside the page prose.
        description = str(infobox.get("description") or "").strip()
        quote_pool = " ".join(part for part in (description, extract) if part)
        quotes = [
            sentence.strip() for sentence in re.split(r"(?<=[.!?])\s+", quote_pool)
            if voice_terms.search(sentence) and len(sentence.strip()) <= 300
        ][:12]
        voice_evidence = list(result.get("voice_evidence") or [])
        for quote_text in quotes:
            voice_evidence.append(source_evidence(
                "voice_description", "wiki_lore", source_url, revision_id, quote_text
            ))
        # Disposition sentences are what can justify a default mood archon: they
        # describe the character's baseline temperament, not one scene. Kept as
        # quotes so a mood proposal is traceable to a page revision.
        disposition_quotes = [
            sentence.strip() for sentence in re.split(r"(?<=[.!?])\s+", quote_pool)
            if DISPOSITION_RE.search(sentence) and len(sentence.strip()) <= 300
        ][:8]
        lore_quotes = list(result.get("lore_quotes") or [])
        for quote_text in disposition_quotes:
            lore_quotes.append(source_evidence(
                "mood", "wiki_lore", source_url, revision_id, quote_text
            ))
        result["identity"] = identity
        result["lore_quotes"] = lore_quotes
        result["lore_evidence"] = evidence
        result["voice_evidence"] = voice_evidence
        result["fact_conflicts"] = conflicts
        result["wiki_page"]["lore_sentence_count"] = len(
            [sentence for sentence in re.split(r"(?<=[.!?])\s+", extract) if sentence.strip()]
        )
        result["inventory"] = dict(result.get("inventory") or {})
        result["inventory"]["wiki_review_required"] = False
        result["inventory"]["wiki_resolution_reason"] = page["reason"]
        result["sources"] = list(result.get("sources") or []) + [{
            "source_type": "wiki",
            "source": source_url,
            "revision_id": revision_id,
            "fields": sorted({record.get("field") for record in evidence}),
        }]
        result["input_hash"] = None
        return result

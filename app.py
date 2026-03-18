import os
import re
import html
import json
import threading
from flask import Flask, render_template, request, jsonify, Response, stream_with_context
import requests

app = Flask(__name__)

CAPI_BASE = "https://content.guardianapis.com"
DEFAULT_API_KEY = os.environ.get("CAPI_KEY", "")

# Regex patterns to detect rich links in body HTML
RICH_LINK_PATTERN = re.compile(
    r'class="[^"]*element-rich-link[^"]*"',
    re.IGNORECASE,
)


def count_rich_links(body_html: str) -> int:
    """Count the number of rich link elements in a piece of body HTML."""
    return len(RICH_LINK_PATTERN.findall(body_html))


def extract_rich_link_targets(body_html: str) -> list[str]:
    """Extract the hrefs from rich link elements."""
    # Rich links wrap an <a href="..."> inside the element
    pattern = re.compile(
        r'class="[^"]*element-rich-link[^"]*".*?<a[^>]+href="([^"]+)"',
        re.DOTALL | re.IGNORECASE,
    )
    return pattern.findall(body_html)


CAPI_MAX_PAGE_SIZE = 50    # CAPI hard limit per request
CAPI_HARD_PAGE_LIMIT = 200  # CAPI won't serve beyond page 200
MAX_CONCURRENT_SCANS = 3   # simultaneous full scans allowed

_scan_semaphore = threading.Semaphore(MAX_CONCURRENT_SCANS)


def _fetch_capi_page(api_key: str, params: dict, page: int) -> dict:
    """Fetch a single page from CAPI and return the raw response dict."""
    resp = requests.get(
        f"{CAPI_BASE}/search",
        params={**params, "page": page},
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json().get("response", {})


def _parse_item(item: dict) -> dict:
    fields = item.get("fields", {})
    body = fields.get("body", "")
    rich_link_count = count_rich_links(body)
    rich_link_targets = extract_rich_link_targets(body) if rich_link_count else []
    return {
        "id": item.get("id", ""),
        "webUrl": item.get("webUrl", ""),
        "webTitle": item.get("webTitle", ""),
        "sectionName": item.get("sectionName", ""),
        "webPublicationDate": item.get("webPublicationDate", ""),
        "headline": fields.get("headline", item.get("webTitle", "")),
        "trailText": fields.get("trailText", ""),
        "thumbnail": fields.get("thumbnail", ""),
        "wordcount": fields.get("wordcount", ""),
        "richLinkCount": rich_link_count,
        "richLinkTargets": rich_link_targets,
        "tags": [t.get("webTitle", "") for t in item.get("tags", [])],
    }


def query_capi(
    api_key: str,
    query: str = "",
    section: str = "",
    tag: str = "",
    date_from: str = "",
    date_to: str = "",
    page: int = 1,
    page_size: int = 20,
    order_by: str = "newest",
    rich_links_only: bool = False,
    min_rich_links: int = 1,
) -> dict:
    """Query the Guardian CAPI and return articles, optionally filtered to those with rich links."""
    base_params = {
        "api-key": api_key,
        "show-fields": "headline,body,thumbnail,trailText,wordcount",
        "show-tags": "keyword",
        "page-size": CAPI_MAX_PAGE_SIZE,
        "order-by": order_by,
    }

    if query:
        base_params["q"] = query
    if section:
        base_params["section"] = section
    if tag:
        base_params["tag"] = tag
    if date_from:
        base_params["from-date"] = date_from
    if date_to:
        base_params["to-date"] = date_to

    if not rich_links_only:
        # Simple case: single fetch, no filtering
        base_params["page-size"] = page_size
        response = _fetch_capi_page(api_key, base_params, page)
        results = [_parse_item(item) for item in response.get("results", [])]
        return {
            "results": results,
            "total": response.get("total", 0),
            "pages": response.get("pages", 1),
            "currentPage": page,
        }

    # Filtered case: walk CAPI pages until we accumulate enough matching results
    # to fill the requested page number × page_size.
    target_count = page * page_size  # need this many matching articles in total
    collected: list[dict] = []
    capi_page = 1
    capi_total = None
    capi_total_pages = 1

    while len(collected) < target_count:
        if capi_total_pages is not None and capi_page > capi_total_pages:
            break  # exhausted all CAPI pages

        response = _fetch_capi_page(api_key, base_params, capi_page)

        if capi_total is None:
            capi_total = response.get("total", 0)
            capi_total_pages = response.get("pages", 1)

        raw = response.get("results", [])
        if not raw:
            break

        for item in raw:
            parsed = _parse_item(item)
            if parsed["richLinkCount"] >= min_rich_links:
                collected.append(parsed)

        capi_page += 1

    # Paginate the collected results
    start = (page - 1) * page_size
    end = start + page_size
    page_results = collected[start:end]

    # Estimate totals: use the ratio of matching articles seen so far
    scanned = (capi_page - 1) * CAPI_MAX_PAGE_SIZE
    if scanned > 0 and capi_total:
        match_ratio = len(collected) / scanned
        estimated_total = int(capi_total * match_ratio)
        estimated_pages = max(1, (estimated_total + page_size - 1) // page_size)
    else:
        estimated_total = len(collected)
        estimated_pages = max(1, (len(collected) + page_size - 1) // page_size)

    return {
        "results": page_results,
        "total": estimated_total,
        "pages": estimated_pages,
        "currentPage": page,
    }


def get_sections(api_key: str) -> list[dict]:
    """Fetch the list of Guardian sections."""
    params = {"api-key": api_key}
    resp = requests.get(f"{CAPI_BASE}/sections", params=params, timeout=10)
    resp.raise_for_status()
    sections = resp.json().get("response", {}).get("results", [])
    return [{"id": s["id"], "webTitle": s["webTitle"]} for s in sections]


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/search")
def api_search():
    api_key = request.args.get("apiKey", DEFAULT_API_KEY).strip() or DEFAULT_API_KEY
    query = request.args.get("q", "").strip()
    section = request.args.get("section", "").strip()
    tag = request.args.get("tag", "").strip()
    date_from = request.args.get("dateFrom", "").strip()
    date_to = request.args.get("dateTo", "").strip()
    page = int(request.args.get("page", 1))
    page_size = int(request.args.get("pageSize", 20))
    order_by = request.args.get("orderBy", "newest")
    rich_links_only = request.args.get("richLinksOnly", "true").lower() == "true"
    min_rich_links = int(request.args.get("minRichLinks", 1))

    try:
        data = query_capi(
            api_key=api_key,
            query=query,
            section=section,
            tag=tag,
            date_from=date_from,
            date_to=date_to,
            page=page,
            page_size=page_size,
            order_by=order_by,
            rich_links_only=rich_links_only,
            min_rich_links=min_rich_links,
        )

        return jsonify({"ok": True, **data})
    except requests.HTTPError as e:
        return jsonify({"ok": False, "error": str(e), "status": e.response.status_code}), 400
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/sections")
def api_sections():
    api_key = request.args.get("apiKey", DEFAULT_API_KEY).strip() or DEFAULT_API_KEY
    try:
        sections = get_sections(api_key)
        return jsonify({"ok": True, "sections": sections})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500



@app.route("/api/scan")
def api_scan():
    """SSE endpoint: walk every accessible CAPI page and stream articles with rich links."""
    api_key = request.args.get("apiKey", DEFAULT_API_KEY).strip() or DEFAULT_API_KEY
    query = request.args.get("q", "").strip()
    section = request.args.get("section", "").strip()
    tag = request.args.get("tag", "").strip()
    date_from = request.args.get("dateFrom", "").strip()
    date_to = request.args.get("dateTo", "").strip()
    order_by = request.args.get("orderBy", "newest")
    min_rich_links = int(request.args.get("minRichLinks", 1))

    base_params = {
        "api-key": api_key,
        "show-fields": "headline,body,thumbnail,trailText,wordcount",
        "show-tags": "keyword",
        "page-size": CAPI_MAX_PAGE_SIZE,
        "order-by": order_by,
    }
    if query:
        base_params["q"] = query
    if section:
        base_params["section"] = section
    if tag:
        base_params["tag"] = tag
    if date_from:
        base_params["from-date"] = date_from
    if date_to:
        base_params["to-date"] = date_to

    def generate():
        # Acquire before entering try/finally so we only release what we acquired
        if not _scan_semaphore.acquire(blocking=False):
            yield f"data: {json.dumps({'type': 'error', 'error': 'Server is busy — too many scans running. Please try again in a moment.'})}\n\n"
            return

        capi_page = 1
        total_articles = 0
        total_capi_pages = None
        found = 0

        try:
            while True:
                if total_capi_pages is not None and capi_page > total_capi_pages:
                    break

                # Keepalive comment sent before each blocking CAPI call.
                # SSE comments are ignored by the browser but reset proxy
                # and load-balancer idle-connection timers.
                yield ": keepalive\n\n"

                response = _fetch_capi_page(api_key, base_params, capi_page)

                if total_capi_pages is None:
                    total_articles = response.get("total", 0)
                    capi_pages = response.get("pages", 1)
                    total_capi_pages = min(capi_pages, CAPI_HARD_PAGE_LIMIT)

                raw = response.get("results", [])
                if not raw:
                    break

                matches = []
                for item in raw:
                    parsed = _parse_item(item)
                    if parsed["richLinkCount"] >= min_rich_links:
                        matches.append(parsed)
                        found += 1

                scanned = min(capi_page * CAPI_MAX_PAGE_SIZE, total_articles)
                event = {
                    "type": "progress",
                    "scanned": scanned,
                    "total": total_articles,
                    "found": found,
                    "matches": matches,
                }
                yield f"data: {json.dumps(event)}\n\n"
                capi_page += 1

            yield f"data: {json.dumps({'type': 'done', 'found': found, 'total': total_articles})}\n\n"

        except Exception as e:
            yield f"data: {json.dumps({'type': 'error', 'error': str(e)})}\n\n"

        finally:
            _scan_semaphore.release()

    return Response(
        stream_with_context(generate()),
        content_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5050))
    app.run(debug=True, port=port)

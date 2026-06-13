import json
import re
import math
from datetime import datetime, timezone
from urllib.parse import quote
from curl_cffi import requests as curl_requests


def run(headers, user_input):
    """List all active investment offerings on Wefunder's public explore page."""
    base_url = BASE_URL

    # Parse optional filters
    category = (user_input.get("category") or "").strip()
    status_filter = (user_input.get("status") or "live").strip()

    valid_statuses = {"live", "ending_soon", "almost_fully_funded", "new_this_week", "hot_this_week"}
    if status_filter not in valid_statuses:
        return {
            "status_code": 400,
            "body": {"error": f"Invalid status '{status_filter}'. Must be one of: {', '.join(sorted(valid_statuses))}"},
        }

    # --- Build tag filters (Algolia-style AND array) ---
    tag_filters = ["-eu"]

    # Category slot
    tag_filters.append(category if category else "")

    # Status tag mapping (live = default, no extra tag needed)
    status_tag_map = {
        "ending_soon": "closing_soon",
        "almost_fully_funded": "almost_fully_funded",
        "new_this_week": "new_this_week",
        "hot_this_week": "hot_this_week",
    }
    if status_filter in status_tag_map:
        tag_filters.append(status_tag_map[status_filter])

    # Base mandatory tags
    tag_filters.extend(["listed_campaign", "status_fundraising", "visible_by_non_accredited_investors"])

    # Choose sort index based on status
    index_map = {
        "ending_soon": "Company_sorted_by_funding_closed_at_asc",
        "new_this_week": "Company_sorted_by_profile_promoted_at_desc",
    }
    index = index_map.get(status_filter, "Company_sorted_by_trending_desc")

    # --- Request headers ---
    req_headers = {
        **headers,
        "accept": "application/json, text/plain, */*",
        "x-requested-with": "XMLHttpRequest",
        "referer": f"{base_url}/explore",
    }

    # --- Paginate through all results ---
    all_offerings = []
    page = 0
    hits_per_page = 1000

    while True:
        # Build tagFilters as JSON — append trailing [] for default "live" + no category
        tf_list = list(tag_filters)
        if status_filter == "live" and not category:
            tf_json = json.dumps(tf_list)[:-1] + ",[]]"
        else:
            tf_json = json.dumps(tf_list)

        # Build URL manually to match captured browser format exactly
        url = (
            f"{base_url}/-/companies/explore"
            f"?index={quote(index)}"
            f"&tagFilters={quote(tf_json)}"
            f"&page={page}"
            f"&hitsPerPage={hits_per_page}"
            f"&filters="
            f"&vipOverride=false"
            f"&capCompanyCount=false"
        )

        response = curl_requests.get(
            url,
            headers=req_headers,
            impersonate="safari",
            timeout=30,
        )

        # Session expiry detection
        if response.status_code == 401:
            return {"status_code": 401, "body": {"error": "Session expired"}}

        if response.status_code == 403:
            text = response.text[:500]
            if "1010" in text or "Just a moment" in text:
                return {"status_code": 401, "body": {"error": "Session expired or blocked by Cloudflare"}}
            return {"status_code": 403, "body": {"error": "Access denied"}}

        if response.status_code != 200:
            return {
                "status_code": response.status_code,
                "body": {"error": f"API returned HTTP {response.status_code}"},
            }

        try:
            data = response.json()
        except Exception:
            text = response.text[:500]
            if "Just a moment" in text or "login" in text.lower():
                return {"status_code": 401, "body": {"error": "Session expired or blocked by challenge"}}
            return {"status_code": 500, "body": {"error": "Invalid JSON response from API"}}

        companies = data.get("companies", [])
        for c in companies:
            all_offerings.append(_parse_offering(c, base_url))

        nb_pages = data.get("nbPages", 1)
        page += 1
        if page >= nb_pages:
            break

    return {
        "status_code": 200,
        "body": {
            "total": len(all_offerings),
            "offerings": all_offerings,
        },
    }


def _parse_offering(c, base_url):
    """Parse a single company object into the output schema."""
    tags = c.get("Tags", [])

    # Security type
    security_type = _detect_security_type(tags, c.get("securityType", ""))

    # Terms
    terms = c.get("terms") or {}
    terms_type = terms.get("txt")
    terms_value = terms.get("nb")
    valuation_cap_usd = _parse_valuation(terms_value, terms_type)

    # Deadline
    funding_closed_at = c.get("fundingClosedAtSeconds", 0)
    deadline_iso = None
    days_remaining = None
    if funding_closed_at and funding_closed_at > 0:
        deadline_dt = datetime.fromtimestamp(funding_closed_at, tz=timezone.utc)
        deadline_iso = deadline_dt.strftime("%Y-%m-%d")
        now = datetime.now(tz=timezone.utc)
        delta = (deadline_dt - now).total_seconds()
        days_remaining = max(0, math.ceil(delta / 86400))

    # Status
    label = c.get("label")
    status = _map_status(label, tags)

    # Categories
    categories = c.get("verticalAndSectorTagNames", [])

    # Offering URL
    url_slug = c.get("url", c.get("slug", ""))

    return {
        "company_name": c.get("name"),
        "slug": c.get("slug") or url_slug,
        "blurb": c.get("tagline"),
        "categories": categories,
        "security_type": security_type,
        "terms_type": terms_type,
        "terms_value": terms_value,
        "valuation_cap_usd": valuation_cap_usd,
        "raised_usd": c.get("totalRaisedThisCampaign"),
        "investor_count": c.get("totalInvestorsThisCampaign"),
        "days_remaining": days_remaining,
        "deadline_iso": deadline_iso,
        "status": status,
        "percent_funded": c.get("percentFunded"),
        "offering_url": f"{base_url}/{url_slug}" if url_slug else None,
    }


def _detect_security_type(tags, security_type_field):
    """Determine security type from tags, falling back to the securityType field."""
    tag_set = set(tags)
    if "security_safe" in tag_set:
        return "crowd_safe"
    if "security_convertible_note" in tag_set:
        return "convertible_note"
    if "security_revenue_share" in tag_set:
        return "revenue_share"
    if "security_equity" in tag_set:
        return "equity"
    # Fallback
    mapping = {"equity": "equity", "debt": "debt", "custom": "custom"}
    return mapping.get(security_type_field, "unknown")


def _parse_valuation(terms_nb, terms_type):
    """Parse a display string like '$62.1M' into a numeric USD value."""
    if not terms_nb or not terms_type:
        return None
    if terms_type not in ("pre-money valuation", "valuation cap"):
        return None
    cleaned = terms_nb.replace("\xa0", "").replace(",", "").strip()
    match = re.match(r"\$([0-9.]+)\s*([MKBmkb])?", cleaned)
    if not match:
        return None
    try:
        num = float(match.group(1))
    except ValueError:
        return None
    suffix = (match.group(2) or "").upper()
    multipliers = {"M": 1_000_000, "K": 1_000, "B": 1_000_000_000}
    return int(num * multipliers.get(suffix, 1))


def _map_status(label, tags):
    """Map label and tags to a user-friendly status string."""
    if label == "almost_fully_funded":
        return "almost_fully_funded"
    if label == "new_this_week":
        return "new_this_week"
    if label == "hot_this_week":
        return "hot_this_week"
    if "closing_soon" in tags:
        return "ending_soon"
    return "live"

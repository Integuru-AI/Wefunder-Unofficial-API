import re
import json
from curl_cffi import requests as curl_requests


def run(headers, user_input):
    """Fetch the most recent founder updates/posts from a Wefunder offering page."""
    base_url = BASE_URL

    # --- Validate input ---
    offering_slug = user_input.get("offering_slug")
    offering_url = user_input.get("offering_url")

    if not offering_slug and not offering_url:
        return {
            "status_code": 400,
            "body": {
                "error": "Either offering_slug or offering_url is required",
                "error_code": "invalid_input",
            },
        }

    if not offering_slug and offering_url:
        match = re.search(r"wefunder\.com/([^/?#]+)", offering_url)
        if not match:
            return {
                "status_code": 400,
                "body": {
                    "error": "Invalid offering_url format",
                    "error_code": "invalid_input",
                },
            }
        offering_slug = match.group(1)

    limit = min(max(1, int(user_input.get("limit", 10))), 50)

    try:
        page_data = _fetch_offering_page(base_url, offering_slug, headers)
    except SessionExpiredError:
        return {
            "status_code": 401,
            "body": {
                "error": "Session expired",
                "error_code": "session_expired",
                "retryable": True,
            },
        }
    except OfferingNotFoundError as e:
        return {
            "status_code": 404,
            "body": {
                "error": str(e),
                "error_code": "offering_not_found",
            },
        }

    company_id = page_data["company_id"]
    csrf_token = page_data["csrf_token"]

    try:
        all_items = _fetch_feed_items(base_url, offering_slug, company_id, csrf_token, headers, limit)
    except SessionExpiredError:
        return {
            "status_code": 401,
            "body": {
                "error": "Session expired",
                "error_code": "session_expired",
                "retryable": True,
            },
        }

    # --- Transform into output format ---
    posts = []
    for item in all_items:
        content_html = item.get("content") or ""

        # Extract image URLs from HTML <img> tags
        image_urls = re.findall(r'<img[^>]+src="([^"]+)"', content_html)

        # Prepend cover photo if present
        cover = item.get("coverPhoto")
        if cover and isinstance(cover, dict) and cover.get("url"):
            image_urls.insert(0, cover["url"])

        # Author info -- use the personal author (item.author), fall back to
        # display author (company-level) if the personal author is absent.
        author = item.get("author") or {}
        display_author = item.get("displayAuthor") or {}

        author_name = author.get("fullName") or display_author.get("fullName")
        author_logo = author.get("avatarUrl") or display_author.get("avatarUrl") or ""
        if author_logo.startswith("//"):
            author_logo = "https:" + author_logo

        posts.append(
            {
                "post_id": str(item.get("id", "")),
                "title": item.get("title"),
                "body_text": item.get("displayContentPlain") or "",
                "body_html": content_html,
                "author_name": author_name,
                "author_logo_url": author_logo,
                "published_at_iso": item.get("publishedAt"),
                "visibility": item.get("visibility", "public"),
                "image_urls": image_urls,
                "like_count": item.get("likeCount", 0),
                "comment_count": item.get("commentCount", 0),
                "view_count": item.get("readCount"),
                "post_url": item.get("url"),
            }
        )

    return {
        "status_code": 200,
        "body": {
            "offering_slug": offering_slug,
            "total_posts": len(posts),
            "posts": posts,
        },
    }

# === PRIVATE ===


class SessionExpiredError(Exception):
    pass


class OfferingNotFoundError(Exception):
    pass


def _fetch_offering_page(base_url, offering_slug, headers):
    """Fetch the offering HTML page to extract company_id and CSRF token."""
    page_headers = {
        **headers,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
    }
    page_resp = curl_requests.get(
        f"{base_url}/{offering_slug}",
        headers=page_headers,
        impersonate="chrome131",
        timeout=30,
        allow_redirects=True,
    )

    if page_resp.status_code == 404:
        raise OfferingNotFoundError("Offering not found")

    # Session expiry: redirect to login page or non-200
    final_url = str(page_resp.url)
    if "/users/sign_in" in final_url or "/login" in final_url:
        raise SessionExpiredError()

    if page_resp.status_code != 200:
        raise OfferingNotFoundError(f"Offering page returned status {page_resp.status_code}")

    page_text = page_resp.text

    # Parse company_id from analytics gtag snippet
    cid_match = re.search(r'"company_id"\s*:\s*(\d+)', page_text)
    if not cid_match:
        # Distinguish session expiry from a genuinely missing offering
        if "sign_in" in page_text or "users/sign_in" in page_text:
            raise SessionExpiredError()
        raise OfferingNotFoundError("Could not find company data on page -- slug may be invalid")

    company_id = cid_match.group(1)

    # Parse CSRF token from <meta name="csrf-token" content="...">
    csrf_match = re.search(r'name="csrf-token"\s+content="([^"]+)"', page_text)
    csrf_token = csrf_match.group(1) if csrf_match else ""

    return {"company_id": company_id, "csrf_token": csrf_token}


def _fetch_feed_items(base_url, offering_slug, company_id, csrf_token, headers, limit):
    """Fetch posts via the feed_items API, paginating as needed."""
    all_items = []
    page_num = 1

    feed_headers = {
        **headers,
        "Accept": "application/json, text/plain, */*",
        "X-Requested-With": "XMLHttpRequest",
        "Referer": f"{base_url}/{offering_slug}/updates",
    }
    if csrf_token:
        feed_headers["X-CSRF-Token"] = csrf_token

    while len(all_items) < limit:
        feed_resp = curl_requests.get(
            f"{base_url}/-/feed_items",
            params={
                "page": page_num,
                "filter[type]": "FeedItem::Update,FeedItem::Note,FeedItem::Spotlight,FeedItem::Investment",
                "filter[sort_algorithm]": "reverse_chronological",
                "filter[subject_id]": company_id,
                "filter[subject_type]": "Company",
                "filter[published]": "true",
            },
            headers=feed_headers,
            impersonate="chrome131",
            timeout=30,
        )

        # Session expiry on the API call
        if feed_resp.status_code in (401, 403):
            raise SessionExpiredError()

        if feed_resp.status_code != 200:
            break

        try:
            data = feed_resp.json()
        except (json.JSONDecodeError, ValueError):
            break

        items = data.get("data", [])
        if not items:
            break

        all_items.extend(items)

        # Stop if no next page
        meta = data.get("meta", {})
        if meta.get("next") is None:
            break

        page_num += 1

    # Trim to requested limit
    return all_items[:limit]

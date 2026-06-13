import json
import re
import time
from urllib.parse import urlparse

from bs4 import BeautifulSoup
from curl_cffi import requests


def run(headers, user_input):
    """Extract visual/media assets and document links from a Wefunder offering page."""
    base_url = BASE_URL

    # --- Input validation ---
    offering_slug = (user_input.get("offering_slug") or "").strip()
    offering_url = (user_input.get("offering_url") or "").strip()

    if not offering_slug and not offering_url:
        return {"status_code": 400, "body": {"error": "invalid_input", "message": "Either offering_slug or offering_url is required"}}

    if not offering_slug and offering_url:
        parsed = urlparse(offering_url)
        path = parsed.path.strip("/")
        offering_slug = path.split("/")[0] if path else ""

    if not offering_slug:
        return {"status_code": 400, "body": {"error": "invalid_input", "message": "Could not extract offering slug from URL"}}

    # --- Fetch main offering page ---
    resp = _fetch(f"{base_url}/{offering_slug}", headers)

    # Session-expired detection
    final_url = str(resp.url) if resp.url else ""
    if resp.status_code == 401 or "/login" in final_url or "/sessions/new" in final_url:
        return {"status_code": 401, "body": {"error": "session_expired"}}

    if resp.status_code == 404:
        return {"status_code": 404, "body": {"error": "offering_not_found"}}

    if resp.status_code != 200:
        return {"status_code": resp.status_code, "body": {"error": f"Unexpected status {resp.status_code}"}}

    html = resp.text

    # Extra guard: if the page is a login form rather than an offering page
    if '<form' in html and 'action="/sessions"' in html and 'id="js-video"' not in html:
        return {"status_code": 401, "body": {"error": "session_expired"}}

    soup = BeautifulSoup(html, "html.parser")

    # --- Hero media ---
    hero = _extract_hero(soup)

    # --- Logo (OG card image) ---
    logo_url = None
    og_img = soup.find("meta", property="og:image")
    if og_img and og_img.get("content"):
        logo_url = og_img["content"]

    # --- Gallery images from pitch body ---
    gallery_images = _extract_gallery(soup)

    # --- Social links ---
    social_links = _extract_social_links(soup)

    # --- Press links ---
    press_links = _extract_press_links(soup)

    # --- Fetch Details tab for documents ---
    details_resp = _fetch(f"{base_url}/{offering_slug}/details", headers)

    documents = []
    if details_resp.status_code == 200:
        details_soup = BeautifulSoup(details_resp.text, "html.parser")
        documents = _extract_documents(details_soup)

    return {
        "status_code": 200,
        "body": {
            "offering_slug": offering_slug,
            "hero": hero,
            "logo_url": logo_url,
            "gallery_images": gallery_images,
            "documents": documents,
            "social_links": social_links,
            "press_links": press_links,
        },
    }


# === PRIVATE ===

# Wefunder uses Cloudflare bot protection.  Rotate impersonation profiles
# on 403 to find one the WAF accepts from this IP.
_IMPERSONATE_PROFILES = ["safari17_0", "safari15_5", "safari17_2_ios", "chrome120"]


def _fetch(url, headers, retries=3):
    """GET with automatic impersonation-profile rotation on Cloudflare 403."""
    last_resp = None
    for attempt in range(retries):
        profile = _IMPERSONATE_PROFILES[attempt % len(_IMPERSONATE_PROFILES)]
        resp = requests.get(
            url,
            headers=headers,
            impersonate=profile,
            timeout=30,
            allow_redirects=True,
        )
        if resp.status_code != 403:
            return resp
        last_resp = resp
        if attempt < retries - 1:
            time.sleep(1)
    return last_resp  # return last 403 if all retries fail


def _normalize_url(url):
    """Ensure protocol-relative URLs get https."""
    if not url:
        return url
    url = url.strip()
    if url.startswith("//"):
        return f"https:{url}"
    return url


def _extract_hero(soup):
    """Extract the hero media (video or image) from above the fold."""
    hero = {"type": None, "media_url": None, "poster_url": None, "width": None, "height": None}

    # Look for the wf-video component (div#wf-video with turbo-mount props)
    video_el = soup.find(id="wf-video")
    if video_el:
        for attr_name, attr_val in video_el.attrs.items():
            if "props-value" in attr_name:
                try:
                    props = json.loads(attr_val)
                    video_url = props.get("video", "")
                    if video_url:
                        video_url = _normalize_url(video_url)
                        # Classify video source
                        if "youtube.com" in video_url or "youtu.be" in video_url:
                            hero["type"] = "youtube"
                        elif "vimeo.com" in video_url:
                            hero["type"] = "vimeo"
                        else:
                            hero["type"] = "video"
                        hero["media_url"] = video_url
                        # Best poster image (highest resolution available)
                        poster = (
                            props.get("cover1264")
                            or props.get("cover1000")
                            or props.get("cover350")
                            or props.get("cover")
                        )
                        hero["poster_url"] = _normalize_url(poster)
                except (json.JSONDecodeError, TypeError):
                    pass
                break

    # If no video, check for a hero image (header_media_photo or custom card)
    if not hero["media_url"]:
        js_video = soup.find(id="js-video")
        if js_video:
            img = js_video.find("img")
            if img and img.get("src"):
                hero["type"] = "image"
                hero["media_url"] = _normalize_url(img["src"])
                if img.get("width"):
                    try:
                        hero["width"] = int(img["width"])
                    except ValueError:
                        pass
                if img.get("height"):
                    try:
                        hero["height"] = int(img["height"])
                    except ValueError:
                        pass

    # Last fallback: OG image meta tags
    if not hero["media_url"]:
        og_img = soup.find("meta", property="og:image")
        if og_img and og_img.get("content"):
            hero["type"] = "image"
            hero["media_url"] = og_img["content"]
            og_w = soup.find("meta", property="og:image:width")
            og_h = soup.find("meta", property="og:image:height")
            if og_w and og_w.get("content"):
                try:
                    hero["width"] = int(og_w["content"])
                except ValueError:
                    pass
            if og_h and og_h.get("content"):
                try:
                    hero["height"] = int(og_h["content"])
                except ValueError:
                    pass

    return hero


def _extract_gallery(soup):
    """Extract gallery images from <figure> tags in the pitch body."""
    images = []
    figures = soup.find_all("figure", attrs={"data-id": True})
    for fig in figures:
        img = fig.find("img")
        if not img or not img.get("src"):
            continue
        url = _normalize_url(img["src"])
        alt = img.get("alt", "").strip() or None
        width = None
        height = None
        if img.get("width"):
            try:
                width = int(img["width"])
            except ValueError:
                pass
        if img.get("height"):
            try:
                height = int(img["height"])
            except ValueError:
                pass
        images.append({"url": url, "alt_text": alt, "width": width, "height": height})
    return images


def _extract_social_links(soup):
    """Extract social links from the offering page."""
    social = {
        "website": None,
        "twitter": None,
        "linkedin": None,
        "instagram": None,
        "youtube": None,
        "facebook": None,
    }

    # 1) Parse social icons (fa-brands) from the visible page
    for a_tag in soup.find_all("a", attrs={"target": "_blank"}, href=True):
        href = a_tag["href"].strip()
        icon = a_tag.find("i")
        icon_class = " ".join(icon.get("class", [])).lower() if icon else ""

        if "fa-linkedin" in icon_class and not social["linkedin"]:
            social["linkedin"] = href
        elif "fa-twitter" in icon_class and not social["twitter"]:
            social["twitter"] = href
        elif "fa-youtube" in icon_class and not social["youtube"]:
            social["youtube"] = href
        elif "fa-instagram" in icon_class and not social["instagram"]:
            social["instagram"] = href
        elif "fa-facebook" in icon_class and not social["facebook"]:
            social["facebook"] = href

    # 2) Website link (appears as class 'wf-link-inline-muted' near the company name)
    website_link = soup.find("a", class_=lambda c: c and "wf-link-inline-muted" in c)
    if website_link and website_link.get("href"):
        href = website_link["href"].strip()
        # Ignore empty/placeholder URLs and self-referential links
        if href and href not in ("https://", "http://", "/") and "wefunder.com" not in href:
            social["website"] = href

    # 3) Fill gaps from JSON-LD sameAs data
    ld_script = soup.find("script", type="application/ld+json")
    if ld_script and ld_script.string:
        try:
            ld_data = json.loads(ld_script.string)
            for item in ld_data.get("@graph", []):
                if item.get("@type") == "Organization":
                    for url in item.get("sameAs", []):
                        ul = url.lower()
                        if "linkedin.com" in ul and not social["linkedin"]:
                            social["linkedin"] = url
                        elif ("twitter.com" in ul or "x.com" in ul) and not social["twitter"]:
                            social["twitter"] = url
                        elif "youtube.com" in ul and not social["youtube"]:
                            social["youtube"] = url
                        elif "instagram.com" in ul and not social["instagram"]:
                            social["instagram"] = url
                        elif "facebook.com" in ul and not social["facebook"]:
                            social["facebook"] = url
                        elif not social["website"]:
                            social["website"] = url
                    break
        except (json.JSONDecodeError, TypeError):
            pass

    return social


def _extract_documents(soup):
    """Extract documents from the Details tab HTML."""
    documents = []
    seen_urls = set()

    for a_tag in soup.find_all("a", href=True):
        href = a_tag["href"].strip()
        if href in seen_urls:
            continue
        label = a_tag.get_text(strip=True)
        if not label:
            continue

        doc = None

        # Investment-term PDFs  (template preview links)
        if "/templates/" in href and "format=pdf" in href:
            ll = label.lower()
            if "safe" in ll:
                doc_type = "safe"
            elif "subscription" in ll or "spv" in ll:
                doc_type = "investment_terms"
            elif "pitch" in ll and "deck" in ll:
                doc_type = "pitch_deck"
            else:
                doc_type = "other"
            doc = {"type": doc_type, "label": label, "url": _normalize_url(href), "mime_type": "application/pdf"}

        # Financials PDF  (remote_files uploads)
        elif "remote_files" in href and href.lower().endswith(".pdf"):
            clean_label = re.sub(r"\.pdf$", "", label, flags=re.IGNORECASE).strip()
            doc = {"type": "financials", "label": clean_label or "Financials", "url": _normalize_url(href), "mime_type": "application/pdf"}

        # SEC filing
        elif "sec.gov" in href:
            doc = {"type": "form_c", "label": label or "SEC Filing", "url": href, "mime_type": None}

        # Pitch deck hosted externally
        elif any(kw in href.lower() for kw in ["drive.google.com", "dropbox.com", "docsend.com"]):
            doc = {"type": "pitch_deck", "label": label or "Pitch Deck", "url": _normalize_url(href), "mime_type": None}

        if doc:
            seen_urls.add(href)
            documents.append(doc)

    # Also check the formC embedded data for the SEC live_url
    for el in soup.find_all(True):
        for attr_name, attr_val in el.attrs.items():
            if "wf-financial-chart" in attr_name and "props-value" in attr_name:
                try:
                    props = json.loads(attr_val) if isinstance(attr_val, str) else None
                    if props:
                        live_url = props.get("formC", {}).get("live_url")
                        if live_url and live_url not in seen_urls:
                            documents.append({
                                "type": "form_c",
                                "label": "SEC Form C Filing",
                                "url": live_url,
                                "mime_type": None,
                            })
                            seen_urls.add(live_url)
                except (json.JSONDecodeError, TypeError):
                    pass

    return documents


def _extract_press_links(soup):
    """Extract press / 'as featured in' links from the pitch body."""
    press = []

    # Look for headings that signal a press section
    for heading in soup.find_all(["h2", "h3", "h4"]):
        text = heading.get_text(strip=True).lower()
        if any(kw in text for kw in ["press", "featured in", "as seen in", "in the news"]):
            for sibling in heading.find_next_siblings():
                if sibling.name in ["h2", "h3", "h4"]:
                    break
                for a in sibling.find_all("a", href=True):
                    href = a["href"]
                    label = a.get_text(strip=True)
                    if label and href.startswith("http") and "wefunder.com" not in href:
                        outlet = urlparse(href).netloc.replace("www.", "")
                        press.append({"outlet": outlet, "url": href, "title": label or None})

    return press

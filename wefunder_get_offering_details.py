import json
import re
import html as htmlmod
from datetime import datetime, timezone
from urllib.parse import urlparse

from curl_cffi import requests


def run(headers, user_input):
    """Read the full detail page for a Wefunder offering and return structured underwriting data."""
    base_url = BASE_URL

    # --- Input: accept slug or offering_url ---
    slug = user_input.get("slug", "").strip().strip("/")
    offering_url = user_input.get("offering_url", "").strip()

    if not slug and offering_url:
        parsed = urlparse(offering_url)
        slug = parsed.path.strip("/").split("/")[0] if parsed.path else ""
    if not slug:
        return {"status_code": 400, "body": {"error": "Either slug or offering_url is required"}}

    # --- Fetch the main overview page ---
    main_resp = _fetch(f"{base_url}/{slug}", headers)
    if main_resp is None:
        return {"status_code": 401, "body": {"error": "Session expired or blocked by Cloudflare"}}
    if main_resp.status_code != 200:
        return {"status_code": main_resp.status_code, "body": {"error": f"Failed to load offering page (HTTP {main_resp.status_code})"}}

    main_html = main_resp.text

    # --- Fetch the details page (Form C data, financials, risks) ---
    details_html = None
    details_resp = _fetch(f"{base_url}/{slug}/details", {**headers, "Referer": f"{base_url}/{slug}"})
    if details_resp and details_resp.status_code == 200:
        details_html = details_resp.text

    # ================================================================
    # PARSE MAIN PAGE
    # ================================================================
    props_list = _extract_turbo_props(main_html)

    # -- Company name & blurb --
    company_name = _meta(main_html, "og:title")
    if company_name:
        company_name = re.sub(r"^Invest in\s+", "", company_name)
        company_name = company_name.split("|")[0].strip()
    blurb = _meta(main_html, "og:description") or _meta(main_html, "Description")

    # -- Fundraise metadata from tab-controller props --
    tab_props = _find_props(props_list, "fundraiseState")
    fundraise_state = tab_props.get("fundraiseState") if tab_props else None
    offering_type = tab_props.get("offeringType") if tab_props else None
    fundraise_structure = tab_props.get("fundraiseStructure") if tab_props else None
    company_id = tab_props.get("companyId") if tab_props else None

    # -- Security type and valuation from INVESTMENT TERMS section --
    security_type = _extract_text_block(main_html, r'<span[^>]*class="[^"]*wf-col-gray-800[^"]*font-medium[^"]*"[^>]*>\s*([^<]+)')
    if not security_type:
        security_type = _extract_text_block(main_html, r'INVESTMENT TERMS.*?<span[^>]*font-medium[^>]*>\s*([^<]+)')

    terms_type = None
    terms_value = None
    valuation_cap_usd = None
    discount_pct = None

    # Extract valuation values (early bird / regular)
    valuation_section = _section_after(main_html, "INVESTMENT TERMS", 2000)
    if valuation_section:
        # Look for early bird valuation (current active one)
        eb_match = re.search(
            r'<span[^>]*font-medium[^>]*>\s*(\$[\d,.]+[MKBmkb]?)\s*<span[^>]*>\s*(pre-money valuation)',
            valuation_section,
        )
        if eb_match:
            terms_value = eb_match.group(1).strip()
            terms_type = eb_match.group(2).strip()
            valuation_cap_usd = _parse_dollar(terms_value)

        # Look for the struck-through (regular) valuation
        regular_match = re.search(
            r'line-through[^>]*>\s*&nbsp;(\$[\d,.]+[MKBmkb]?)&nbsp;',
            valuation_section,
        )
        if regular_match and valuation_cap_usd:
            regular_val = _parse_dollar(regular_match.group(1).strip())
            if regular_val and regular_val > valuation_cap_usd:
                discount_pct = round((1 - valuation_cap_usd / regular_val) * 100, 1)

    # -- Min investment --
    invest_props = _find_props(props_list, "minPurchase")
    min_investment_usd = None
    if invest_props:
        min_investment_usd = _parse_dollar(invest_props.get("minPurchase", ""))

    # -- Raised amount --
    raised_usd = None
    raised_match = re.search(
        r'id="amount-raised-primary-value"[^>]*>\s*(\$[\d,]+(?:\.\d+)?)',
        main_html,
    )
    if raised_match:
        raised_usd = _parse_dollar(raised_match.group(1))

    # -- Investor count --
    investor_count = None
    inv_match = re.search(r'raised from ([\d,]+)\+?\s*investors', main_html)
    if inv_match:
        investor_count = int(inv_match.group(1).replace(",", ""))

    # -- Percent funded (progress bar width) --
    percent_funded = None
    pct_match = re.search(r'class="filled"[^>]*style="width:([\d.]+)%"', main_html)
    if pct_match:
        percent_funded = round(float(pct_match.group(1)), 2)

    # -- Team --
    team = []
    for p in props_list:
        if "userTitle" in p and "userBio" in p and "companyId" in p:
            linkedin = p.get("userLinkedIn") or p.get("userPersonalUrl") or None
            if linkedin and "linkedin" not in linkedin.lower():
                linkedin = None
            avatar = p.get("userAvatar") or None
            if avatar:
                avatar = avatar if avatar.startswith("http") else f"https:{avatar}"
                # Strip query-string cache-busters for a cleaner URL
                avatar = avatar.split("?")[0]
            team.append({
                "name": p.get("userFullName"),
                "role": p.get("userTitle"),
                "bio_short": p.get("userBio"),
                "linkedin_url": linkedin,
                "photo_url": avatar,
            })

    # -- Perks --
    perks = []
    perk_props = _find_props(props_list, "perkList")
    if perk_props and "perkList" in perk_props:
        for pk in perk_props["perkList"]:
            perks.append({
                "min_amount_usd": pk.get("qualifying_amount"),
                "description": pk.get("description"),
            })

    # -- Tab counts (Posts=investor_updates_count, Ask a Question=qa_count) --
    investor_updates_count = _extract_tab_count(main_html, "Posts")
    qa_count = _extract_tab_count(main_html, "Ask a Question")

    # -- Highlights / categories --
    categories = []
    highlights_section = _section_after(main_html, ">Highlights<", 3000)
    if highlights_section:
        categories = re.findall(
            r'class="text-lg font-semibold[^"]*"[^>]*>([^<]+)<',
            highlights_section,
        )
        categories = list(dict.fromkeys(c.strip() for c in categories if c.strip()))

    # -- Full pitch HTML --
    full_pitch_html = _extract_pitch_html(main_html)

    # -- Status --
    status = fundraise_state  # "open", "closed", etc.

    # ================================================================
    # PARSE DETAILS PAGE (Form C)
    # ================================================================
    target_raise_min_usd = None
    target_raise_max_usd = None
    deadline_iso = None
    days_remaining = None
    sec_filing_url = None
    risk_factors_text = None
    use_of_funds_text = None
    financials = {
        "last_year_revenue_usd": None,
        "last_year_net_income_usd": None,
        "total_assets_usd": None,
        "total_liabilities_usd": None,
        "cash_usd": None,
    }

    if details_html:
        details_props = _extract_turbo_props(details_html)
        form_c = None
        for p in details_props:
            if "formC" in p and isinstance(p["formC"], dict):
                form_c = p["formC"]
                break

        if form_c:
            # Offering amounts
            oa = form_c.get("offering_amount")
            target_raise_min_usd = float(oa) if oa else None
            ma = form_c.get("maximum_offering_amount")
            target_raise_max_usd = float(ma) if ma else None

            # Deadline
            dl = form_c.get("deadline_date")
            if dl:
                deadline_iso = dl[:10]  # YYYY-MM-DD
                try:
                    dl_date = datetime.strptime(deadline_iso, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                    now = datetime.now(timezone.utc)
                    diff = (dl_date - now).days
                    days_remaining = max(diff, 0)
                except Exception:
                    pass

            # SEC filing URL
            sec_filing_url = form_c.get("live_url") or None

            # Security type fallback
            if not security_type:
                security_type = form_c.get("security_offered_type")

            # Financials
            def _fval(key):
                v = form_c.get(key)
                return float(v) if v is not None else None

            rev = _fval("revenue_most_recent_fiscal_year")
            ni = _fval("net_income_most_recent_fiscal_year")
            ta = _fval("total_asset_most_recent_fiscal_year")
            cash = _fval("cash_equi_most_recent_fiscal_year")
            st_debt = _fval("short_term_debt_most_recent_fiscal_year")
            lt_debt = _fval("long_term_debt_most_recent_fiscal_year")
            total_liab = None
            if st_debt is not None or lt_debt is not None:
                total_liab = (st_debt or 0) + (lt_debt or 0)

            financials = {
                "last_year_revenue_usd": rev,
                "last_year_net_income_usd": ni,
                "total_assets_usd": ta,
                "total_liabilities_usd": total_liab,
                "cash_usd": cash,
            }

        # Risk factors from rendered HTML
        risk_factors_text = _extract_risks_text(details_html)

        # Use of funds from rendered HTML
        use_of_funds_text = _extract_use_of_funds_text(details_html)

    # ================================================================
    # BUILD RESULT
    # ================================================================
    result = {
        "company_name": company_name,
        "slug": slug,
        "blurb": blurb,
        "full_pitch_html": full_pitch_html,
        "categories": categories if categories else None,
        "security_type": security_type,
        "terms_type": terms_type,
        "terms_value": terms_value,
        "valuation_cap_usd": valuation_cap_usd,
        "discount_pct": discount_pct,
        "min_investment_usd": min_investment_usd,
        "target_raise_min_usd": target_raise_min_usd,
        "target_raise_max_usd": target_raise_max_usd,
        "raised_usd": raised_usd,
        "investor_count": investor_count,
        "percent_funded": percent_funded,
        "deadline_iso": deadline_iso,
        "days_remaining": days_remaining,
        "status": status,
        "offering_url": f"{base_url}/{slug}",
        "sec_filing_url": sec_filing_url,
        "risk_factors_text": risk_factors_text,
        "use_of_funds_text": use_of_funds_text,
        "team": team if team else None,
        "financials": financials,
        "perks": perks if perks else None,
        "investor_updates_count": investor_updates_count,
        "qa_count": qa_count,
    }

    return {"status_code": 200, "body": result}


# === PRIVATE ===


# ================================================================
# HELPER FUNCTIONS
# ================================================================

_IMPERSONATE_PROFILES = ["safari17_0", "safari15_5", "chrome119", "safari17_2_ios"]


def _fetch(url, hdrs):
    """Fetch a URL, trying multiple TLS fingerprints to bypass Cloudflare."""
    for profile in _IMPERSONATE_PROFILES:
        try:
            resp = requests.get(url, headers=hdrs, impersonate=profile, timeout=30)
            if resp.status_code == 200 and "Just a moment" not in resp.text[:500]:
                return resp
            if resp.status_code == 200:
                continue  # Cloudflare challenge page, try next profile
        except Exception:
            continue
    # All profiles failed — return the last response for error handling
    # or None if we suspect auth issues
    try:
        resp = requests.get(url, headers=hdrs, impersonate=_IMPERSONATE_PROFILES[0], timeout=30)
        if _is_login_or_challenge(resp):
            return None
        return resp
    except Exception:
        return None


def _is_login_or_challenge(resp):
    """Detect Cloudflare challenge or login redirect."""
    text = resp.text[:1000] if resp.text else ""
    if "Just a moment" in text:
        return True
    if "Enable JavaScript and cookies to continue" in text:
        return True
    if resp.status_code in (401, 403) and ("challenge" in text.lower() or "error code: 1010" in text.lower()):
        return True
    # Check for login redirect
    if resp.status_code in (301, 302, 303, 307, 308):
        loc = resp.headers.get("location", "")
        if "/login" in loc or "/sign_in" in loc:
            return True
    return False


def _extract_turbo_props(html_text):
    """Extract all turbo-mount props-value JSON objects from HTML."""
    results = []
    for m in re.finditer(r'data-turbo-mount[^=]*-props-value="([^"]+)"', html_text):
        try:
            decoded = htmlmod.unescape(m.group(1))
            results.append(json.loads(decoded))
        except (json.JSONDecodeError, ValueError):
            pass
    return results


def _find_props(props_list, key):
    """Find the first props dict containing a given key."""
    for p in props_list:
        if key in p:
            return p
    return None


def _meta(html_text, name):
    """Extract a meta tag content by name or property."""
    m = re.search(
        rf'<meta\s+(?:name|property)="{re.escape(name)}"\s+content="([^"]*)"',
        html_text,
    )
    if not m:
        m = re.search(
            rf'<meta\s+content="([^"]*)"\s+(?:name|property)="{re.escape(name)}"',
            html_text,
        )
    return htmlmod.unescape(m.group(1)).strip() if m else None


def _extract_text_block(html_text, pattern):
    """Extract first match of a regex from HTML, stripping tags."""
    m = re.search(pattern, html_text, re.DOTALL | re.IGNORECASE)
    if m:
        text = m.group(1)
        text = re.sub(r"<[^>]+>", " ", text)
        return re.sub(r"\s+", " ", text).strip()
    return None


def _section_after(html_text, marker, length=3000):
    """Return a chunk of HTML starting at the marker."""
    idx = html_text.find(marker)
    if idx < 0:
        return None
    return html_text[idx : idx + length]


def _parse_dollar(text):
    """Parse a dollar string like '$49.6M' or '$17,251,170' into a number."""
    if not text:
        return None
    text = text.replace("$", "").replace(",", "").strip()
    multiplier = 1
    if text.upper().endswith("B"):
        multiplier = 1_000_000_000
        text = text[:-1]
    elif text.upper().endswith("M"):
        multiplier = 1_000_000
        text = text[:-1]
    elif text.upper().endswith("K"):
        multiplier = 1_000
        text = text[:-1]
    try:
        return float(text) * multiplier
    except ValueError:
        return None


def _extract_tab_count(html_text, tab_label):
    """Extract the count number shown next to a tab label."""
    # Pattern: tab label text followed by a number (possibly in a sibling element)
    pattern = rf">{re.escape(tab_label)}\s*</(?:button|a|span|div)>\s*(?:<[^>]+>\s*)*?(\d[\d,]*)"
    m = re.search(pattern, html_text)
    if m:
        return int(m.group(1).replace(",", ""))
    # Fallback: label and number close together
    pattern2 = rf"{re.escape(tab_label)}\s*(?:</[^>]+>\s*(?:<[^>]+>\s*)*)?(\d[\d,]*)"
    m2 = re.search(pattern2, html_text)
    if m2:
        return int(m2.group(1).replace(",", ""))
    return None


def _extract_pitch_html(html_text):
    """Extract the long-form pitch/memo HTML from the 'story-styles' container."""
    # The memo lives inside <div class="story-styles js-footnote-container">
    marker = 'class="story-styles js-footnote-container"'
    start_tag = html_text.find(marker)
    if start_tag < 0:
        return None

    content_start = html_text.find(">", start_tag) + 1

    # Track nested divs to find the matching closing </div>
    depth = 1
    pos = content_start
    content_end = len(html_text)
    while depth > 0 and pos < len(html_text):
        next_open = html_text.find("<div", pos)
        next_close = html_text.find("</div>", pos)
        if next_close < 0:
            break
        if next_open >= 0 and next_open < next_close:
            depth += 1
            pos = next_open + 4
        else:
            depth -= 1
            if depth == 0:
                content_end = next_close
                break
            pos = next_close + 6

    raw_html = html_text[content_start:content_end].strip()
    if not raw_html:
        return None

    # Return the raw inner HTML (figures, paragraphs, lists)
    return raw_html


def _extract_risks_text(html_text):
    """Extract risk factors text from the details page."""
    idx = html_text.find(">Risks<")
    if idx < 0:
        idx = html_text.find(">Risk Factors<")
    if idx < 0:
        return None

    # Skip past the heading tag
    idx = html_text.find(">", idx + 1)
    if idx < 0:
        return None
    idx += 1  # move past the '>'

    # Find the end of the risks section (next <h3> or major section)
    end_markers = [">Other Disclosures<", ">The Board of Directors<", ">Capital Structure<", ">Use of Funds<"]
    end_idx = len(html_text)
    for marker in end_markers:
        eidx = html_text.find(marker, idx)
        if eidx > 0 and eidx < end_idx:
            end_idx = eidx

    section = html_text[idx:end_idx]
    clean = re.sub(r"<[^>]+>", " ", section)
    clean = htmlmod.unescape(clean)
    # Collapse whitespace: multiple spaces/newlines into single newline
    clean = re.sub(r"[ \t]+", " ", clean)
    clean = re.sub(r"(\s*\n\s*)+", "\n", clean).strip()
    # Remove leftover heading text and leading numbering noise
    clean = re.sub(r"^(?:Risks?\s*(?:Factors?)?\s*\n*)", "", clean).strip()
    # Normalize numbered risk items: "1 We rely..." -> "1. We rely..."
    clean = re.sub(r"(?m)^(\d+)\s+", r"\1. ", clean)
    return clean if clean else None


def _extract_use_of_funds_text(html_text):
    """Extract use of funds text from the details page."""
    idx = html_text.find(">Use of Funds<")
    if idx < 0:
        return None

    # Skip past the heading tag
    idx = html_text.find(">", idx + 1)
    if idx < 0:
        return None
    idx += 1

    # End at Capital Structure or next major section
    end_markers = [">Capital Structure<", ">The Board of Directors<", ">Other Disclosures<"]
    end_idx = len(html_text)
    for marker in end_markers:
        eidx = html_text.find(marker, idx)
        if eidx > 0 and eidx < end_idx:
            end_idx = eidx

    section = html_text[idx:end_idx]
    clean = re.sub(r"<[^>]+>", " ", section)
    clean = htmlmod.unescape(clean)
    clean = re.sub(r"[ \t]+", " ", clean)
    clean = re.sub(r"(\s*\n\s*)+", "\n", clean).strip()
    clean = re.sub(r"^Use of Funds\s*\n*", "", clean).strip()
    return clean if clean else None

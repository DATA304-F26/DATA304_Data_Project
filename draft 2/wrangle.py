#!/usr/bin/env python3

from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
from datetime import datetime, timezone

import pandas as pd

LIST_SEP = " | "
DROP_COLUMNS = {"images", "thumbnail"}

US_STATES = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR", "california": "CA",
    "colorado": "CO", "connecticut": "CT", "delaware": "DE", "district of columbia": "DC",
    "florida": "FL", "georgia": "GA", "hawaii": "HI", "idaho": "ID", "illinois": "IL",
    "indiana": "IN", "iowa": "IA", "kansas": "KS", "kentucky": "KY", "louisiana": "LA",
    "maine": "ME", "maryland": "MD", "massachusetts": "MA", "michigan": "MI", "minnesota": "MN",
    "mississippi": "MS", "missouri": "MO", "montana": "MT", "nebraska": "NE", "nevada": "NV",
    "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM", "new york": "NY",
    "north carolina": "NC", "north dakota": "ND", "ohio": "OH", "oklahoma": "OK", "oregon": "OR",
    "pennsylvania": "PA", "rhode island": "RI", "south carolina": "SC", "south dakota": "SD",
    "tennessee": "TN", "texas": "TX", "utah": "UT", "vermont": "VT", "virginia": "VA",
    "washington": "WA", "west virginia": "WV", "wisconsin": "WI", "wyoming": "WY",
}
COUNTRIES = {"united states": "US", "usa": "US", "united states of america": "US", "canada": "CA",
             "mexico": "MX", "united kingdom": "GB"}
CURRENCIES = {"$": "USD", "US$": "USD", "CA$": "CAD", "C$": "CAD", "€": "EUR", "£": "GBP"}

AMENITY_FLAGS = {
    "has_pool": r"\bpool\b(?! table)",
    "has_hot_tub": r"hot tub|jacuzzi",
    "has_wifi": r"wifi|wi-fi",
    "has_kitchen": r"\bkitchen\b",
    "has_parking": r"parking",
    "has_air_conditioning": r"air conditioning|\ba/c\b",
    "has_washer": r"\bwasher\b",
    "has_gym": r"\bgym\b|exercise equipment",
}

OUTPUT_COLUMNS = [
    "platform", "id", "url", "name", "headline", "property_type", "room_type",
    "city", "state", "country", "neighborhood", "address_line", "postal_code", "lat", "lng",
    "is_exact_location", "guests", "bedrooms", "beds", "baths",
    "price_amount", "price_nights", "price_currency", "price_original_amount",
    "price_avg_nightly_listed", "rating", "rating_label", "reviews_count",
    "is_guest_favorite", "is_superhost",
    "host_name", "host_is_verified", "host_rating_count", "host_rating_avg", "host_years_hosting",
    "host_response_rate_pct", "host_response_time", "host_profile_url", "host_info_text",
    "amenities_count", *AMENITY_FLAGS, "amenities",
    "check_in_time", "checkout_time", "pets_allowed", "house_rules",
    "description", "seo_title", "meta_description", "amenities_by_category",
    "search_city", "search_rank", "check_in", "check_out", "scraped_at", "source_file",
]


# ---------------------------------------------------------------- loading
def load_rows(paths: list[str]) -> list[dict]:
    rows: list[dict] = []
    for path in paths:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
        try:
            data = json.loads(text)
            data = data if isinstance(data, list) else [data]
        except json.JSONDecodeError:  # JSON Lines
            data = [json.loads(line) for line in text.splitlines() if line.strip()]
        base = os.path.basename(path)
        m = re.search(r"(\d{4}-\d{2}-\d{2})_(\d{2})-(\d{2})-(\d{2})", base)
        file_ts = f"{m.group(1)}T{m.group(2)}:{m.group(3)}:{m.group(4)}Z" if m else None
        for r in data:
            if isinstance(r, dict):
                r["_source_file"] = base
                r["_file_ts"] = file_ts
                rows.append(r)
    return rows


def blank(v) -> bool:
    return v is None or v == "" or v == [] or v == {} or (isinstance(v, float) and v != v)


def norm_text(s):
    if not isinstance(s, str):
        return s
    s = s.replace("\u202f", " ").replace("\xa0", " ")
    s = html.unescape(s)
    return re.sub(r"[ \t]+", " ", s).strip()


def to_float(v):
    if blank(v):
        return None
    try:
        return float(str(v).replace(",", ""))
    except ValueError:
        m = re.search(r"\d[\d,]*\.?\d*", str(v))
        return float(m.group(0).replace(",", "")) if m else None


def to_int(v):
    f = to_float(v)
    return int(f) if f is not None else None


def parse_price(label):
    """'$1,392' -> (1392.0, 'USD'). Returns (None, None) when nothing numeric."""
    if blank(label):
        return None, None
    label = norm_text(str(label))
    m = re.search(r"(CA\$|C\$|US\$|\$|€|£)\s*([\d,]+(?:\.\d+)?)", label)
    if m:
        return float(m.group(2).replace(",", "")), CURRENCIES.get(m.group(1))
    return to_float(label), None


def nights_from(qualifier):
    if blank(qualifier):
        return None
    m = re.search(r"(\d+)\s*nights?", str(qualifier), re.I)
    if m:
        return int(m.group(1))
    if re.search(r"night", str(qualifier), re.I):
        return 1  # per night
    return None


def coalesce_bool(*vals):
    vals = [v for v in vals if isinstance(v, bool)]
    if not vals:
        return None
    return any(vals)


def parse_time(text):
    m = re.search(r"(\d{1,2})(?::(\d{2}))?\s*([AP]M)", text or "", re.I)
    if not m:
        return None
    return datetime.strptime(f"{m.group(1)}:{m.group(2) or '00'} {m.group(3).upper()}", "%I:%M %p").strftime("%H:%M")


def parse_room_counts(texts: list[str]):
    pool = " ; ".join(norm_text(t).lower() for t in texts if isinstance(t, str))
    bedrooms = beds = baths = guests = None
    m = re.search(r"(\d+(?:\.\d+)?)\s+bedrooms?\b", pool)
    if m:
        bedrooms = float(m.group(1))
    elif "studio" in pool:
        bedrooms = 0.0
    m = re.search(r"\b(\d+)\s+(?:[a-z-]+\s+)?beds?\b", pool)
    if m:
        beds = float(m.group(1))
    m = re.search(r"(\d+(?:\.\d+)?)\s+(?:shared\s+|private\s+)?bath(?:room)?s?\b", pool)
    if m:
        baths = float(m.group(1))
    elif re.search(r"half-bath", pool):
        baths = 0.5
    m = re.search(r"(\d+)\s+guests?\b", pool) or re.search(r"sleeps\s+(\d+)", pool)
    if m:
        guests = int(m.group(1))
    return bedrooms, beds, baths, guests


def split_location(subtitle):
    """'Nashville, Tennessee, United States' -> (city, state, country)."""
    if blank(subtitle):
        return None, None, None
    parts = [p.strip() for p in str(subtitle).split(",")]
    if len(parts) >= 3:
        return parts[0], parts[-2], parts[-1]
    if len(parts) == 2:
        return parts[0], None, parts[1]
    return parts[0], None, None


def norm_state(s):
    if blank(s):
        return None
    s = str(s).strip()
    return US_STATES.get(s.lower(), s.upper() if len(s) == 2 else s)


def norm_country(s):
    if blank(s):
        return None
    s = str(s).strip()
    return COUNTRIES.get(s.lower(), s.upper() if len(s) == 2 else s)


def clean_description(s):
    if not isinstance(s, str):
        return None
    s = re.sub(r"<\s*br\s*/?>", "\n", s, flags=re.I)
    s = re.sub(r"<[^>]+>", " ", s)
    s = norm_text(s)
    s = re.sub(r"\n\s*\n+", "\n\n", s)
    return s or None


def join_list(v):
    if isinstance(v, list):
        items = [norm_text(str(x)) for x in v if not blank(x)]
        return LIST_SEP.join(items) if items else None
    return norm_text(v) if not blank(v) else None



def clean_row(r: dict) -> dict:
    out: dict = {}
    host = r.get("host") if isinstance(r.get("host"), dict) else {}
    coord = r.get("coordinates") if isinstance(r.get("coordinates"), dict) else {}
    addr = r.get("address") if isinstance(r.get("address"), dict) else {}

    out["platform"] = r.get("platform")
    out["id"] = str(r["id"]) if not blank(r.get("id")) else None
    out["url"] = r.get("url")
    out["name"] = norm_text(r.get("name"))
    out["headline"] = norm_text(r.get("headline"))
    out["property_type"] = norm_text(r.get("propertyType"))
    out["room_type"] = r.get("roomType")

    city, state, country = split_location(r.get("locationSubtitle"))
    city = addr.get("city") or city
    state = addr.get("province") or state
    country = addr.get("countryCode") or country
    out["city"], out["state"], out["country"] = norm_text(city), norm_state(state), norm_country(country)
    out["neighborhood"] = norm_text(r.get("neighborhood"))
    out["address_line"] = norm_text(addr.get("full"))
    out["postal_code"] = addr.get("postalCode")
    out["lat"], out["lng"] = to_float(coord.get("lat")), to_float(coord.get("lng"))
    out["is_exact_location"] = r.get("isExactLocation") if isinstance(r.get("isExactLocation"), bool) else None

    texts = list(r.get("overviewItems") or []) + list(r.get("roomInfo") or [])
    bedrooms, beds, baths, guests = parse_room_counts(texts)
    out["guests"] = to_int(r.get("personCapacity")) or guests
    out["bedrooms"], out["beds"], out["baths"] = bedrooms, beds, baths

    amount, cur = parse_price(r.get("priceLabel"))
    orig, _ = parse_price(r.get("originalPriceLabel"))
    avg, _ = parse_price(r.get("avgNightlyLabel"))
    out["price_amount"], out["price_currency"] = amount, cur
    out["price_nights"] = nights_from(r.get("priceQualifier")) or to_int(r.get("nights"))
    out["price_original_amount"] = orig
    out["price_avg_nightly_listed"] = avg

    rating = to_float(r.get("rating"))
    out["rating"] = rating if rating is not None and 0 <= rating <= 10 else None
    out["rating_label"] = norm_text(r.get("ratingLabel"))
    out["reviews_count"] = to_int(r.get("reviewsCount"))

    badges = [b for b in (r.get("badges") or []) if isinstance(b, str)]
    out["is_guest_favorite"] = coalesce_bool(r.get("isGuestFavorite"), True if "Guest favorite" in badges else None)
    out["is_superhost"] = coalesce_bool(r.get("isSuperhost"), host.get("isSuperhost"),
                                        True if "Superhost" in badges else None)

    out["host_name"] = norm_text(host.get("name"))
    out["host_is_verified"] = host.get("isVerified") if isinstance(host.get("isVerified"), bool) else None
    out["host_rating_count"] = to_int(host.get("ratingCount"))
    out["host_rating_avg"] = to_float(host.get("ratingAverage"))
    out["host_years_hosting"] = to_int(host.get("yearsHosting"))
    out["host_response_rate_pct"] = to_float(re.search(r"(\d+(?:\.\d+)?)\s*%", str(host.get("responseRate") or "")).group(1)) \
        if re.search(r"(\d+(?:\.\d+)?)\s*%", str(host.get("responseRate") or "")) else None
    out["host_response_time"] = norm_text(host.get("responseTime"))
    out["host_profile_url"] = host.get("profileUrl")
    out["host_info_text"] = clean_description(r.get("hostInfo"))

    amenities = [norm_text(a) for a in (r.get("amenities") or []) if isinstance(a, str)]
    out["amenities_count"] = to_int(r.get("amenitiesCount")) if amenities else None
    blob = " ; ".join(amenities).lower()
    for col, pattern in AMENITY_FLAGS.items():
        out[col] = bool(re.search(pattern, blob)) if amenities else None
    out["amenities"] = LIST_SEP.join(amenities) if amenities else None

    rules = r.get("houseRules")
    if isinstance(rules, str):  # VRBO: one text blob
        rule_list = [norm_text(x) for x in rules.split("\n") if x.strip()]
    else:
        rule_list = [norm_text(x) for x in (rules or []) if isinstance(x, str)]
    ci = next((x for x in rule_list if re.search(r"check-?in", x, re.I)), None)
    co = next((x for x in rule_list if re.search(r"check-?out", x, re.I)), None)
    out["check_in_time"], out["checkout_time"] = parse_time(ci), parse_time(co)
    low = " ; ".join(rule_list).lower()
    out["pets_allowed"] = (False if re.search(r"no pets|pets not allowed", low)
                           else True if re.search(r"pets allowed|pets welcome", low) else None)
    out["house_rules"] = LIST_SEP.join(rule_list) if rule_list else None

    cats = r.get("categoryRatings")
    if isinstance(cats, dict):
        for k, v in cats.items():
            out[f"rating_{re.sub(r'[^a-z0-9]+', '_', str(k).lower()).strip('_')}"] = to_float(v)

    out["description"] = clean_description(r.get("description"))
    out["seo_title"] = norm_text(r.get("seoTitle"))
    out["meta_description"] = norm_text(r.get("metaDescription"))
    abc = r.get("amenitiesByCategory")
    out["amenities_by_category"] = json.dumps(abc, ensure_ascii=False) if isinstance(abc, dict) else None

    out["search_city"] = r.get("search_city")
    out["search_rank"] = to_int(r.get("search_rank"))
    out["check_in"], out["check_out"] = r.get("check_in"), r.get("check_out")
    out["scraped_at"] = r.get("scraped_at") or r.get("_file_ts")
    out["source_file"] = r.get("_source_file")
    return out



def wrangle(paths: list[str]):
    raw = load_rows(paths)
    report: list[str] = [f"Input files: {len(paths)}", f"Raw rows loaded: {len(raw)}"]
    seen_keys = set()
    for r in raw:
        seen_keys.update(r)
    handled = {"platform", "id", "url", "name", "headline", "propertyType", "roomType", "spaceType",
               "locationSubtitle", "neighborhood", "address", "coordinates", "isExactLocation",
               "overviewItems", "roomInfo", "personCapacity", "priceLabel", "originalPriceLabel",
               "avgNightlyLabel", "priceQualifier", "rating", "ratingLabel", "reviewsCount", "badges",
               "isGuestFavorite", "isSuperhost", "host", "hostInfo", "amenities", "amenitiesCount",
               "amenitiesByCategory", "houseRules", "categoryRatings", "description", "seoTitle",
               "metaDescription", "search_city", "search_rank", "check_in", "check_out", "nights",
               "scraped_at", "_source_file", "_file_ts"} | DROP_COLUMNS
    unmapped = sorted(seen_keys - handled)
    if unmapped:
        report.append(f"WARNING raw fields not mapped (ignored): {unmapped}")

    df = pd.DataFrame([clean_row(r) for r in raw])
    report.append("Rows by platform: " + json.dumps(df["platform"].value_counts(dropna=False).to_dict()))

    bad = df["id"].isna() | df["platform"].isna()
    if bad.any():
        report.append(f"Dropped {int(bad.sum())} rows missing platform or id")
        df = df[~bad]

    
    df["_filled"] = df.notna().sum(axis=1)
    df = df.sort_values(["scraped_at", "_filled"], ascending=[False, False], na_position="last")
    before = len(df)
    df = df.drop_duplicates(["platform", "id"], keep="first").drop(columns="_filled")
    report.append(f"Exact duplicates removed (same platform+id): {before - len(df)}")
    report.append(f"Final rows: {len(df)}")

    extra = sorted(c for c in df.columns if c.startswith("rating_") and c not in OUTPUT_COLUMNS)
    cols = [c for c in OUTPUT_COLUMNS if c in df.columns]
    pos = cols.index("rating_label") + 1 if "rating_label" in cols else len(cols)
    cols[pos:pos] = extra
    df = df[cols].sort_values(["platform", "search_city", "search_rank", "id"], na_position="last") \
        if "search_city" in cols else df[cols]

    
    report.append("\nDATA-QUALITY NOTES")
    for col in ("bedrooms", "beds", "baths", "guests", "price_amount", "price_nights", "city", "state", "lat"):
        n = int(df[col].isna().sum())
        if n:
            report.append(f"  {col}: {n} missing of {len(df)}")
    if "price_nights" in df:
        report.append("  price_nights values: " + json.dumps(df["price_nights"].value_counts(dropna=False).to_dict(), default=str))
    if "price_currency" in df:
        report.append("  price_currency values: " + json.dumps(df["price_currency"].value_counts(dropna=False).to_dict()))
    if df["is_exact_location"].notna().any():
        report.append(f"  approximate (non-exact) coordinates: {int((df['is_exact_location'] == False).sum())}")  # noqa: E712
    over5 = int((df["rating"] > 5).sum())
    if over5:
        report.append(f"  ratings above 5: {over5} (likely a 10-point scale; do not mix with 5-point ratings)")
    hi = df[df["reviews_count"] > 1000]
    if len(hi):
        report.append(f"  listings with >1000 reviews (check outliers): {len(hi)}")
    report.append("  property_type values: " + json.dumps(df["property_type"].value_counts(dropna=False).to_dict()))
    return df, report


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("inputs", nargs="+", help="raw JSON / JSONL export files")
    ap.add_argument("-o", "--output", default="listings_clean.csv")
    ap.add_argument("-r", "--report", default="cleaning_report.txt")
    args = ap.parse_args(argv)
    df, report = wrangle(args.inputs)
    df.to_csv(args.output, index=False, encoding="utf-8-sig")
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with open(args.report, "w", encoding="utf-8") as fh:
        fh.write(f"Cleaning report ({stamp})\n" + "\n".join(report) + "\n")
    print("\n".join(report))
    print(f"\nWrote {args.output} ({len(df)} rows, {len(df.columns)} columns) and {args.report}")


if __name__ == "__main__":
    sys.exit(main())

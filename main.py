import hashlib
import logging
import math
import os
import re
import secrets
import sys
import time
import uuid
from urllib.parse import parse_qs, urlsplit
import json
import requests
import gspread
from zoneinfo import ZoneInfo
from datetime import datetime, date, timedelta
from oauth2client.service_account import ServiceAccountCredentials

META_API_VERSION = "v27.0"
GOOGLE_ADS_API_VERSION = "v23"
JST = ZoneInfo("Asia/Tokyo")
DEFAULT_WORKSHEET_NAME = "gitreport"

# 媒体ごとの実行可否。使わない媒体は False にすると、
# その媒体の取得処理をスキップし、secrets 側が空でもエラーにしません。
ENABLE_META = True
ENABLE_TIKTOK = False
ENABLE_GOOGLE = False


class ExportError(Exception):
    """Safe for logs. Messages contain only fixed labels and validated numbers."""


def main():
    print("=== Start Unified Reach Export ===")
    config = load_secret()

    resolved = resolve_config(config)
    validate_config(resolved)

    dest = resolved["sheet"]
    book = connect_spreadsheet(dest["spreadsheet_id"], dest["google_service_account"])
    lease = acquire_lease(book, dest["worksheet_name"])
    if lease is None:
        print("Export skipped: another run owns the destination.")
        return
    try:
        rows = collect_rows(resolved)
        write_to_sheet(book, dest["worksheet_name"], rows, lease)
    except BaseException:
        release_lease(book, lease)
        raise
    print(f"Total rows written: {len(rows)}")
    print("=== Completed ===")


def collect_rows(resolved):
    all_monthly_ranges, monthly_ranges, daily_since, daily_until = get_target_date_ranges()

    all_monthly_range_text = ", ".join(
        [f"{r['label']}({r['since']} to {r['until']})" for r in all_monthly_ranges]
    )
    monthly_range_text = ", ".join(
        [f"{r['label']}({r['since']} to {r['until']})" for r in monthly_ranges]
    )

    print(f"Target all monthly ranges: {all_monthly_range_text}")
    print(f"Target normal monthly ranges: {monthly_range_text}")
    print(f"Target daily range: {daily_since} to {daily_until}")

    if ENABLE_META:
        meta_rows = fetch_meta_rows(
            act_id=resolved["meta"]["account_id"],
            token=resolved["meta"]["token"],
            all_monthly_ranges=all_monthly_ranges,
            monthly_ranges=monthly_ranges,
            daily_since=daily_since,
            daily_until=daily_until,
        )
        print(f"Meta rows built: {len(meta_rows)}")
    else:
        meta_rows = []
        print("Meta skipped")

    if ENABLE_TIKTOK:
        tiktok_fetch_since = get_tiktok_daily_fetch_since(daily_until)
        tiktok_rows = fetch_tiktok_rows(
            advertiser_id=resolved["tiktok"]["advertiser_id"],
            access_token=resolved["tiktok"]["access_token"],
            all_monthly_ranges=all_monthly_ranges,
            monthly_ranges=monthly_ranges,
            output_since=daily_since,
            output_until=daily_until,
            daily_fetch_since=tiktok_fetch_since,
        )
        print(f"TikTok rows built: {len(tiktok_rows)}")
    else:
        tiktok_rows = []
        print("TikTok skipped")

    if ENABLE_GOOGLE:
        google_rows = fetch_google_ads_rows(
            google_ads_conf=resolved["google_ads"],
            all_monthly_ranges=all_monthly_ranges,
            monthly_ranges=monthly_ranges,
            daily_since=daily_since,
            daily_until=daily_until,
        )
        print(f"Google Ads rows built: {len(google_rows)}")
    else:
        google_rows = []
        print("Google Ads skipped")

    return sort_rows(meta_rows + tiktok_rows + google_rows)


def load_secret():
    secret_env = os.environ.get("APP_SECRET_JSON")
    if not secret_env:
        raise ExportError("APP_SECRET_JSON is not set")

    try:
        return json.loads(secret_env)
    except json.JSONDecodeError:
        raise ExportError("APP_SECRET_JSON is invalid JSON.") from None


def resolve_config(config):
    meta_conf = config.get("meta", {})
    tiktok_conf = config.get("tiktok", {})
    google_ads_conf = config.get("google_ads", {})
    sheets_conf = config.get("sheets", {})

    spreadsheet_id = sheets_conf.get("spreadsheet_id")
    if not spreadsheet_id:
        legacy_sheet_id = config.get("s_id")
        if isinstance(legacy_sheet_id, list):
            spreadsheet_id = legacy_sheet_id[0] if legacy_sheet_id else None
        else:
            spreadsheet_id = legacy_sheet_id

    worksheet_name = sheets_conf.get("worksheet_name") or DEFAULT_WORKSHEET_NAME

    google_service_account = config.get("gcp_service_account") or config.get("g_creds")
    google_service_account = normalize_google_service_account(google_service_account)

    return {
        "meta": {
            "token": meta_conf.get("token") or config.get("m_token"),
            "account_id": meta_conf.get("account_id") or config.get("m_act_id"),
        },
        "tiktok": {
            "access_token": tiktok_conf.get("access_token"),
            "advertiser_id": tiktok_conf.get("advertiser_id"),
        },
        "google_ads": {
            "developer_token": google_ads_conf.get("developer_token"),
            "client_id": google_ads_conf.get("client_id"),
            "client_secret": google_ads_conf.get("client_secret"),
            "refresh_token": google_ads_conf.get("refresh_token"),
            "customer_id": normalize_customer_id(google_ads_conf.get("customer_id")),
            "login_customer_id": normalize_customer_id(
                google_ads_conf.get("login_customer_id")
            ),
        },
        "sheet": {
            "spreadsheet_id": spreadsheet_id,
            "worksheet_name": worksheet_name,
            "google_service_account": google_service_account,
        },
    }


def validate_config(resolved):
    required = {
        "sheet.spreadsheet_id": resolved["sheet"]["spreadsheet_id"],
        "sheet.google_service_account": resolved["sheet"]["google_service_account"],
    }

    if ENABLE_META:
        required.update({
            "meta.token": resolved["meta"]["token"],
            "meta.account_id": resolved["meta"]["account_id"],
        })

    if ENABLE_TIKTOK:
        required.update({
            "tiktok.access_token": resolved["tiktok"]["access_token"],
            "tiktok.advertiser_id": resolved["tiktok"]["advertiser_id"],
        })

    if ENABLE_GOOGLE:
        required.update({
            "google_ads.developer_token": resolved["google_ads"]["developer_token"],
            "google_ads.client_id": resolved["google_ads"]["client_id"],
            "google_ads.client_secret": resolved["google_ads"]["client_secret"],
            "google_ads.refresh_token": resolved["google_ads"]["refresh_token"],
            "google_ads.customer_id": resolved["google_ads"]["customer_id"],
        })

    missing = [k for k, v in required.items() if not v]
    if missing:
        raise ExportError(f"Missing required config keys: {', '.join(missing)}")


def normalize_google_service_account(creds):
    if not creds:
        return None

    fixed = dict(creds)
    private_key = fixed.get("private_key", "")
    if private_key:
        fixed["private_key"] = private_key.replace("\\n", "\n")
    return fixed


def normalize_customer_id(value):
    if value is None:
        return None
    value = str(value).strip().replace("-", "")
    return value or None


def normalize_meta_act_id(raw_act_id):
    cleaned = (
        str(raw_act_id)
        .replace("act=", "")
        .replace("act_", "")
        .replace("act", "")
        .strip()
    )
    if not re.fullmatch(r"[0-9]+", cleaned):
        raise ExportError("Invalid Meta account ID.")
    return f"act_{cleaned}"


def normalize_day_str(value):
    if not value:
        return ""
    value = str(value).strip()
    return value[:10]


def add_months(base_date, months):
    month = base_date.month - 1 + months
    year = base_date.year + month // 12
    month = month % 12 + 1
    return date(year, month, 1)


def get_target_date_ranges():
    today_jst = datetime.now(JST).date()
    yesterday = today_jst - timedelta(days=1)

    this_month_start = date(today_jst.year, today_jst.month, 1)
    last_month_end = this_month_start - timedelta(days=1)
    last_month_start = date(last_month_end.year, last_month_end.month, 1)

    # 通常範囲：既存どおり前月1日〜前日
    daily_since = last_month_start
    daily_until = yesterday

    # scope="all" の月別のみ：当月を含む過去6ヶ月
    # ただし1日実行時は当月を含めない
    if yesterday < this_month_start:
        all_end_month_start = last_month_start
    else:
        all_end_month_start = this_month_start

    all_start_month = add_months(all_end_month_start, -5)

    all_monthly_ranges = []
    current_month = all_start_month

    while current_month <= all_end_month_start:
        next_month = add_months(current_month, 1)
        month_end = next_month - timedelta(days=1)

        all_monthly_ranges.append({
            "label": current_month.strftime("%Y-%m"),
            "since": current_month,
            "until": min(month_end, yesterday),
        })

        current_month = next_month

    # その他の月別：既存どおり前月＋当月前日まで
    monthly_ranges = [
        {
            "label": last_month_start.strftime("%Y-%m"),
            "since": last_month_start,
            "until": last_month_end,
        }
    ]

    if yesterday >= this_month_start:
        monthly_ranges.append({
            "label": this_month_start.strftime("%Y-%m"),
            "since": this_month_start,
            "until": yesterday,
        })

    return all_monthly_ranges, monthly_ranges, daily_since, daily_until


def get_tiktok_daily_fetch_since(until):
    return until - timedelta(days=63)


def iter_dates(since, until):
    current = since
    while current <= until:
        yield current
        current += timedelta(days=1)


def make_output_row(
    media,
    scope,
    period,
    campaign_name="",
    adset_name="",
    ad_name="",
    unique_reach=0,
    unique_ad_recall_lift="",
):
    return [
        media,
        scope,
        period,
        campaign_name or "",
        adset_name or "",
        ad_name or "",
        to_int(unique_reach),
        to_int_or_blank(unique_ad_recall_lift),
    ]


def to_int_or_blank(value):
    if value in (None, ""):
        return ""
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return ""


def get_nested(data, *keys, default=""):
    current = data
    for key in keys:
        if not isinstance(current, dict):
            return default
        current = current.get(key)
        if current is None:
            return default
    return current


def fetch_meta_rows(
    act_id,
    token,
    all_monthly_ranges,
    monthly_ranges,
    daily_since,
    daily_until,
):
    normalized_act_id = normalize_meta_act_id(act_id)
    rows = []

    common_fields = ["reach", "estimated_ad_recallers"]

    # all（月別）のみ過去6ヶ月
    for month_range in all_monthly_ranges:
        account_monthly = fetch_meta_insights(
            act_id=normalized_act_id,
            token=token,
            since=month_range["since"],
            until=month_range["until"],
            time_increment="monthly",
            level="account",
            fields=common_fields,
        )
        for item in account_monthly:
            rows.append(
                make_output_row(
                    media="meta",
                    scope="all",
                    period=month_range["label"],
                    unique_reach=item.get("reach"),
                    unique_ad_recall_lift=item.get("estimated_ad_recallers"),
                )
            )

    account_daily = fetch_meta_insights(
        act_id=normalized_act_id,
        token=token,
        since=daily_since,
        until=daily_until,
        time_increment="1",
        level="account",
        fields=common_fields,
    )
    for item in account_daily:
        rows.append(
            make_output_row(
                media="meta",
                scope="day",
                period=item.get("date_start", ""),
                unique_reach=item.get("reach"),
                unique_ad_recall_lift=item.get("estimated_ad_recallers"),
            )
        )

    campaign_daily = fetch_meta_insights(
        act_id=normalized_act_id,
        token=token,
        since=daily_since,
        until=daily_until,
        time_increment="1",
        level="campaign",
        fields=["campaign_name", "reach", "estimated_ad_recallers"],
    )
    for item in campaign_daily:
        rows.append(
            make_output_row(
                media="meta",
                scope="campaign_day",
                period=item.get("date_start", ""),
                campaign_name=item.get("campaign_name", ""),
                unique_reach=item.get("reach"),
                unique_ad_recall_lift=item.get("estimated_ad_recallers"),
            )
        )

    # その他の月別は既存どおり
    for month_range in monthly_ranges:
        campaign_monthly = fetch_meta_insights(
            act_id=normalized_act_id,
            token=token,
            since=month_range["since"],
            until=month_range["until"],
            time_increment="monthly",
            level="campaign",
            fields=["campaign_name", "reach", "estimated_ad_recallers"],
        )
        for item in campaign_monthly:
            rows.append(
                make_output_row(
                    media="meta",
                    scope="campaign",
                    period=month_range["label"],
                    campaign_name=item.get("campaign_name", ""),
                    unique_reach=item.get("reach"),
                    unique_ad_recall_lift=item.get("estimated_ad_recallers"),
                )
            )

        adset_monthly = fetch_meta_insights(
            act_id=normalized_act_id,
            token=token,
            since=month_range["since"],
            until=month_range["until"],
            time_increment="monthly",
            level="adset",
            fields=["campaign_name", "adset_name", "reach", "estimated_ad_recallers"],
        )
        for item in adset_monthly:
            rows.append(
                make_output_row(
                    media="meta",
                    scope="adset",
                    period=month_range["label"],
                    campaign_name=item.get("campaign_name", ""),
                    adset_name=item.get("adset_name", ""),
                    unique_reach=item.get("reach"),
                    unique_ad_recall_lift=item.get("estimated_ad_recallers"),
                )
            )

        ad_monthly = fetch_meta_insights(
            act_id=normalized_act_id,
            token=token,
            since=month_range["since"],
            until=month_range["until"],
            time_increment="monthly",
            level="ad",
            fields=[
                "campaign_name",
                "adset_name",
                "ad_name",
                "reach",
                "estimated_ad_recallers",
            ],
        )
        for item in ad_monthly:
            rows.append(
                make_output_row(
                    media="meta",
                    scope="ad",
                    period=month_range["label"],
                    campaign_name=item.get("campaign_name", ""),
                    adset_name=item.get("adset_name", ""),
                    ad_name=item.get("ad_name", ""),
                    unique_reach=item.get("reach"),
                    unique_ad_recall_lift=item.get("estimated_ad_recallers"),
                )
            )

    return rows


def api_json(service, method, url, **kwargs):
    try:
        response = requests.request(method, url, timeout=120, allow_redirects=False, **kwargs)
    except requests.RequestException:
        raise ExportError(f"{service} connection failed.") from None
    status = response.status_code
    try:
        data = response.json()
    except ValueError:
        raise ExportError(f"{service} invalid JSON (HTTP {status}).") from None
    has_error = isinstance(data, dict) and "error" in data
    if not 200 <= status < 300 or has_error:
        error = data.get("error") if has_error else None
        details = ""
        if isinstance(error, dict):
            for key in ("code", "error_subcode"):
                if type(error.get(key)) is int:
                    details += f", {key}={error[key]}"
        raise ExportError(f"{service} failed (HTTP {status}{details}).")
    return data


def valid_count(value):
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return False
    try:
        number = float(value)
        return math.isfinite(number) and number >= 0
    except (ValueError, OverflowError):
        return False


def validate_meta_row(row, fields, since, until, daily):
    if not isinstance(row, dict) or not valid_count(row.get("reach")):
        raise ExportError("Meta returned invalid reach.")
    recall = row.get("estimated_ad_recallers")
    if recall not in (None, "") and not valid_count(recall):
        raise ExportError("Meta returned invalid estimated ad recallers.")
    for field in fields:
        if field.endswith("_name") and not isinstance(row.get(field), str):
            raise ExportError("Meta returned an invalid name.")
    if daily:
        try:
            day = date.fromisoformat(row["date_start"])
            if not since <= day <= until or day.isoformat() != row["date_start"]:
                raise ValueError
        except (KeyError, TypeError, ValueError):
            raise ExportError("Meta returned an invalid date.") from None


def next_cursor(data, endpoint):
    paging = data.get("paging", {})
    if not isinstance(paging, dict):
        raise ExportError("Meta returned invalid paging.")
    if "next" not in paging:
        return None
    link = paging["next"]
    if not isinstance(link, str):
        raise ExportError("Meta returned an invalid next page.")
    parts = urlsplit(link)
    if (parts.scheme != "https" or parts.netloc != "graph.facebook.com"
            or parts.path != urlsplit(endpoint).path or parts.fragment):
        raise ExportError("Meta returned an unexpected next page.")
    query = parse_qs(parts.query, keep_blank_values=True)
    after = query.get("after", [])
    if len(after) != 1 or not after[0] or any(k in query for k in ("before", "offset", "__paging_token")):
        raise ExportError("Meta returned unsupported pagination.")
    cursors = paging.get("cursors", {})
    if not isinstance(cursors, dict) or cursors.get("after", after[0]) != after[0]:
        raise ExportError("Meta returned inconsistent cursors.")
    return after[0]


def fetch_meta_insights(act_id, token, since, until, time_increment, level, fields):
    url = f"https://graph.facebook.com/{META_API_VERSION}/{act_id}/insights"
    params = {
        "level": level,
        "time_range": json.dumps({"since": since.isoformat(), "until": until.isoformat()}),
        "fields": ",".join(fields),
        "time_increment": time_increment,
        "limit": 5000,
    }
    headers = {"Authorization": f"Bearer {token}"}
    all_rows = []
    seen = set()
    while True:
        data = api_json("Meta API", "GET", url, headers=headers, params=params)
        if not isinstance(data, dict) or not isinstance(data.get("data"), list):
            raise ExportError("Meta returned an invalid data array.")
        for row in data["data"]:
            validate_meta_row(row, fields, since, until, time_increment == "1")
            all_rows.append(row)
        after = next_cursor(data, url)
        if after is None:
            return all_rows
        if after in seen:
            raise ExportError("Meta pagination repeated a cursor.")
        seen.add(after)
        params["after"] = after


def fetch_tiktok_rows(
    advertiser_id,
    access_token,
    all_monthly_ranges,
    monthly_ranges,
    output_since,
    output_until,
    daily_fetch_since,
):
    rows = []

    # all（月次）のみ過去6ヶ月
    for month_range in all_monthly_ranges:
        batch = fetch_tiktok_report(
            advertiser_id=advertiser_id,
            access_token=access_token,
            data_level="AUCTION_ADVERTISER",
            dimensions=["advertiser_id"],
            metrics=["reach"],
            since=month_range["since"],
            until=month_range["until"],
        )
        for item in batch:
            rows.append(
                make_output_row(
                    media="tiktok",
                    scope="all",
                    period=month_range["label"],
                    unique_reach=extract_tiktok_metric(item, "reach"),
                    unique_ad_recall_lift="",
                )
            )

    # day（日次）
    for chunk_since, chunk_until in split_date_ranges(daily_fetch_since, output_until, 30):
        batch = fetch_tiktok_report(
            advertiser_id=advertiser_id,
            access_token=access_token,
            data_level="AUCTION_ADVERTISER",
            dimensions=["stat_time_day"],
            metrics=["reach"],
            since=chunk_since,
            until=chunk_until,
        )
        for item in batch:
            day = normalize_day_str(extract_tiktok_dimension(item, "stat_time_day"))
            if not day:
                continue
            if not is_in_date_range(day, output_since, output_until):
                continue

            rows.append(
                make_output_row(
                    media="tiktok",
                    scope="day",
                    period=day,
                    unique_reach=extract_tiktok_metric(item, "reach"),
                    unique_ad_recall_lift="",
                )
            )

    # campaign_day（日次）
    for chunk_since, chunk_until in split_date_ranges(daily_fetch_since, output_until, 30):
        batch = fetch_tiktok_report(
            advertiser_id=advertiser_id,
            access_token=access_token,
            data_level="AUCTION_CAMPAIGN",
            dimensions=["campaign_id", "stat_time_day"],
            metrics=["campaign_name", "reach"],
            since=chunk_since,
            until=chunk_until,
        )
        for item in batch:
            day = normalize_day_str(extract_tiktok_dimension(item, "stat_time_day"))
            if not day:
                continue
            if not is_in_date_range(day, output_since, output_until):
                continue

            rows.append(
                make_output_row(
                    media="tiktok",
                    scope="campaign_day",
                    period=day,
                    campaign_name=extract_tiktok_metric(item, "campaign_name"),
                    unique_reach=extract_tiktok_metric(item, "reach"),
                    unique_ad_recall_lift="",
                )
            )

    # その他の月別は既存どおり
    for month_range in monthly_ranges:
        batch = fetch_tiktok_report(
            advertiser_id=advertiser_id,
            access_token=access_token,
            data_level="AUCTION_CAMPAIGN",
            dimensions=["campaign_id"],
            metrics=["campaign_name", "reach"],
            since=month_range["since"],
            until=month_range["until"],
        )
        for item in batch:
            rows.append(
                make_output_row(
                    media="tiktok",
                    scope="campaign",
                    period=month_range["label"],
                    campaign_name=extract_tiktok_metric(item, "campaign_name"),
                    unique_reach=extract_tiktok_metric(item, "reach"),
                    unique_ad_recall_lift="",
                )
            )

        batch = fetch_tiktok_report(
            advertiser_id=advertiser_id,
            access_token=access_token,
            data_level="AUCTION_ADGROUP",
            dimensions=["adgroup_id"],
            metrics=["campaign_name", "adgroup_name", "reach"],
            since=month_range["since"],
            until=month_range["until"],
        )
        for item in batch:
            rows.append(
                make_output_row(
                    media="tiktok",
                    scope="adset",
                    period=month_range["label"],
                    campaign_name=extract_tiktok_metric(item, "campaign_name"),
                    adset_name=extract_tiktok_metric(item, "adgroup_name"),
                    unique_reach=extract_tiktok_metric(item, "reach"),
                    unique_ad_recall_lift="",
                )
            )

        batch = fetch_tiktok_report(
            advertiser_id=advertiser_id,
            access_token=access_token,
            data_level="AUCTION_AD",
            dimensions=["ad_id"],
            metrics=["campaign_name", "adgroup_name", "ad_name", "reach"],
            since=month_range["since"],
            until=month_range["until"],
        )
        for item in batch:
            rows.append(
                make_output_row(
                    media="tiktok",
                    scope="ad",
                    period=month_range["label"],
                    campaign_name=extract_tiktok_metric(item, "campaign_name"),
                    adset_name=extract_tiktok_metric(item, "adgroup_name"),
                    ad_name=extract_tiktok_metric(item, "ad_name"),
                    unique_reach=extract_tiktok_metric(item, "reach"),
                    unique_ad_recall_lift="",
                )
            )

    return rows


def fetch_tiktok_report(
    advertiser_id,
    access_token,
    data_level,
    dimensions,
    metrics,
    since,
    until,
):
    url = "https://business-api.tiktok.com/open_api/v1.3/report/integrated/get/"
    headers = {
        "Access-Token": access_token,
    }

    page = 1
    page_size = 1000
    all_rows = []

    while True:
        params = {
            "advertiser_id": advertiser_id,
            "report_type": "BASIC",
            "service_type": "AUCTION",
            "data_level": data_level,
            "dimensions": json.dumps(dimensions, separators=(",", ":")),
            "metrics": json.dumps(metrics, separators=(",", ":")),
            "start_date": since.strftime("%Y-%m-%d"),
            "end_date": until.strftime("%Y-%m-%d"),
            "page": page,
            "page_size": page_size,
            "query_lifetime": "false",
            "enable_total_metrics": "false",
            "multi_adv_report_in_utc_time": "false",
        }

        payload = api_json("TikTok API", "GET", url, headers=headers, params=params)
        if not isinstance(payload, dict) or str(payload.get("code")) != "0":
            raise ExportError("TikTok API rejected the report.")
        data = payload.get("data")
        if not isinstance(data, dict) or not isinstance(data.get("list"), list):
            raise ExportError("TikTok returned an invalid report.")
        batch = data["list"]
        for item in batch:
            if not isinstance(item, dict):
                raise ExportError("TikTok returned an invalid row.")
            for metric in metrics:
                value = extract_tiktok_metric(item, metric)
                if metric == "reach" and not valid_count(value):
                    raise ExportError("TikTok returned invalid reach.")
                if metric.endswith("_name") and not isinstance(value, str):
                    raise ExportError("TikTok returned an invalid name.")
            for field in dimensions:
                value = extract_tiktok_dimension(item, field)
                if value in (None, ""):
                    raise ExportError("TikTok returned an invalid dimension.")
                if field == "stat_time_day" and not is_in_date_range(value, since, until):
                    raise ExportError("TikTok returned an invalid date.")
        all_rows.extend(batch)
        info = data.get("page_info")
        if not isinstance(info, dict):
            raise ExportError("TikTok returned invalid paging.")
        try:
            total = int(info["total_page"])
            count = int(info["total_number"])
            actual_page = int(info.get("page", page))
        except (KeyError, TypeError, ValueError):
            raise ExportError("TikTok returned invalid paging.") from None
        if actual_page != page or total < 0 or count < 0:
            raise ExportError("TikTok returned inconsistent paging.")
        if page >= total:
            if len(all_rows) != count:
                raise ExportError("TikTok returned an incomplete report.")
            break
        if not batch:
            raise ExportError("TikTok returned an incomplete page.")


        page += 1

    return all_rows


def extract_tiktok_dimension(item, key):
    dimensions = item.get("dimensions", {})
    if isinstance(dimensions, dict) and key in dimensions:
        return dimensions.get(key)

    if key in item:
        return item.get(key)

    return ""


def extract_tiktok_metric(item, key):
    metrics = item.get("metrics", {})
    if isinstance(metrics, dict) and key in metrics:
        return metrics.get(key)

    if isinstance(metrics, list) and metrics:
        for metric_item in metrics:
            if isinstance(metric_item, dict) and key in metric_item:
                return metric_item.get(key)

    if key in item:
        return item.get(key)

    return None


def fetch_google_ads_rows(
    google_ads_conf,
    all_monthly_ranges,
    monthly_ranges,
    daily_since,
    daily_until,
):
    access_token = refresh_google_ads_access_token(
        client_id=google_ads_conf["client_id"],
        client_secret=google_ads_conf["client_secret"],
        refresh_token=google_ads_conf["refresh_token"],
    )

    rows = []

    # all（月次）のみ過去6ヶ月
    for month_range in all_monthly_ranges:
        monthly_all_query = f"""
            SELECT
              campaign.id,
              metrics.unique_users
            FROM campaign
            WHERE campaign.status != 'REMOVED'
              AND segments.date BETWEEN '{month_range["since"]:%Y-%m-%d}' AND '{month_range["until"]:%Y-%m-%d}'
            ORDER BY campaign.id
        """.strip()

        monthly_all_response = google_ads_search_stream(
            access_token=access_token,
            developer_token=google_ads_conf["developer_token"],
            customer_id=google_ads_conf["customer_id"],
            login_customer_id=google_ads_conf["login_customer_id"],
            query=monthly_all_query,
            summary_row_setting="SUMMARY_ROW_WITH_RESULTS",
        )

        summary_row = monthly_all_response.get("summary_row")
        if summary_row:
            rows.append(
                make_output_row(
                    media="google",
                    scope="all",
                    period=month_range["label"],
                    unique_reach=get_nested(
                        summary_row, "metrics", "uniqueUsers", default=0
                    ),
                    unique_ad_recall_lift="",
                )
            )

    # その他の月別は既存どおり
    for month_range in monthly_ranges:
        monthly_campaign_query = f"""
            SELECT
              campaign.id,
              campaign.name,
              metrics.unique_users
            FROM campaign
            WHERE campaign.status != 'REMOVED'
              AND segments.date BETWEEN '{month_range["since"]:%Y-%m-%d}' AND '{month_range["until"]:%Y-%m-%d}'
            ORDER BY campaign.id
        """.strip()

        monthly_campaign_response = google_ads_search_stream(
            access_token=access_token,
            developer_token=google_ads_conf["developer_token"],
            customer_id=google_ads_conf["customer_id"],
            login_customer_id=google_ads_conf["login_customer_id"],
            query=monthly_campaign_query,
            summary_row_setting="SUMMARY_ROW_WITH_RESULTS",
        )

        for item in monthly_campaign_response["results"]:
            rows.append(
                make_output_row(
                    media="google",
                    scope="campaign",
                    period=month_range["label"],
                    campaign_name=get_nested(item, "campaign", "name", default=""),
                    unique_reach=get_nested(item, "metrics", "uniqueUsers", default=0),
                    unique_ad_recall_lift="",
                )
            )

    # day（日別 summary row）
    for day in iter_dates(daily_since, daily_until):
        daily_all_query = f"""
            SELECT
              campaign.id,
              metrics.unique_users
            FROM campaign
            WHERE campaign.status != 'REMOVED'
              AND segments.date = '{day:%Y-%m-%d}'
            ORDER BY campaign.id
        """.strip()

        daily_all_response = google_ads_search_stream(
            access_token=access_token,
            developer_token=google_ads_conf["developer_token"],
            customer_id=google_ads_conf["customer_id"],
            login_customer_id=google_ads_conf["login_customer_id"],
            query=daily_all_query,
            summary_row_setting="SUMMARY_ROW_ONLY",
        )

        summary_row = daily_all_response.get("summary_row")
        if summary_row:
            rows.append(
                make_output_row(
                    media="google",
                    scope="day",
                    period=day.strftime("%Y-%m-%d"),
                    unique_reach=get_nested(
                        summary_row, "metrics", "uniqueUsers", default=0
                    ),
                    unique_ad_recall_lift="",
                )
            )

    # campaign_day（日次明細）
    daily_campaign_query = f"""
        SELECT
          campaign.id,
          campaign.name,
          segments.date,
          metrics.unique_users
        FROM campaign
        WHERE campaign.status != 'REMOVED'
          AND segments.date BETWEEN '{daily_since:%Y-%m-%d}' AND '{daily_until:%Y-%m-%d}'
        ORDER BY segments.date, campaign.id
    """.strip()

    daily_campaign_response = google_ads_search_stream(
        access_token=access_token,
        developer_token=google_ads_conf["developer_token"],
        customer_id=google_ads_conf["customer_id"],
        login_customer_id=google_ads_conf["login_customer_id"],
        query=daily_campaign_query,
    )

    for item in daily_campaign_response["results"]:
        rows.append(
            make_output_row(
                media="google",
                scope="campaign_day",
                period=get_nested(item, "segments", "date", default=""),
                campaign_name=get_nested(item, "campaign", "name", default=""),
                unique_reach=get_nested(item, "metrics", "uniqueUsers", default=0),
                unique_ad_recall_lift="",
            )
        )

    return rows


def refresh_google_ads_access_token(client_id, client_secret, refresh_token):
    payload = api_json(
        "Google OAuth", "POST", "https://oauth2.googleapis.com/token",
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        },
    )
    access_token = payload.get("access_token") if isinstance(payload, dict) else None
    if not isinstance(access_token, str) or not access_token:
        raise ExportError("Google OAuth returned no access token.")

    print("Google Ads OAuth token refreshed successfully")
    return access_token


def google_ads_search_stream(
    access_token,
    developer_token,
    customer_id,
    login_customer_id,
    query,
    summary_row_setting=None,
):
    url = (
        f"https://googleads.googleapis.com/{GOOGLE_ADS_API_VERSION}/customers/"
        f"{customer_id}/googleAds:searchStream"
    )
    headers = {
        "Authorization": f"Bearer {access_token}",
        "developer-token": developer_token,
        "Content-Type": "application/json",
    }
    if login_customer_id:
        headers["login-customer-id"] = login_customer_id

    body = {"query": query}
    if summary_row_setting:
        body["summaryRowSetting"] = summary_row_setting

    payload = api_json("Google Ads API", "POST", url, headers=headers, json=body)
    if not isinstance(payload, list):
        raise ExportError("Google Ads returned an invalid report.")
    all_rows = []
    summary_row = None
    for chunk in payload:
        if (not isinstance(chunk, dict) or "error" in chunk
                or not any(k in chunk for k in ("results", "summaryRow", "fieldMask", "requestId"))):
            raise ExportError("Google Ads returned an invalid chunk.")
        batch = chunk.get("results", [])
        if not isinstance(batch, list):
            raise ExportError("Google Ads returned invalid results.")
        for row in batch:
            if not isinstance(row, dict) or not valid_count(get_nested(row, "metrics", "uniqueUsers")):
                raise ExportError("Google Ads returned invalid reach.")
            if "campaign.name" in query and not isinstance(get_nested(row, "campaign", "name", default=None), str):
                raise ExportError("Google Ads returned an invalid name.")
            if "segments.date," in query:
                date.fromisoformat(get_nested(row, "segments", "date"))
        all_rows.extend(batch)
        if "summaryRow" in chunk:
            summary_row = chunk["summaryRow"]
            if not valid_count(get_nested(summary_row, "metrics", "uniqueUsers")):
                raise ExportError("Google Ads returned an invalid summary.")
    if summary_row_setting and all_rows and summary_row is None:
        raise ExportError("Google Ads returned an incomplete summary.")

    return {
        "results": all_rows,
        "summary_row": summary_row,
    }


def connect_spreadsheet(sheet_id, google_creds_dict):
    try:
        scope = [
            "https://spreadsheets.google.com/feeds",
            "https://www.googleapis.com/auth/drive",
        ]
        creds = ServiceAccountCredentials.from_json_keyfile_dict(
            google_creds_dict, scope
        )
        client = gspread.authorize(creds)
        spreadsheet = client.open_by_key(sheet_id)
        print("Google Sheets connected successfully")
        return spreadsheet
    except Exception:
        raise ExportError("Google Sheets connection failed.") from None


HEADER = ["media", "scope", "period", "campaign_name", "adset_name", "ad_name",
          "unique_reach", "unique_ad_recall_lift"]
LEASE_SECONDS = 3600


def sheet_state(book):
    return book.fetch_sheet_metadata(params={
        "fields": "sheets(properties(sheetId,title,sheetType,gridProperties(rowCount,columnCount))),"
                  "namedRanges(namedRangeId,name)"
    })


def range_names(state):
    return {item["name"]: item["namedRangeId"] for item in state.get("namedRanges", [])}


def delete_range(range_id):
    return {"deleteNamedRange": {"namedRangeId": range_id}}


def add_range(name, range_id, sheet_id):
    return {"addNamedRange": {"namedRange": {
        "name": name, "namedRangeId": range_id,
        "range": {"sheetId": sheet_id, "startRowIndex": 0, "endRowIndex": 1,
                  "startColumnIndex": 0, "endColumnIndex": 1},
    }}}


def lease_time(range_id):
    match = re.fullmatch(r"tgp_([0-9]+)_[0-9a-f]{32}", range_id)
    if not match:
        raise ExportError("Unexpected Sheets lock. Check named ranges.")
    return int(match.group(1))


def acquire_lease(book, title):
    # All runners compete for one unique name. Each acquisition uses a fresh ID.
    key = hashlib.sha256(title.casefold().encode()).hexdigest()[:24]
    lock = "tgp_lock_" + key
    state = sheet_state(book)
    existing = range_names(state).get(lock)
    now = int(time.time())
    if existing and now < lease_time(existing) + LEASE_SECONDS:
        return None
    token = f"tgp_{now}_{uuid.uuid4().hex}"
    anchor = next(s["properties"]["sheetId"] for s in state["sheets"]
                  if s["properties"].get("sheetType", "GRID") == "GRID")
    lease = {"id": token, "name": lock, "receipt_prefix": "tgp_done_" + key + "_",
             "anchor": anchor}
    ops = [delete_range(existing)] if existing else []
    ops.append(add_range(lock, token, anchor))
    try:
        book.batch_update({"requests": ops})
    except Exception:
        owner = range_names(sheet_state(book)).get(lock)
        if owner == token:
            return lease
        if owner and owner != existing:
            return None
        raise ExportError("Sheets lock acquisition failed.") from None
    return lease


def release_lease(book, lease):
    # IDs are never reused. A delayed cleanup cannot unlock the successor.
    try:
        if range_names(sheet_state(book)).get(lease["name"]) == lease["id"]:
            book.batch_update({"requests": [delete_range(lease["id"])]})
    except Exception:
        print("Sheets lock cleanup unconfirmed. Expiry permits recovery.", file=sys.stderr)


def previous_report_height(book, title, new_height):
    escaped = title.replace("'", "''")
    result = book.values_get(f"'{escaped}'!A:H", params={"valueRenderOption": "FORMULA"})
    values = result.get("values", [])
    height = 0
    if values and values[0] == HEADER:
        height = 1
        for row in values[1:]:
            if len(row) < 3 or row[0] not in {"meta", "tiktok", "google"}:
                break
            if row[1] not in {"all", "day", "campaign_day", "campaign", "adset", "ad"}:
                break
            height += 1
    if any(any(cell != "" for cell in row) for row in values[height:new_height]):
        raise ExportError("Sheets output overlaps existing non-report data.")
    return height


def write_to_sheet(book, title, rows, lease):
    state = sheet_state(book)
    names = range_names(state)
    if names.get(lease["name"]) != lease["id"]:
        raise ExportError("Sheets lock ownership changed.")
    # A missing owner ID invalidates this *whole* batch even after the preliminary read.
    receipt_id = "receipt_" + lease["id"]
    receipt = lease["receipt_prefix"] + lease["id"]
    ops = [{"updateNamedRange": {"namedRange": {"namedRangeId": lease["id"], "name": receipt},
                                 "fields": "name"}},
           delete_range(lease["id"]), add_range(receipt, receipt_id, lease["anchor"])]
    # Keep receipts long enough for a timed-out writer to confirm its own commit.
    cutoff = int(time.time()) - 86400
    for name, range_id in names.items():
        saved_token = range_id.removeprefix("receipt_")
        if name == lease["receipt_prefix"] + saved_token and lease_time(saved_token) < cutoff:
            ops.append(delete_range(range_id))
    props = next((s["properties"] for s in state["sheets"]
                  if s["properties"]["title"] == title), None)
    output = [HEADER, *rows]
    if props is None:
        sheet_id = secrets.randbelow(2**31 - 1) + 1
        height = len(output)
        ops.append({"addSheet": {"properties": {
            "sheetId": sheet_id, "title": title,
            "gridProperties": {"rowCount": max(1000, height), "columnCount": len(HEADER)},
        }}})
    else:
        sheet_id = props["sheetId"]
        grid = props["gridProperties"]
        height = max(len(output), previous_report_height(book, title, len(output)))
        for dimension, current, needed in (("ROWS", grid["rowCount"], height),
                                            ("COLUMNS", grid["columnCount"], len(HEADER))):
            if needed > current:
                ops.append({"appendDimension": {"sheetId": sheet_id, "dimension": dimension,
                                                "length": needed - current}})
    # updateCells with an explicit range clears only surplus values in the old report.
    cell_rows = []
    for row in output:
        cells = []
        for value in row:
            field = "numberValue" if isinstance(value, (int, float)) else "stringValue"
            cells.append({"userEnteredValue": {field: value}})
        cell_rows.append({"values": cells})
    ops.append({"updateCells": {
        "range": {"sheetId": sheet_id, "startRowIndex": 0, "endRowIndex": height,
                  "startColumnIndex": 0, "endColumnIndex": len(HEADER)},
        "rows": cell_rows, "fields": "userEnteredValue",
    }})
    try:
        book.batch_update({"requests": ops})
    except Exception:
        try:
            confirmed = range_names(sheet_state(book)).get(receipt) == receipt_id
        except Exception:
            raise ExportError("Sheets commit could not be confirmed.") from None
        if not confirmed:
            raise ExportError("Sheets commit was not confirmed.") from None


def sort_rows(rows):
    media_order = {"meta": 0, "tiktok": 1, "google": 2}
    scope_order = {
        "all": 0,
        "day": 1,
        "campaign_day": 2,
        "campaign": 3,
        "adset": 4,
        "ad": 5,
    }

    def sort_key(row):
        media, scope, period, campaign_name, adset_name, ad_name, _reach, _lift = row
        period_num = int(str(period).replace("-", "")) if period else 0
        return (
            media_order.get(media, 999),
            -period_num,
            scope_order.get(scope, 999),
            campaign_name,
            adset_name,
            ad_name,
        )

    return sorted(rows, key=sort_key)


def split_date_ranges(since, until, max_days):
    ranges = []
    current_since = since

    while current_since <= until:
        current_until = min(current_since + timedelta(days=max_days - 1), until)
        ranges.append((current_since, current_until))
        current_since = current_until + timedelta(days=1)

    return ranges


def is_in_date_range(day_str, since, until):
    normalized = normalize_day_str(day_str)
    if not normalized:
        return False
    target = datetime.strptime(normalized, "%Y-%m-%d").date()
    return since <= target <= until


def chunked(items, size):
    for i in range(0, len(items), size):
        yield items[i : i + size]


def to_month(value):
    if not value:
        return ""
    return str(value)[:7]


def to_int(value):
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0





if __name__ == "__main__":
    logging.disable(logging.CRITICAL)
    try:
        main()
    except ExportError as exc:
        sys.exit(str(exc))
    except Exception:
        sys.exit("Export failed: unexpected configuration, service, or response error.")

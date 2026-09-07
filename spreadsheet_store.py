import json
import os
import re
from datetime import datetime

import gspread


MEMBERS_SHEET_NAME = "members"
SCORES_SHEET_NAME = "scores"
SCHEDULED_NOTICES_SHEET_NAME = "scheduled_notices"

MEMBERS_HEADERS = [
    "user_id",
    "display_name",
    "name",
    "roles",
    "score",
    "updated_at",
]
SCORES_HEADERS = [
    "created_at",
    "user_id",
    "display_name",
    "points",
    "reason",
    "created_by",
]
SCHEDULED_NOTICES_HEADERS = [
    "id",
    "send_at",
    "content",
    "status",
    "created_by",
    "created_at",
    "sent_at",
    "sent_message_id",
    "error",
]


def _get_client():
    service_account_json = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
    if service_account_json:
        return gspread.service_account_from_dict(json.loads(service_account_json))

    credentials_file = (
        os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE")
        or os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
    )
    if credentials_file:
        return gspread.service_account(filename=credentials_file)

    return gspread.service_account()


def _get_spreadsheet():
    spreadsheet_id = os.getenv("GOOGLE_SPREADSHEET_ID")
    if not spreadsheet_id:
        raise RuntimeError("GOOGLE_SPREADSHEET_ID 환경변수가 설정되지 않았습니다.")

    return _get_client().open_by_key(spreadsheet_id)


def _get_or_create_worksheet(spreadsheet, title, headers):
    try:
        worksheet = spreadsheet.worksheet(title)
    except gspread.WorksheetNotFound:
        worksheet = spreadsheet.add_worksheet(title=title, rows=1000, cols=len(headers))

    existing_headers = worksheet.row_values(1)
    if existing_headers != headers:
        worksheet.update("A1", [headers])

    return worksheet


def get_worksheets():
    spreadsheet = _get_spreadsheet()
    return {
        MEMBERS_SHEET_NAME: _get_or_create_worksheet(spreadsheet, MEMBERS_SHEET_NAME, MEMBERS_HEADERS),
        SCORES_SHEET_NAME: _get_or_create_worksheet(spreadsheet, SCORES_SHEET_NAME, SCORES_HEADERS),
        SCHEDULED_NOTICES_SHEET_NAME: _get_or_create_worksheet(
            spreadsheet,
            SCHEDULED_NOTICES_SHEET_NAME,
            SCHEDULED_NOTICES_HEADERS,
        ),
    }


def sync_members(members):
    worksheets = get_worksheets()
    worksheet = worksheets[MEMBERS_SHEET_NAME]
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    existing_rows = worksheet.get_all_records()
    existing_scores = {
        str(row.get("user_id")): row.get("score", 0)
        for row in existing_rows
        if row.get("user_id")
    }

    rows = []
    for member in members:
        user_id = str(member["user_id"])
        rows.append([
            user_id,
            member["display_name"],
            member["name"],
            ", ".join(member["roles"]),
            existing_scores.get(user_id, 0),
            now,
        ])

    worksheet.clear()
    worksheet.update("A1", [MEMBERS_HEADERS] + rows)
    return len(rows)


def find_member(member_key):
    worksheets = get_worksheets()
    rows = worksheets[MEMBERS_SHEET_NAME].get_all_records()
    normalized_key = _normalize_member_key(member_key)
    matches = []

    for row in rows:
        user_id = str(row.get("user_id", "")).strip()
        display_name = str(row.get("display_name", "")).strip().lower()
        name = str(row.get("name", "")).strip().lower()
        if (
            normalized_key == user_id
            or normalized_key == display_name
            or normalized_key == name
            or display_name.startswith(normalized_key)
            or name.startswith(normalized_key)
        ):
            matches.append(row)

    return matches


def _normalize_member_key(member_key):
    normalized_key = str(member_key).strip().lower()
    mention_match = re.fullmatch(r"<@!?(\d+)>", normalized_key)

    if mention_match:
        return mention_match.group(1)

    return normalized_key


def add_score(member, points, reason, created_by):
    worksheets = get_worksheets()
    members_worksheet = worksheets[MEMBERS_SHEET_NAME]
    scores_worksheet = worksheets[SCORES_SHEET_NAME]
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    scores_worksheet.append_row([
        now,
        member["user_id"],
        member["display_name"],
        points,
        reason,
        created_by,
    ])

    rows = members_worksheet.get_all_records()
    for index, row in enumerate(rows, start=2):
        if str(row.get("user_id")) == str(member["user_id"]):
            current_score = int(row.get("score") or 0)
            new_score = current_score + points
            members_worksheet.update_cell(index, MEMBERS_HEADERS.index("score") + 1, new_score)
            members_worksheet.update_cell(index, MEMBERS_HEADERS.index("updated_at") + 1, now)
            return new_score

    return points


def reset_score(member, created_by):
    worksheets = get_worksheets()
    members_worksheet = worksheets[MEMBERS_SHEET_NAME]
    scores_worksheet = worksheets[SCORES_SHEET_NAME]
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    rows = members_worksheet.get_all_records()
    current_score = 0
    member_row_index = None

    for index, row in enumerate(rows, start=2):
        if str(row.get("user_id")) == str(member["user_id"]):
            current_score = int(row.get("score") or 0)
            member_row_index = index
            break

    reset_delta = -current_score
    scores_worksheet.append_row([
        now,
        member["user_id"],
        member["display_name"],
        reset_delta,
        "점수 초기화",
        created_by,
    ])

    if member_row_index is not None:
        members_worksheet.update_cell(member_row_index, MEMBERS_HEADERS.index("score") + 1, 0)
        members_worksheet.update_cell(member_row_index, MEMBERS_HEADERS.index("updated_at") + 1, now)

    return current_score


def add_scheduled_notice(send_at, content, created_by):
    worksheets = get_worksheets()
    worksheet = worksheets[SCHEDULED_NOTICES_SHEET_NAME]
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    rows = worksheet.get_all_records()
    next_id = max([int(row.get("id") or 0) for row in rows] or [0]) + 1

    worksheet.append_row([
        next_id,
        send_at.strftime("%Y-%m-%d %H:%M:%S"),
        content,
        "pending",
        created_by,
        now,
        "",
        "",
        "",
    ])
    return next_id


def get_pending_scheduled_notices():
    worksheets = get_worksheets()
    rows = worksheets[SCHEDULED_NOTICES_SHEET_NAME].get_all_records()
    pending = []

    for index, row in enumerate(rows, start=2):
        if str(row.get("status", "")).strip().lower() != "pending":
            continue

        pending.append((index, row))

    return pending


def parse_scheduled_notice_time(value):
    return datetime.strptime(str(value), "%Y-%m-%d %H:%M:%S")


def update_scheduled_notice_status_by_id(scheduled_notice_id, status, sent_message_id="", error=""):
    worksheets = get_worksheets()
    worksheet = worksheets[SCHEDULED_NOTICES_SHEET_NAME]
    rows = worksheet.get_all_records()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    for index, row in enumerate(rows, start=2):
        if str(row.get("id")) == str(scheduled_notice_id):
            worksheet.update_cell(index, SCHEDULED_NOTICES_HEADERS.index("status") + 1, status)
            worksheet.update_cell(index, SCHEDULED_NOTICES_HEADERS.index("sent_at") + 1, now)
            worksheet.update_cell(index, SCHEDULED_NOTICES_HEADERS.index("sent_message_id") + 1, sent_message_id)
            worksheet.update_cell(index, SCHEDULED_NOTICES_HEADERS.index("error") + 1, error)
            return True

    return False


def update_scheduled_notice_status(row_index, status, sent_message_id="", error=""):
    worksheets = get_worksheets()
    worksheet = worksheets[SCHEDULED_NOTICES_SHEET_NAME]
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    worksheet.update_cell(row_index, SCHEDULED_NOTICES_HEADERS.index("status") + 1, status)
    worksheet.update_cell(row_index, SCHEDULED_NOTICES_HEADERS.index("sent_at") + 1, now)
    worksheet.update_cell(row_index, SCHEDULED_NOTICES_HEADERS.index("sent_message_id") + 1, sent_message_id)
    worksheet.update_cell(row_index, SCHEDULED_NOTICES_HEADERS.index("error") + 1, error)

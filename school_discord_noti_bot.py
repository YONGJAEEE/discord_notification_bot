import discord
from discord.ext import commands
import asyncio
import os
import re
import typing
from datetime import datetime

from dm_sender import send_direct_message
from env_loader import load_env_file
from spreadsheet_store import (
    add_scheduled_notice,
    add_score,
    find_member,
    get_pending_scheduled_notices,
    mark_pending_score_dm_results_unknown,
    parse_scheduled_notice_time,
    reset_all_scores,
    reset_score,
    sync_members,
    update_score_dm_result,
    update_scheduled_notice_status_by_id,
)


load_env_file()


def get_required_env(name):
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"{name} 환경변수가 설정되지 않았습니다.")
    return value


def get_required_int_env(name):
    return int(get_required_env(name))


def get_required_int_list_env(name):
    value = get_required_env(name)
    return [int(item.strip()) for item in value.split(",") if item.strip()]

intents = discord.Intents.default()
intents.message_content = True
intents.reactions = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)
scheduled_notice_tasks = {}
notice_reaction_check_tasks = {}

DISCORD_BOT_TOKEN = get_required_env("DISCORD_BOT_TOKEN")

# 채널 ID
BOT_NOTICE_CHANNEL_ID = get_required_int_env("BOT_NOTICE_CHANNEL_ID")
CHALLENGER_NOTICE_CHANNEL_ID = get_required_int_env("CHALLENGER_NOTICE_CHANNEL_ID")
NOTICE_STATISTICS_CHANNEL_ID = get_required_int_env("NOTICE_STATISTICS_CHANNEL_ID")
SCORE_COMMAND_CHANNEL_ID = get_required_int_env("SCORE_COMMAND_CHANNEL_ID")

# 역할 ID
TARGET_ROLE_IDS = get_required_int_list_env("TARGET_ROLE_IDS")
STAFF_ROLE_IDS = get_required_int_list_env("STAFF_ROLE_IDS")
NOTICE_CONFIRM_EMOJI_ID = 1019827185676197918
NOTICE_CONFIRM_EMOJI_TEXT = "<:gachon:1019827185676197918>"

@bot.event
async def on_ready():
    print(f'{bot.user} 이 활성화 되었습니다!')
    await restore_scheduled_notice_tasks()


async def restore_scheduled_notice_tasks():
    try:
        pending_notices = await asyncio.to_thread(get_pending_scheduled_notices)
    except Exception as error:
        print(f"예약 공지 복구 중 오류가 발생했습니다: {error}")
        return

    for _row_index, scheduled_notice in pending_notices:
        try:
            scheduled_notice_id = int(scheduled_notice["id"])
            send_at = parse_scheduled_notice_time(scheduled_notice["send_at"])
            schedule_notice_task(scheduled_notice_id, send_at, scheduled_notice["content"])
        except Exception as error:
            print(f"예약 공지 #{scheduled_notice.get('id')} 복구 중 오류가 발생했습니다: {error}")


def schedule_notice_task(scheduled_notice_id, send_at, message_content):
    existing_task = scheduled_notice_tasks.get(scheduled_notice_id)
    if existing_task is not None and not existing_task.done():
        return

    scheduled_notice_tasks[scheduled_notice_id] = asyncio.create_task(
        run_scheduled_notice(scheduled_notice_id, send_at, message_content)
    )


def schedule_notice_reaction_check_task(guild, message_id):
    existing_task = notice_reaction_check_tasks.get(message_id)
    if existing_task is not None and not existing_task.done():
        return

    notice_reaction_check_tasks[message_id] = asyncio.create_task(
        run_notice_reaction_check(guild, message_id)
    )


async def run_scheduled_notice(scheduled_notice_id, send_at, message_content):
    try:
        delay = max((send_at - datetime.now()).total_seconds(), 0)
        if delay > 0:
            await asyncio.sleep(delay)

        await send_scheduled_notice(scheduled_notice_id, message_content)
    finally:
        scheduled_notice_tasks.pop(scheduled_notice_id, None)


async def run_notice_reaction_check(guild, message_id):
    try:
        await asyncio.sleep(86400)
        try:
            await send_notice_reaction_report(guild, message_id)
        except Exception as error:
            print(f"공지 메시지 {message_id} 리액션 확인 중 오류가 발생했습니다: {error}")
    finally:
        notice_reaction_check_tasks.pop(message_id, None)


async def send_scheduled_notice(scheduled_notice_id, message_content):
    try:
        await asyncio.to_thread(
            update_scheduled_notice_status_by_id,
            scheduled_notice_id,
            "sending",
        )

        challenger_notice_channel = bot.get_channel(CHALLENGER_NOTICE_CHANNEL_ID)
        if challenger_notice_channel is None:
            raise RuntimeError("챌린저_공지 채널을 찾을 수 없습니다.")

        bot_notice_channel = bot.get_channel(BOT_NOTICE_CHANNEL_ID)

        notice_message = await challenger_notice_channel.send(message_content)
        await notice_message.add_reaction(NOTICE_CONFIRM_EMOJI_TEXT)
        await asyncio.to_thread(
            update_scheduled_notice_status_by_id,
            scheduled_notice_id,
            "sent",
            str(notice_message.id),
            "",
        )
        first_line = get_first_line(message_content)
        if bot_notice_channel is not None:
            await bot_notice_channel.send(
                f"예약 공지 #{scheduled_notice_id} 발송 완료\n"
                f"메시지 ID: {notice_message.id}\n"
                f"> {first_line}"
            )
        schedule_notice_reaction_check_task(challenger_notice_channel.guild, notice_message.id)
    except Exception as error:
        await asyncio.to_thread(
            update_scheduled_notice_status_by_id,
            scheduled_notice_id,
            "failed",
            "",
            str(error),
        )


def get_first_line(message_content):
    lines = str(message_content).splitlines()
    return lines[0] if lines else ""


async def fetch_challenger_notice_message(message_id):
    challenger_notice_channel = bot.get_channel(CHALLENGER_NOTICE_CHANNEL_ID)
    if challenger_notice_channel is None:
        raise RuntimeError("챌린저_공지 채널을 찾을 수 없습니다.")

    return await challenger_notice_channel.fetch_message(message_id)


async def get_notice_reacted_user_ids(notice_message):
    for reaction in notice_message.reactions:
        if getattr(reaction.emoji, "id", None) != NOTICE_CONFIRM_EMOJI_ID:
            continue

        return {
            user.id
            async for user in reaction.users()
            if not user.bot
        }

    return set()


def get_target_notice_members(guild):
    target_members_by_id = {}

    for role_id in TARGET_ROLE_IDS:
        target_role = guild.get_role(role_id)
        if target_role is None:
            continue

        for member in target_role.members:
            if member.bot:
                continue

            target_members_by_id[member.id] = member

    return list(target_members_by_id.values())


def is_staff_member(member):
    return any(role.id in STAFF_ROLE_IDS for role in member.roles)


async def send_notice_reaction_report(guild, message_id):
    notice_message = await fetch_challenger_notice_message(message_id)
    reacted_user_ids = await get_notice_reacted_user_ids(notice_message)
    non_reactors = [
        member
        for member in get_target_notice_members(guild)
        if member.id not in reacted_user_ids and not is_staff_member(member)
    ]

    first_line = get_first_line(notice_message.content)
    non_reactors_list = "\n".join(member.display_name for member in non_reactors).replace("\n", "\n> ")
    statistics_channel = bot.get_channel(NOTICE_STATISTICS_CHANNEL_ID)

    if statistics_channel is None:
        raise RuntimeError("공지 통계 채널을 찾을 수 없습니다.")

    if non_reactors:
        await statistics_channel.send(f"🔴 {first_line}\n## 미응답자 목록\n> {non_reactors_list}")
    else:
        await statistics_channel.send(f"🔵 {first_line}\n> 모든 특정 역할의 사용자가 이모지를 달았습니다.")


def can_send_dm(member):
    if member.guild_permissions.administrator:
        return True

    return any(role.id in STAFF_ROLE_IDS for role in member.roles)


def build_score_dm_content(display_name, points, reason, dm_reason=None, custom_reason=False):
    display_reason = dm_reason or reason

    if points > 0:
        reason_text = (
            f"`{display_reason}`(으)로 인해, 상점 `+{points}점`을 부여합니다."
            if custom_reason
            else f"{display_reason} 상점 `+{points}점`을 부여합니다."
        )
        return (
            f"안녕하세요 `{display_name}`, UMC 운영진입니다.\n"
            f"{reason_text}\n"
            "감사합니다."
        )

    reason_text = (
        f"`{display_reason}`(으)로 인해, 감점 `{points}점`을 안내드립니다."
        if custom_reason
        else f"`{display_reason}`로 인한 감점 `{points}점`을 안내드립니다."
    )
    return (
        f"안녕하세요 `{display_name}`, UMC 운영진입니다.\n"
        f"{reason_text}\n"
        "감사합니다."
    )


SCORE_RULES = {
    "벌점-공지미체크": {
        "points": -2,
        "parameter": "notice_message_id",
        "usage_param": "메시지ID",
    },
    "벌점-과제미수행": {
        "points": -4,
        "parameter": "week",
        "reason_template": "{week} 과제 미수행",
        "usage_param": "n주차",
    },
    "벌점-스터디지각": {
        "points": -2,
        "parameter": "week",
        "reason_template": "{week} 스터디 지각",
        "usage_param": "n주차",
    },
    "벌점-스터디불참": {
        "points": -4,
        "parameter": "week",
        "reason_template": "{week} 스터디 불참",
        "usage_param": "n주차",
    },
    "벌점-행사지각": {
        "points": -2,
        "parameter": "event_name",
        "reason_template": "{event_name} 행사 지각",
        "usage_param": "행사명",
    },
    "벌점-중도퇴실": {
        "points": -2,
        "parameter": "event_name",
        "reason_template": "{event_name} 행사 중도 퇴실",
        "usage_param": "행사명",
    },
    "벌점-기간외취소": {
        "points": -4,
        "parameter": "event_name",
        "reason_template": "{event_name} 행사 기간 외 취소",
        "usage_param": "행사명",
    },
    "벌점-노쇼": {
        "points": -10,
        "parameter": "event_name",
        "reason_template": "{event_name} 노쇼 (결석)",
        "usage_param": "행사명",
    },
    "상점-블로그": {
        "points": 3,
        "parameter": "week",
        "reason_template": "{week} 블로그 챌린지",
        "dm_reason_template": "`{week} 블로그 챌린지` 참여로",
        "usage_param": "n주차",
    },
    "상점-베스트워크북": {
        "points": 2,
        "parameter": "week",
        "reason_template": "{week} 베스트 워크북 선정",
        "dm_reason_template": "`{week} 베스트 워크북`에 선정되어,",
        "usage_param": "n주차",
    },
    "상점-행사리뷰어": {
        "points": 1,
        "reason": "행사 리뷰어",
        "dm_reason": "`행사 리뷰어` 참여로",
    },
    "상점-중앙행사": {
        "points": 2,
        "reason": "중앙 행사 참여",
        "dm_reason": "`중앙 행사` 참여로",
    },
    "상점-지식인": {
        "points": 1,
        "reason": "지식인 채널 활동",
        "dm_reason": "`지식인 채널 활동`으로",
    },
}

PENALTY_HELP_ITEMS = [
    ("!벌점 맹덕/이용재 -2 사유", "커스텀 벌점을 부여합니다."),
    ("!벌점-공지미체크 맹덕/이용재 메시지ID", "벌점 2점을 자동 부여합니다. 해당 공지의 첫 행을 가져와 공지 미체크로 기록합니다."),
    ("!벌점-과제미수행 맹덕/이용재 n주차", "벌점 4점을 자동 부여합니다. 예: !벌점-과제미수행 맹덕/이용재 5주차"),
    ("!벌점-스터디지각 맹덕/이용재 n주차", "벌점 2점을 자동 부여합니다. 예: !벌점-스터디지각 맹덕/이용재 5주차"),
    ("!벌점-스터디불참 맹덕/이용재 n주차", "벌점 4점을 자동 부여합니다. 예: !벌점-스터디불참 맹덕/이용재 5주차"),
    ("!벌점-행사지각 맹덕/이용재 행사명", "벌점 2점을 자동 부여합니다. 행사명을 DM과 기록에 포함합니다."),
    ("!벌점-중도퇴실 맹덕/이용재 행사명", "벌점 2점을 자동 부여합니다. 행사명을 DM과 기록에 포함합니다."),
    ("!벌점-기간외취소 맹덕/이용재 행사명", "벌점 4점을 자동 부여합니다. 행사명을 DM과 기록에 포함합니다."),
    ("!벌점-노쇼 맹덕/이용재 행사명", "벌점 10점을 자동 부여합니다. 행사명을 DM과 기록에 포함합니다."),
]

BONUS_HELP_ITEMS = [
    ("!상점 맹덕/이용재 +2 사유", "커스텀 상점을 부여합니다."),
    ("!상점-블로그 맹덕/이용재 n주차", "상점 3점을 자동 부여합니다. 예: !상점-블로그 맹덕/이용재 5주차"),
    ("!상점-베스트워크북 맹덕/이용재 n주차", "상점 2점을 자동 부여합니다. 예: !상점-베스트워크북 맹덕/이용재 5주차"),
    ("!상점-행사리뷰어 맹덕/이용재", "상점 1점을 자동 부여합니다. 행사 후기 구글폼 제출 확인 시 사용합니다."),
    ("!상점-중앙행사 맹덕/이용재", "상점 2점을 자동 부여합니다. 중앙 행사 누적 2회 참여 시 사용합니다."),
    ("!상점-지식인 맹덕/이용재", "상점 1점을 자동 부여합니다. 지식인 채널 질문 또는 답변 작성 시 사용합니다."),
]

NOTICE_HELP_ITEMS = [
    ("!공지 공지글", "챌린저_공지 채널에 공지를 발송하고 확인 이모지를 추가합니다."),
    ("!공지-예약 2026-09-10 21:00 공지글", "지정한 시간에 공지를 예약 발송합니다."),
    ("!공지 메시지ID 수정할공지글", "기존 공지 메시지를 수정합니다."),
    ("!check 메시지ID", "해당 공지의 확인 이모지 미응답자를 수동 확인합니다."),
]

GENERAL_HELP_ITEMS = [
    ("!help", "전체 명령어 도움말을 보여줍니다."),
    ("!help-벌점", "벌점 명령어 도움말을 보여줍니다."),
    ("!help-상점", "상점 명령어 도움말을 보여줍니다."),
    ("!help-공지", "공지 명령어 도움말을 보여줍니다."),
    ("!점수초기화 맹덕/이용재", "대상자의 누적 점수를 0점으로 초기화하고 기록을 남깁니다."),
    ("!점수초기화-all", "모든 멤버의 누적 점수를 0점으로 초기화하고 기록을 남깁니다."),
    ("!sync-members", "대상 역할 멤버를 스프레드시트에 동기화합니다."),
]


def build_help_section(title, items):
    lines = [f"## {title}"]
    for item in items:
        command, description = item
        lines.append(f"`{command}` - {description}")

    return "\n".join(lines)


def build_all_help_content():
    return "\n\n".join([
        build_help_section("기본", GENERAL_HELP_ITEMS),
        build_help_section("벌점", PENALTY_HELP_ITEMS),
        build_help_section("상점", BONUS_HELP_ITEMS),
        build_help_section("공지", NOTICE_HELP_ITEMS),
    ])


def get_score_command_usage():
    usages = []
    for command, rule in SCORE_RULES.items():
        usage = f"!{command} 맹덕/이용재"
        if rule.get("usage_param"):
            usage += f" {rule['usage_param']}"
        usages.append(usage)

    return "\n".join(usages)


def format_rule_usage(command_name, rule):
    usage = f"!{command_name} 맹덕/이용재"
    if rule.get("usage_param"):
        usage += f" {rule['usage_param']}"

    return usage


def parse_week(extra):
    week = extra.strip()
    if re.fullmatch(r"\d+주차", week) is None:
        raise ValueError("주차는 5주차처럼 입력해주세요.")

    return week


def parse_custom_score(score_text):
    match = re.fullmatch(r"([+-]?\d+)점?", score_text.strip())
    if match is None:
        raise ValueError("점수는 +2 또는 -2처럼 입력해주세요.")

    points = int(match.group(1))
    if points == 0:
        raise ValueError("점수는 0점이 아닌 값으로 입력해주세요.")

    return points


async def get_notice_first_line(message_id):
    challenger_notice_channel = bot.get_channel(CHALLENGER_NOTICE_CHANNEL_ID)
    if challenger_notice_channel is None:
        raise RuntimeError("챌린저_공지 채널을 찾을 수 없습니다.")

    try:
        notice_message = await challenger_notice_channel.fetch_message(message_id)
    except discord.NotFound:
        raise ValueError(f"메시지 {message_id}를 찾을 수 없습니다.")
    except discord.HTTPException as error:
        raise ValueError(f"메시지 {message_id} 조회 중 오류가 발생했습니다: {error}")

    first_line = get_first_line(notice_message.content).strip()
    if not first_line:
        raise ValueError(f"메시지 {message_id}의 첫 행이 비어 있습니다.")

    return first_line


async def build_score_context(ctx, rule, extra):
    parameter = rule.get("parameter")
    extra = extra.strip()

    if parameter is None:
        if extra:
            raise ValueError(f"`!{ctx.invoked_with}` 명령어는 대상자만 입력해주세요.")

        return rule["reason"], rule.get("dm_reason")

    if not extra:
        raise ValueError("필수 파라미터가 빠졌습니다.")

    if parameter == "week":
        week = parse_week(extra)
        reason = rule["reason_template"].format(week=week)
        dm_reason_template = rule.get("dm_reason_template", rule["reason_template"])
        return reason, dm_reason_template.format(week=week)

    if parameter == "notice_message_id":
        if re.fullmatch(r"\d+", extra) is None:
            raise ValueError("공지 미체크 메시지 ID는 숫자로 입력해주세요.")

        message_id = int(extra)
        notice_first_line = await get_notice_first_line(message_id)
        reason = f"공지 미체크: {notice_first_line} ({message_id})"
        dm_reason = f"{notice_first_line} 공지 미체크"
        return reason, dm_reason

    if parameter == "event_name":
        reason = rule["reason_template"].format(event_name=extra)
        return reason, reason

    raise RuntimeError(f"지원하지 않는 점수 명령어 파라미터입니다: {parameter}")


def parse_scheduled_notice_command_time(date_text, time_text):
    now = datetime.now()
    date_text = date_text.strip()
    time_text = time_text.strip()

    date_match = re.fullmatch(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})", date_text)
    if date_match:
        year, month, day = map(int, date_match.groups())
    else:
        date_match = re.fullmatch(r"(\d{1,2})[-/.](\d{1,2})", date_text)
        if date_match:
            year = now.year
            month, day = map(int, date_match.groups())
        else:
            date_match = re.fullmatch(r"(\d{1,2})월(\d{1,2})일?", date_text)
            if date_match is None:
                raise ValueError("예약 날짜는 2026-09-10, 9-10, 9월10일 형식으로 입력해주세요.")

            year = now.year
            month, day = map(int, date_match.groups())

    time_match = re.fullmatch(r"(\d{1,2}):(\d{1,2})", time_text)
    if time_match:
        hour, minute = map(int, time_match.groups())
    else:
        time_match = re.fullmatch(r"(\d{1,2})시(?:(\d{1,2})분?)?", time_text)
        if time_match is None:
            raise ValueError("예약 시간은 21:00 또는 21시 형식으로 입력해주세요.")

        hour = int(time_match.group(1))
        minute = int(time_match.group(2) or 0)

    return datetime(year, month, day, hour, minute)


async def handle_score_command(ctx, member_key, points, reason, expected_score_type=None, dm_reason=None, custom_reason=False):
    if ctx.channel.id != SCORE_COMMAND_CHANNEL_ID:
        await ctx.send("상/벌점 명령어는 지정된 채널에서만 사용할 수 있습니다.")
        return

    if not can_send_dm(ctx.author):
        await ctx.send("상/벌점 부여 권한이 없습니다.")
        return

    if points == 0:
        await ctx.send("점수는 + 또는 - 값으로 입력해주세요.")
        return

    if expected_score_type == "상점" and points < 0:
        await ctx.send("상점은 + 점수로 입력해주세요. 예: !상점 맹덕 2주차 베스트 워크북 +2점")
        return

    if expected_score_type == "벌점" and points > 0:
        await ctx.send("벌점은 - 점수로 입력해주세요. 예: !벌점 맹덕 5주차 미션 미제출 -4점")
        return

    member = await find_one_member_for_score(ctx, member_key)
    if member is None:
        return

    new_score, score_row_index = await asyncio.to_thread(
        add_score,
        member,
        points,
        reason,
        str(ctx.author),
    )

    score_type = "상점" if points > 0 else "벌점"
    dm_content = build_score_dm_content(
        member["display_name"],
        points,
        reason,
        dm_reason,
        custom_reason,
    )
    success, response = await send_direct_message(bot, int(member["user_id"]), dm_content)
    try:
        await asyncio.to_thread(update_score_dm_result, score_row_index, success, response)
        dm_record_response = ""
    except Exception as error:
        dm_record_response = f" DM 결과 기록 업데이트는 실패했습니다: {error}"

    await ctx.send(f"{member['display_name']} 님에게 {score_type} {points:+d}점을 반영했습니다. {response}{dm_record_response}")


async def find_one_member_for_score(ctx, member_key):
    matched_members = await asyncio.to_thread(find_member, member_key)

    if not matched_members:
        await ctx.send("스프레드시트에서 대상 멤버를 찾을 수 없습니다. 먼저 !sync-members를 실행해주세요.")
        return None

    if len(matched_members) > 1:
        names = ", ".join(member["display_name"] for member in matched_members)
        await ctx.send(f"동명이인이 있습니다. 사용자 ID로 다시 입력해주세요: {names}")
        return None

    return matched_members[0]


async def handle_fixed_score_command(ctx, member_key: str, *, extra=""):
    rule = SCORE_RULES[ctx.command.name]

    try:
        reason, dm_reason = await build_score_context(ctx, rule, extra)
    except ValueError as error:
        await ctx.send(f"{error} 예: {format_rule_usage(ctx.invoked_with, rule)}")
        return
    except RuntimeError as error:
        await ctx.send(str(error))
        return

    await handle_score_command(ctx, member_key, rule["points"], reason, dm_reason=dm_reason)


for score_command_name in SCORE_RULES:
    bot.command(name=score_command_name)(handle_fixed_score_command)


async def register_scheduled_notice(ctx, send_at, message_content):
    if not can_send_dm(ctx.author):
        await ctx.send("예약 공지 등록 권한이 없습니다.")
        return

    if send_at <= datetime.now():
        await ctx.send("현재보다 이후 시간으로 예약해주세요.")
        return

    scheduled_notice_id = await asyncio.to_thread(
        add_scheduled_notice,
        send_at,
        message_content,
        str(ctx.author),
    )
    schedule_notice_task(scheduled_notice_id, send_at, message_content)
    await ctx.send(f"예약 공지 #{scheduled_notice_id} 등록 완료: {send_at:%Y-%m-%d %H:%M}")


@bot.command(name='sync-members')
async def sync_members_command(ctx):
    if ctx.channel.id != SCORE_COMMAND_CHANNEL_ID:
        await ctx.send("멤버 동기화 명령어는 지정된 상/벌점 채널에서만 사용할 수 있습니다.")
        return

    if not can_send_dm(ctx.author):
        await ctx.send("멤버 동기화 권한이 없습니다.")
        return

    target_members_by_id = {}

    for role_id in TARGET_ROLE_IDS:
        target_role = ctx.guild.get_role(role_id)
        if target_role is None:
            continue

        for member in target_role.members:
            if member.bot:
                continue

            target_members_by_id[member.id] = {
                "user_id": member.id,
                "display_name": member.display_name,
                "name": str(member),
                "roles": [
                    role.name
                    for role in member.roles
                    if role.id in TARGET_ROLE_IDS
                ],
            }

    saved_count = await asyncio.to_thread(sync_members, list(target_members_by_id.values()))
    await ctx.send(f"대상 역할 멤버 {saved_count}명을 스프레드시트에 저장했습니다.")


@bot.command(name='help')
async def help_command(ctx):
    await ctx.send(build_all_help_content())


@bot.command(name='help-벌점')
async def help_penalty_command(ctx):
    await ctx.send(build_help_section("벌점", PENALTY_HELP_ITEMS))


@bot.command(name='help-상점')
async def help_bonus_command(ctx):
    await ctx.send(build_help_section("상점", BONUS_HELP_ITEMS))


@bot.command(name='help-공지')
async def help_notice_command(ctx):
    await ctx.send(build_help_section("공지", NOTICE_HELP_ITEMS))


@bot.command(name='score')
async def score(ctx, *args):
    await ctx.send(
        "점수는 한글 명령어로 부여해주세요.\n"
        "`!상점 맹덕/이용재 +2 사유`\n"
        "`!벌점 맹덕/이용재 -2 사유`\n\n"
        + get_score_command_usage()
    )


@bot.command(name='상점')
async def bonus_score(ctx, member_key: str = None, score_text: str = None, *, reason=""):
    if member_key is None or score_text is None or not reason.strip():
        await ctx.send("커스텀 상점 형식: !상점 맹덕/이용재 +2 사유")
        return

    try:
        points = parse_custom_score(score_text)
    except ValueError as error:
        await ctx.send(str(error))
        return

    if points < 0:
        await ctx.send("상점은 + 점수로 입력해주세요. 예: !상점 맹덕/이용재 +2 사유")
        return

    await handle_score_command(ctx, member_key, points, reason.strip(), "상점", custom_reason=True)


@bot.command(name='벌점')
async def penalty_score(ctx, member_key: str = None, score_text: str = None, *, reason=""):
    if member_key is None or score_text is None or not reason.strip():
        await ctx.send("커스텀 벌점 형식: !벌점 맹덕/이용재 -2 사유")
        return

    try:
        points = parse_custom_score(score_text)
    except ValueError as error:
        await ctx.send(str(error))
        return

    if points > 0:
        await ctx.send("벌점은 - 점수로 입력해주세요. 예: !벌점 맹덕/이용재 -2 사유")
        return

    await handle_score_command(ctx, member_key, points, reason.strip(), "벌점", custom_reason=True)


@bot.command(name='점수초기화')
async def reset_member_score(ctx, member_key: str, *, extra=""):
    if ctx.channel.id != SCORE_COMMAND_CHANNEL_ID:
        await ctx.send("점수 초기화 명령어는 지정된 상/벌점 채널에서만 사용할 수 있습니다.")
        return

    if not can_send_dm(ctx.author):
        await ctx.send("점수 초기화 권한이 없습니다.")
        return

    if extra.strip():
        await ctx.send("점수 초기화 명령어는 대상자만 입력해주세요. 예: !점수초기화 맹덕/이용재")
        return

    member = await find_one_member_for_score(ctx, member_key)
    if member is None:
        return

    previous_score = await asyncio.to_thread(reset_score, member, str(ctx.author))
    await ctx.send(
        f"{member['display_name']} 님의 점수를 초기화했습니다. "
        f"이전 점수: {previous_score:+d}점"
    )


@bot.command(name='점수초기화-all')
async def reset_all_member_scores(ctx):
    if ctx.channel.id != SCORE_COMMAND_CHANNEL_ID:
        await ctx.send("점수 초기화 명령어는 지정된 상/벌점 채널에서만 사용할 수 있습니다.")
        return

    if not can_send_dm(ctx.author):
        await ctx.send("점수 초기화 권한이 없습니다.")
        return

    reset_count = await asyncio.to_thread(reset_all_scores, str(ctx.author))
    await ctx.send(f"모든 멤버 {reset_count}명의 점수를 초기화했습니다.")


@bot.command(name='dm상태복구')
async def recover_dm_status(ctx):
    if ctx.channel.id != SCORE_COMMAND_CHANNEL_ID:
        await ctx.send("DM 상태 복구 명령어는 지정된 상/벌점 채널에서만 사용할 수 있습니다.")
        return

    if not can_send_dm(ctx.author):
        await ctx.send("DM 상태 복구 권한이 없습니다.")
        return

    updated_count = await asyncio.to_thread(mark_pending_score_dm_results_unknown)
    await ctx.send(f"pending DM 상태 {updated_count}건을 unknown으로 정리했습니다.")


@bot.command(name='notice-schd')
async def notice_schd(ctx, year: int, month: int, day: int, hour: int, minute: int, *, message_content):
    try:
        send_at = datetime(year, month, day, hour, minute)
    except ValueError:
        await ctx.send("예약 날짜/시간 형식이 올바르지 않습니다.")
        return

    await register_scheduled_notice(ctx, send_at, message_content)


@bot.command(name='공지-예약')
async def korean_notice_schd(ctx, date_text: str, time_text: str, *, message_content):
    try:
        send_at = parse_scheduled_notice_command_time(date_text, time_text)
    except ValueError as error:
        await ctx.send(str(error))
        return

    await register_scheduled_notice(ctx, send_at, message_content)


@bot.command(name='notice', aliases=['공지'])
async def notice(ctx, message_id: typing.Optional[int], *, message_content):
    challenger_notice_channel = bot.get_channel(CHALLENGER_NOTICE_CHANNEL_ID)

    if challenger_notice_channel is None:
        await ctx.send("챌린저_공지 채널을 찾을 수 없습니다.")
        return

    if message_id is None:
        # 새 공지 생성 로직
        notice_message = await challenger_notice_channel.send(message_content)
        await ctx.send(f"{message_content.splitlines()[0]} 공지의 메시지 ID: {notice_message.id}")
        
        # 커스텀 이모지 사용하도록 설정
        await notice_message.add_reaction(NOTICE_CONFIRM_EMOJI_TEXT)
        schedule_notice_reaction_check_task(ctx.guild, notice_message.id)
    else:
        # 기존 공지 수정 로직
        try:
            notice_message = await challenger_notice_channel.fetch_message(message_id)
            await notice_message.edit(content=message_content)
            await ctx.send(f"메시지 {message_id}가 성공적으로 수정되었습니다.")
        except discord.NotFound:
            await ctx.send(f"메시지 {message_id}를 찾을 수 없습니다.")

@bot.command(name='check')
async def check(ctx, message_id: int):
    """수동으로 리액션 확인하는 명령어"""
    try:
        await send_notice_reaction_report(ctx.guild, message_id)
        await ctx.send("리액션 확인 완료!")
    except discord.NotFound:
        await ctx.send(f"메시지 {message_id}를 찾을 수 없습니다.")
    except RuntimeError as error:
        await ctx.send(str(error))

bot.run(DISCORD_BOT_TOKEN)

import discord
from discord.ext import commands
import asyncio
import os
import typing
from datetime import datetime

from dm_sender import send_direct_message
from env_loader import load_env_file
from spreadsheet_store import (
    add_scheduled_notice,
    add_score,
    find_member,
    get_pending_scheduled_notices,
    parse_scheduled_notice_time,
    sync_members,
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

bot = commands.Bot(command_prefix="!", intents=intents)
scheduled_notice_tasks = {}

DISCORD_BOT_TOKEN = get_required_env("DISCORD_BOT_TOKEN")

# 채널 ID
BOT_NOTICE_CHANNEL_ID = get_required_int_env("BOT_NOTICE_CHANNEL_ID")
CHALLENGER_NOTICE_CHANNEL_ID = get_required_int_env("CHALLENGER_NOTICE_CHANNEL_ID")
NOTICE_STATISTICS_CHANNEL_ID = get_required_int_env("NOTICE_STATISTICS_CHANNEL_ID")
SCORE_COMMAND_CHANNEL_ID = get_required_int_env("SCORE_COMMAND_CHANNEL_ID")

# 역할 ID
TARGET_ROLE_IDS = get_required_int_list_env("TARGET_ROLE_IDS")
STAFF_ROLE_IDS = get_required_int_list_env("STAFF_ROLE_IDS")

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


async def run_scheduled_notice(scheduled_notice_id, send_at, message_content):
    try:
        delay = max((send_at - datetime.now()).total_seconds(), 0)
        if delay > 0:
            await asyncio.sleep(delay)

        await send_scheduled_notice(scheduled_notice_id, message_content)
    finally:
        scheduled_notice_tasks.pop(scheduled_notice_id, None)


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
        await notice_message.add_reaction("<:gachon:1019827185676197918>")
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


def can_send_dm(member):
    if member.guild_permissions.administrator:
        return True

    return any(role.id in STAFF_ROLE_IDS for role in member.roles)


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


@bot.command(name='score')
async def score(ctx, member_key: str, points: int, *, reason):
    if ctx.channel.id != SCORE_COMMAND_CHANNEL_ID:
        await ctx.send("상/벌점 명령어는 지정된 채널에서만 사용할 수 있습니다.")
        return

    if not can_send_dm(ctx.author):
        await ctx.send("상/벌점 부여 권한이 없습니다.")
        return

    if points == 0:
        await ctx.send("점수는 + 또는 - 값으로 입력해주세요.")
        return

    matched_members = await asyncio.to_thread(find_member, member_key)

    if not matched_members:
        await ctx.send("스프레드시트에서 대상 멤버를 찾을 수 없습니다. 먼저 !sync-members를 실행해주세요.")
        return

    if len(matched_members) > 1:
        names = ", ".join(member["display_name"] for member in matched_members)
        await ctx.send(f"동명이인이 있습니다. 사용자 ID로 다시 입력해주세요: {names}")
        return

    member = matched_members[0]
    new_score = await asyncio.to_thread(
        add_score,
        member,
        points,
        reason,
        str(ctx.author),
    )

    score_type = "상점" if points > 0 else "벌점"
    dm_content = (
        f"{score_type} {points:+d}점이 부여되었습니다.\n"
        f"사유: {reason}\n"
        f"현재 점수: {new_score:+d}점"
    )
    _success, response = await send_direct_message(bot, int(member["user_id"]), dm_content)
    await ctx.send(f"{member['display_name']} 님에게 {score_type} {points:+d}점을 반영했습니다. {response}")


@bot.command(name='notice-schd')
async def notice_schd(ctx, year: int, month: int, day: int, hour: int, minute: int, *, message_content):
    if not can_send_dm(ctx.author):
        await ctx.send("예약 공지 등록 권한이 없습니다.")
        return

    try:
        send_at = datetime(year, month, day, hour, minute)
    except ValueError:
        await ctx.send("예약 날짜/시간 형식이 올바르지 않습니다.")
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


@bot.command(name='notice')
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
        emoji = "<:gachon:1019827185676197918>"
        await notice_message.add_reaction(emoji)
    else:
        # 기존 공지 수정 로직
        try:
            notice_message = await challenger_notice_channel.fetch_message(message_id)
            await notice_message.edit(content=message_content)
            await ctx.send(f"메시지 {message_id}가 성공적으로 수정되었습니다.")
        except discord.NotFound:
            await ctx.send(f"메시지 {message_id}를 찾을 수 없습니다.")

    # 공지 보낸 후 1일 뒤에 리액션 확인
    await asyncio.sleep(86400)  # 1일 = 86400초

    # 메시지 리액션 추가한 챌린저 확인
    notice_message = await challenger_notice_channel.fetch_message(notice_message.id)
    reaction = discord.utils.get(notice_message.reactions, emoji=discord.PartialEmoji(name="gachon", id=1019827185676197918))

    if reaction is not None:
        users_who_reacted = [user async for user in reaction.users() if not user.bot]
    else:
        users_who_reacted = []

    # 특정 역할 필터링
    non_reactors = []
    for role_id in TARGET_ROLE_IDS:
        target_role = ctx.guild.get_role(role_id)
        if target_role is not None:
            members_with_role = [member for member in ctx.guild.members if target_role in member.roles]
            non_reactors.extend([member for member in members_with_role if member.id not in [user.id for user in users_who_reacted]])

    # 운영진 필터링
    final_non_reactors = []
    for member in non_reactors:
        has_staff_role = any(staff_role_id in [role.id for role in member.roles] for staff_role_id in STAFF_ROLE_IDS)
        if not has_staff_role:
            final_non_reactors.append(member)

    # 메시지 첫 줄 추출
    first_line = message_content.splitlines()[0]

    # 미응답자 목록에서 줄바꿈 처리
    non_reactors_list = "\n".join([member.display_name for member in final_non_reactors]).replace('\n', '\n> ')

    # 통계 채널 변수 생성
    statistics_channel = bot.get_channel(NOTICE_STATISTICS_CHANNEL_ID)
    
    # 미응답자 목록 출력
    if final_non_reactors:
        response = f"🔴 {first_line}\n## 미응답자 목록\n> {non_reactors_list}"
        await statistics_channel.send(response)
    else:
        response = f"🔵 {first_line}\n> 모든 특정 역할의 사용자가 이모지를 달았습니다."
        await statistics_channel.send(response)

@bot.command(name='check')
async def check(ctx, message_id: int):
    """수동으로 리액션 확인하는 명령어"""
    challenger_notice_channel = bot.get_channel(CHALLENGER_NOTICE_CHANNEL_ID)
    
    try:
        notice_message = await challenger_notice_channel.fetch_message(message_id)
        
        users_who_reacted = []
        for reaction in notice_message.reactions:
            # 커스텀 이모지 확인 - ID로 정확하게 확인
            if hasattr(reaction.emoji, 'id') and reaction.emoji.id == 1019827185676197918:
                async for user in reaction.users():
                    if not user.bot:
                        users_who_reacted.append(user)
                break

        # 특정 역할 필터링
        non_reactors = []
        for role_id in TARGET_ROLE_IDS:
            target_role = ctx.guild.get_role(role_id)
            if target_role is not None:
                members_with_role = [member for member in ctx.guild.members if target_role in member.roles]
                non_reactors.extend([member for member in members_with_role if member not in users_who_reacted])

        # 운영진 필터링
        final_non_reactors = []
        for member in non_reactors:
            has_staff_role = any(staff_role_id in [role.id for role in member.roles] for staff_role_id in STAFF_ROLE_IDS)
            if not has_staff_role:
                final_non_reactors.append(member)

        # 메시지 첫 줄 추출
        first_line = notice_message.content.splitlines()[0]

        # 미응답자 목록에서 줄바꿈 처리
        non_reactors_list = "\n".join([member.display_name for member in final_non_reactors]).replace('\n', '\n> ')

        # 통계 채널 변수 생성
        statistics_channel = bot.get_channel(NOTICE_STATISTICS_CHANNEL_ID)
        
        # 미응답자 목록 출력
        if final_non_reactors:
            response = f"🔴 {first_line}\n## 미응답자 목록\n> {non_reactors_list}"
            await statistics_channel.send(response)
        else:
            response = f"🔵 {first_line}\n> 모든 특정 역할의 사용자가 이모지를 달았습니다."
            await statistics_channel.send(response)
            
        await ctx.send("리액션 확인 완료!")
        
    except discord.NotFound:
        await ctx.send(f"메시지 {message_id}를 찾을 수 없습니다.")

bot.run(DISCORD_BOT_TOKEN)

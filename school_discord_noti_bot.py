import discord
from discord.ext import commands
import asyncio
import os
import typing


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

DISCORD_BOT_TOKEN = get_required_env("DISCORD_BOT_TOKEN")

# 채널 ID
BOT_NOTICE_CHANNEL_ID = get_required_int_env("BOT_NOTICE_CHANNEL_ID")
CHALLENGER_NOTICE_CHANNEL_ID = get_required_int_env("CHALLENGER_NOTICE_CHANNEL_ID")
NOTICE_STATISTICS_CHANNEL_ID = get_required_int_env("NOTICE_STATISTICS_CHANNEL_ID")

# 역할 ID
TARGET_ROLE_IDS = get_required_int_list_env("TARGET_ROLE_IDS")
STAFF_ROLE_IDS = get_required_int_list_env("STAFF_ROLE_IDS")

# 커스텀 이모지
NOTICE_REACTION_EMOJI_NAME = "gachon"
NOTICE_REACTION_EMOJI_ID = 1019827185676197918

@bot.event
async def on_ready():
    print(f'{bot.user} 이 활성화 되었습니다!')

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
        emoji = f"<:{NOTICE_REACTION_EMOJI_NAME}:{NOTICE_REACTION_EMOJI_ID}>"
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
    reaction = discord.utils.get(
        notice_message.reactions,
        emoji=discord.PartialEmoji(name=NOTICE_REACTION_EMOJI_NAME, id=NOTICE_REACTION_EMOJI_ID),
    )

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
            if hasattr(reaction.emoji, 'id') and reaction.emoji.id == NOTICE_REACTION_EMOJI_ID:
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

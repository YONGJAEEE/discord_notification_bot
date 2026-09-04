import discord


async def send_direct_message(bot, user_id: int, message_content: str):
    user = bot.get_user(user_id)

    if user is None:
        user = await bot.fetch_user(user_id)

    if user is None:
        return False, "대상 사용자를 찾을 수 없습니다."

    try:
        await user.send(message_content)
    except discord.Forbidden:
        return False, "대상 사용자가 DM을 받을 수 없는 상태입니다."
    except discord.HTTPException as error:
        return False, f"DM 전송 중 오류가 발생했습니다: {error}"

    return True, f"{user} 님에게 DM을 전송했습니다."

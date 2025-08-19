import itertools

from nonebot.adapters import Bot, Event
from nonebot_plugin_alconna import MsgTarget, get_target

from ..game import Game, get_running_games
# 导入正在准备的游戏列表
from ._prepare_game import preparing_games


def user_in_game(self_id: str, user_id: str, group_id: str | None) -> bool:
    # 检查已开始的游戏
    if group_id is None:
        # 私聊时检查所有正在运行的游戏
        is_in_running_game = any(
            p.user.self_id == self_id and p.user_id == user_id
            for p in itertools.chain(*[g.players for g in get_running_games().values()])
        )
        if is_in_running_game:
            return True
    else:
        # 群聊时检查特定群组的正在运行的游戏
        def check_running(game: Game) -> bool:
            return self_id == game.group.self_id and group_id == game.group.id

        if game := next(filter(check_running, get_running_games().values()), None):
            if any(p.user_id == user_id for p in game.players):
                return True

    # 检查正在准备的游戏
    if group_id:
        for target, prepare_game in preparing_games.items():
            if target.id == group_id and user_id in prepare_game.players:
                return True

    return False


async def rule_in_game(bot: Bot, event: Event) -> bool:
    # running_games 和 preparing_games 任意一个不为空，就说明有游戏活动
    if not get_running_games() and not preparing_games:
        return False

    try:
        target = get_target(event, bot)
    except NotImplementedError:
        return False

    if target.private:
        return user_in_game(bot.self_id, target.id, None)

    try:
        user_id = event.get_user_id()
    except Exception:
        return False

    return user_in_game(bot.self_id, user_id, target.id)


async def rule_not_in_game(bot: Bot, event: Event) -> bool:
    return not await rule_in_game(bot, event)


async def is_group(target: MsgTarget) -> bool:
    return not target.private
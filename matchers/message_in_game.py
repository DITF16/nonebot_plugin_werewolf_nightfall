import contextlib

from nonebot import on_message
from nonebot.adapters import Bot, Event
from nonebot.exception import ActionFailed
from nonebot.rule import Rule
from nonebot_plugin_alconna import Alconna, MsgTarget, UniMessage, UniMsg, on_alconna

from ..config import config
from ..constant import STOP_COMMAND
from ..game import get_running_games
from ..models import GameContext
from ..utils import InputStore
from .depends import is_group, rule_in_game

# 修正了 Rule 的组合方式，使用 Rule() 构造函数而不是 '&'
game_chat_handler = on_message(
    rule=Rule(rule_in_game, is_group),  # 仅处理游戏中的群聊消息
    priority=config.matcher_priority.in_game,
    block=True,  # 阻止事件继续传播
)


@game_chat_handler.handle()
async def _(bot: Bot, event: Event, target: MsgTarget):
    game = get_running_games().get(target)
    # 如果游戏不存在或已结束，则不处理
    if not game:
        return

    user_id = event.get_user_id()
    player = game._player_map.get(user_id)

    # 1. 处理死亡玩家在群聊中的发言
    if player and not player.alive:
        with contextlib.suppress(ActionFailed):  # 机器人可能没有管理员权限
            # 修正：使用 send 发送警告，然后 finish 结束处理
            await game_chat_handler.send("🤫死者请在死者频道发言，不要在群里打扰活着的玩家哦。")
            # 尝试撤回消息，需要从 event 中获取 message_id
            if hasattr(event, "message_id"):
                await bot.delete_msg(message_id=event.message_id)
        await game_chat_handler.finish()  # 结束事件处理

    current_state = game.context.state

    # 2. 如果是夜晚，活着的玩家不允许在群聊发言
    if current_state == GameContext.State.NIGHT and player and player.alive:
        with contextlib.suppress(ActionFailed):
            await game_chat_handler.send("🤫夜深了，活人请保持安静，有事请私聊我。")
            if hasattr(event, "message_id"):
                await bot.delete_msg(message_id=event.message_id)
        await game_chat_handler.finish()

    # 3. 将合规的发言（如自由讨论或按顺序发言）放入 InputStore
    # 原有的 handle_input 逻辑是正确的，这里我们重新实现它
    InputStore.put(UniMsg(event.get_message()), user_id, target.id)


# 保留原有的 stop 命令处理逻辑，并修正逻辑
stopcmd = on_alconna(
    Alconna(config.get_stop_command()[0]),
    rule=rule_in_game,
    block=True,
    aliases=set(aliases) if (aliases := config.get_stop_command()[1:]) else None,
    use_cmd_start=config.use_cmd_start,
    priority=config.matcher_priority.stop,
)


@stopcmd.handle()
async def handle_stopcmd(event: Event, target: MsgTarget) -> None:
    # 私聊消息和群聊消息分开处理
    if target.private:
        InputStore.put(UniMessage.text(STOP_COMMAND), target.id)
    else:
        InputStore.put(UniMessage.text(STOP_COMMAND), event.get_user_id(), target.id)
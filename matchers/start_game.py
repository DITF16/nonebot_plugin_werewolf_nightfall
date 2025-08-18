import json
import anyio
from nonebot.internal.matcher import current_bot
from nonebot.rule import Rule, to_me
from nonebot.typing import T_State
from nonebot_plugin_alconna import (
    Alconna,
    FallbackStrategy,
    MsgTarget,
    Option,
    Target,
    UniMessage,
    UniMsg,
    on_alconna,
)
from nonebot_plugin_localstore import get_plugin_data_file
from nonebot_plugin_uninfo import QryItrface, Uninfo
import nonebot_plugin_waiter.unimsg as waiter

from ..config import GameBehavior, config, stop_command_prompt
from ..game import Game, get_running_games
from ..utils import extract_session_member_nick
from ._prepare_game import PrepareGame, solve_button
from .depends import rule_not_in_game
from .poke import poke_enabled

start_game = on_alconna(
    Alconna(
        "werewolf",
        Option("restart|-r|--restart|重开", dest="restart"),
    ),
    rule=to_me() & rule_not_in_game
    if config.get_require_at("start")
    else rule_not_in_game,
    aliases={"狼人杀"},
    use_cmd_start=config.use_cmd_start,
    priority=config.matcher_priority.start,
)
player_data_file = get_plugin_data_file("players.json")
if not player_data_file.exists():
    player_data_file.write_text("[]")


def dump_players(target: Target, players: dict[str, str]) -> None:
    data: list[dict] = json.loads(player_data_file.read_text(encoding="utf-8"))

    for item in data:
        if Target.load(item["target"]).verify(target):
            item["players"] = players
            break
    else:
        data.append({"target": target.dump(), "players": players})

    player_data_file.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def load_players(target: Target) -> dict[str, str] | None:
    for item in json.loads(player_data_file.read_text(encoding="utf-8")):
        if Target.load(item["target"]).verify(target):
            return item["players"]
    return None


@start_game.handle()
async def handle_notice(target: MsgTarget) -> None:
    if target.private:
        await UniMessage("⚠️请在群组中创建新游戏").finish(reply_to=True)
    if target in get_running_games():
        await (
            UniMessage.text("⚠️当前群组内有正在进行的游戏\n")
            .text("无法开始新游戏")
            .finish(reply_to=True)
        )

    msg = UniMessage.text(
        "🎉成功创建游戏\n\n"
        "  玩家请发送 “加入游戏”、“退出游戏”\n"
        "  玩家发送 “当前玩家” 可查看玩家列表\n"
        "  游戏发起者发送 “结束游戏” 可结束当前游戏\n"
        "  玩家均加入后，游戏发起者请发送 “开始游戏”\n"
    )
    if poke_enabled():
        msg.text(f"\n💫可使用戳一戳代替游戏交互中的 “{stop_command_prompt}” 命令\n")

    prepare_timeout = GameBehavior.get().timeout.prepare
    msg.text(f"\nℹ️游戏准备阶段限时{prepare_timeout / 60:.1f}分钟，超时将自动结束")
    await solve_button(msg).send(reply_to=True, fallback=FallbackStrategy.ignore)


@start_game.assign("restart")
async def handle_restart(target: MsgTarget, state: T_State) -> None:
    players = load_players(target)
    if players is None:
        await UniMessage.text("ℹ️未找到历史游戏记录，将创建新游戏").send()
        return

    msg = UniMessage.text("🎉成功加载上次游戏:\n")
    for user in players:
        msg.text("\n- ").at(user)
    await msg.send()

    state["players"] = players


@start_game.handle()
async def handle_start(
    state: T_State,
    session: Uninfo,
    target: MsgTarget,
    interface: QryItrface,
) -> None:
    players: dict[str, str] = state.get("players", {})
    admin_id = session.user.id
    admin_name = extract_session_member_nick(session) or admin_id
    players[admin_id] = admin_name

    prepare_game = PrepareGame(admin_id, players)
    with anyio.move_on_after(GameBehavior.get().timeout.prepare) as scope:
        await prepare_game.run()
    if scope.cancelled_caught:
        await UniMessage.text("⚠️游戏准备超时，已自动结束").finish(reply_to=True)

    if not prepare_game.shoud_start_game:
        return

    bot = current_bot.get()
    await UniMessage.text("游戏即将开始，正在进行私聊连通性测试...").send(target)

    while True:
        failed_players = {}
        for user_id, user_name in players.items():
            private_target = Target(
                user_id,
                private=True,
                self_id=bot.self_id,
                scope=target.scope,
                adapter=target.adapter,
                extra=target.extra,
            )
            try:
                test_msg = UniMessage.text(
                    "【狼人杀】私聊连通性测试，收到此消息说明您可以正常进行游戏。"
                )
                await test_msg.send(private_target, bot)
                await anyio.sleep(0.5)
            except Exception:
                failed_players[user_id] = user_name

        if not failed_players:
            break

        error_msg = UniMessage.text("以下玩家私聊发送失败，请添加机器人为好友或检查私聊设置：\n")
        for user_id, user_name in failed_players.items():
            error_msg.at(user_id).text(f" {user_name}\n")
        error_msg.text("\n问题解决后，请游戏发起者发送“重试”继续。")
        await error_msg.send(target)

        @waiter.waiter(
            waits=["message"],
            keep_session=False,
            rule=Rule(lambda event: event.get_user_id() == admin_id),
        )
        def wait_retry(msg: UniMsg):
            return msg.extract_plain_text().strip()

        try:
            with anyio.fail_after(GameBehavior.get().timeout.prepare):
                while True:
                    retry_cmd = wait_retry()
                    if retry_cmd == "重试":
                        break
                    if retry_cmd == "取消":
                        await UniMessage.text("游戏发起者取消了游戏。").send(target)
                        return
        except TimeoutError:
            await UniMessage.text("⚠️等待重试超时，游戏创建已取消。").send(target)
            return

    await UniMessage.text("✅所有玩家私聊测试通过，正在分配身份...").send(target)

    dump_players(target, players)
    game = await Game.new(target, set(players), interface)
    game.start()
import asyncio
import random

from nonebot.permission import SUPERUSER
from nonebot_plugin_alconna import Alconna, Args, Match, MsgTarget, UniMessage, on_alconna
from nonebot_plugin_uninfo import Interface, Member, QryItrface, SceneType, Uninfo

from ..config import config
from ..constant import FAKE_USER_ID_START
from ..game import Game, get_running_games
from ..models import GameContext, Role
from ..player import Player
from ..utils import InputStore, extract_session_member_nick


class MockInterface(Interface):
    """
    一个模拟的 uninfo 查询接口。
    用于拦截对虚拟机器人玩家的信息查询，避免产生API错误。
    """
    def __init__(
        self,
        session: Uninfo,
        original_interface: Interface,
        bot_user_ids: list[str],
    ):
        super().__init__(session, original_interface.fetcher)
        self.original = original_interface
        self.bot_user_ids = set(bot_user_ids)

    async def get_member(
        self, scene_type: SceneType, scene_id: str, user_id: str
    ) -> Member | None:
        if user_id in self.bot_user_ids:
            return None
        return await self.original.get_member(scene_type, scene_id, user_id)


async def apply_monkey_patch(game: Game, real_player_id: str):
    """
    替换 InputStore.fetch 方法，实现机器人输入的全方位模拟。
    """
    original_fetch = InputStore.fetch
    bot_user_ids = {p.user_id for p in game.players if p.user_id != real_player_id}
    bot_states = {} # 为每个机器人存储状态

    async def fake_fetch(user_id: str, group_id: str | None = None) -> UniMessage:
        if user_id not in bot_user_ids:
            return await original_fetch(user_id, group_id)

        await asyncio.sleep(random.uniform(1.0, 2.5))
        player_obj = game._player_map[user_id]
        state = bot_states.setdefault(user_id, {})
        action = config.get_stop_command()[0]

        # 每日重置状态
        if state.get("day") != game.context.day:
            state.clear()
            state["day"] = game.context.day

        # --- 核心AI逻辑 ---
        # 0. 如果机器人的回合已结束，则不自动响应，让其自然超时
        if state.get("turn_over"):
            return await original_fetch(user_id, group_id)

        # 1. 响应群聊输入 (自由讨论)
        if group_id is not None:
            if not game.behavior.speak_in_turn and game.context.state == GameContext.State.DAY:
                action = config.get_stop_command()[0]
                player_obj.log(f"<r>Auto Action (Group Stop)</r> | {action}")
                return UniMessage.text(action)
            return await original_fetch(user_id, group_id) # 其他情况机器人不响应群聊

        # 2. 响应私聊输入 (技能、投票等)
        # 狼人专属逻辑
        if player_obj.role == Role.WEREWOLF and game.context.state == GameContext.State.NIGHT:
            if state.get("werewolf_has_selected"):
                action = config.get_stop_command()[0] # 第二步，确认选择
                state["turn_over"] = True # 标记回合结束
            else:
                if random.random() < 0.9: # 90% 概率刀人
                    selectable = game.players.alive().exclude(player_obj).sorted
                    if selectable:
                        target = random.choice(selectable)
                        action = str(selectable.index(target) + 1)
                state["werewolf_has_selected"] = True # 第一步，标记已选择
        # 通用单步操作逻辑
        else:
            state["turn_over"] = True # 单步操作，直接标记回合结束
            if random.random() < 0.9: # 90% 概率执行有效操作
                selectable = game.players.alive().exclude(player_obj).sorted
                # 女巫救人特殊逻辑
                if player_obj.role == Role.WITCH and game.context.killed and player_obj.antidote:
                     action = "1" if random.random() < 0.7 else config.get_stop_command()[0]
                elif selectable:
                    target = random.choice(selectable)
                    action = str(selectable.index(target) + 1)

        player_obj.log(f"<r>Auto Action (Private)</r> | {action}")
        return UniMessage.text(action)

    InputStore.fetch = fake_fetch


test_harness = on_alconna(
    Alconna("werewolf_test", Args["player_count", int]),
    aliases={"狼人杀测试"},
    permission=SUPERUSER,
    use_cmd_start=config.use_cmd_start,
    priority=config.matcher_priority.start - 1,
)


@test_harness.handle()
async def handle_test_harness(
    target: MsgTarget,
    player_count: Match[int],
    session: Uninfo,
    interface: QryItrface,
):
    if target.private:
        await UniMessage("请在群组内开始测试").finish()

    if target in get_running_games():
        await UniMessage("⚠️当前群组内有正在进行的游戏，无法开始测试").finish()

    num_bots = player_count.result - 1
    if num_bots < 1:
        await UniMessage("⚠️测试玩家总数必须大于1").finish()

    await UniMessage(
        f"✅ 正在创建测试游戏，包含 1 位真实玩家和 {num_bots} 个机器人玩家..."
    ).send(target)

    players = {}
    admin_id = session.user.id
    admin_name = extract_session_member_nick(session) or admin_id
    players[admin_id] = admin_name

    bot_user_ids = []
    for i in range(num_bots):
        user_id = str(FAKE_USER_ID_START + i)
        bot_user_ids.append(user_id)
        players[user_id] = f"机器人玩家-{i+1}"

    mock_interface = MockInterface(
        session=session,
        original_interface=interface,
        bot_user_ids=bot_user_ids,
    )

    try:
        game = await Game.new(target, set(players), mock_interface)
        await apply_monkey_patch(game, admin_id)
        game.is_test = True
        game.start()
    except Exception as e:
        await UniMessage(f"❌ 游戏创建失败: {e}").send(target)
        return

    await UniMessage("✅ 机器人玩家已启动，它们将自动进行游戏。").send(target)
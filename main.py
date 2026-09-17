# -*- coding: utf-8 -*-
"""
AstrBot 插件：米游社每日签到

每天自动在米游社进行每日签到，支持：
1. 米游社社区签到（大别野等板块，可获得米游币/经验）
2. 游戏签到（原神、崩坏：星穹铁道、绝区零、崩坏3、未定事件簿、崩坏学园2）

聊天指令：
/mys签到 或 /米游社签到    手动执行每日签到
/mys测试 或 /米游社测试    测试 Cookie 是否有效
/mys绑定 或 /米游社绑定    绑定当前会话，用于接收自动签到结果推送
/mys帮助 或 /米游社帮助    查看帮助

说明：
- Cookie 在 AstrBot WebUI 的插件配置中填写（`cookie` 配置项）。
- 社区签到需要 Cookie 中包含 stuid/stoken；游戏签到使用完整浏览器 Cookie 即可。
- 触发验证码（retcode 1034）时本插件无法自动处理，可稍后在米游社手动签到。
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
import random
import re
import string
import time
import uuid
from datetime import datetime, timedelta
from typing import Any, Optional

import aiohttp

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import filter, AstrMessageEvent, MessageChain
from astrbot.api.star import Context, Star, register

# ---------------------------------------------------------------------------
# 米游社 API 常量
# ---------------------------------------------------------------------------

BBS_VERSION = "2.106.2"
# 各接口对应的 DS 签名 salt（与 BBS_VERSION 配套，来源：开源项目 MiyoQian）
BBS_SALT = "idMMaGYmVgPzh3wxmWudUXKUPGidO7GM"  # 米游社 App 通用 salt（DS v1）
BBS_WEB_SALT = "G1ktdwFL4IyGkHuuWSmz0wUe9Db9scyK"  # 网页端 / 游戏签到 salt（DS v1）
BBS_X6_SALT = "t0qEgfub6cvueAPgR5m9aQWWVciEer7v"  # 米游社 App x6 salt（DS v2，社区签到）
PASSPORT_APP_ID = "bll8iq97cem8"  # x-rpc-verify_key / x-rpc-app_id
PASSPORT_X4_SALT = "xV8v4Qu54lUKrEYFZkJhB8cuOh9Asafs"  # 通行证接口 salt（DS v2，扫码登录/换 token）

PASSPORT_API = "https://passport-api.mihoyo.com"
PASSPORT_APP_VERSION = "2.90.1"
PASSPORT_APP_UA = f"Mozilla/5.0 miHoYoBBS/{PASSPORT_APP_VERSION} Capture/2.2.0"
QRCODE_FETCH_URL = f"{PASSPORT_API}/account/ma-cn-passport/app/createQRLogin"
QRCODE_QUERY_URL = f"{PASSPORT_API}/account/ma-cn-passport/app/queryQRLoginStatus"
LTOKEN_BY_STOKEN_URL = f"{PASSPORT_API}/account/auth/api/getLTokenBySToken"
COOKIE_TOKEN_BY_STOKEN_URL = f"{PASSPORT_API}/account/auth/api/getCookieAccountInfoBySToken"

MOBILE_UA = (
    "Mozilla/5.0 (Linux; Android 12; Unspecified Device) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 "
    f"Chrome/103.0.5060.129 Mobile Safari/537.36 miHoYoBBS/{BBS_VERSION}"
)

TAKUMI_API = "https://api-takumi.mihoyo.com"
BBS_API = "https://bbs-api.miyoushe.com"
ZZZ_ACT_API = "https://act-nap-api.mihoyo.com"

ACCOUNT_ROLES_URL = f"{TAKUMI_API}/binding/api/getUserGameRolesByCookie"
GAME_HOME_URL = f"{TAKUMI_API}/event/luna/home?lang=zh-cn"
GAME_INFO_URL = f"{TAKUMI_API}/event/luna/info?lang=zh-cn"
GAME_SIGN_URL = f"{TAKUMI_API}/event/luna/sign"
ZZZ_HOME_URL = f"{ZZZ_ACT_API}/event/luna/zzz/home?lang=zh-cn"
ZZZ_INFO_URL = f"{ZZZ_ACT_API}/event/luna/zzz/info?lang=zh-cn"
ZZZ_SIGN_URL = f"{ZZZ_ACT_API}/event/luna/zzz/sign"
BBS_SIGN_URL = f"{BBS_API}/apihub/app/api/signIn"
BBS_TASKS_URL = f"{BBS_API}/apihub/wapi/getUserMissionsState"

GAMES = {
    "genshin": {
        "name": "原神", "game_biz": "hk4e_cn", "act_id": "e202311201442471",
        "home_url": GAME_HOME_URL, "info_url": GAME_INFO_URL, "sign_url": GAME_SIGN_URL,
        "extra": {"x-rpc-signgame": "hk4e"},
    },
    "starrail": {
        "name": "崩坏：星穹铁道", "game_biz": "hkrpg_cn", "act_id": "e202304121516551",
        "home_url": GAME_HOME_URL, "info_url": GAME_INFO_URL, "sign_url": GAME_SIGN_URL,
        "extra": {},
    },
    "zzz": {
        "name": "绝区零", "game_biz": "nap_cn", "act_id": "e202406242138391",
        "home_url": ZZZ_HOME_URL, "info_url": ZZZ_INFO_URL, "sign_url": ZZZ_SIGN_URL,
        "extra": {"x-rpc-signgame": "zzz"},
    },
    "honkai3rd": {
        "name": "崩坏3", "game_biz": "bh3_cn", "act_id": "e202306201626331",
        "home_url": GAME_HOME_URL, "info_url": GAME_INFO_URL, "sign_url": GAME_SIGN_URL,
        "extra": {},
    },
    "tears": {
        "name": "未定事件簿", "game_biz": "nxx_cn", "act_id": "e202202251749321",
        "home_url": GAME_HOME_URL, "info_url": GAME_INFO_URL, "sign_url": GAME_SIGN_URL,
        "extra": {},
    },
    "honkai2": {
        "name": "崩坏学园2", "game_biz": "bh2_cn", "act_id": "e202203291431091",
        "home_url": GAME_HOME_URL, "info_url": GAME_INFO_URL, "sign_url": GAME_SIGN_URL,
        "extra": {},
    },
}

FORUMS = {
    "1": "崩坏3",
    "2": "原神",
    "3": "崩坏学园2",
    "4": "未定事件簿",
    "5": "大别野",
    "6": "崩坏：星穹铁道",
    "8": "绝区零",
}

HELP_TEXT = (
    "【米游社每日签到】使用帮助\n"
    "━━━━━━━━━━━━━━\n"
    "📌 配置（AstrBot WebUI → 插件管理 → 米游社每日签到）：\n"
    "• cookie：米游社 Cookie（必填，含 stuid/stoken 可签到社区板块）\n"
    "• 社区签到：默认开启（大别野）\n"
    "• 游戏签到：默认关闭，可按需开启原神/星穹铁道/绝区零等\n"
    "• 自动签到时间：每天 HH:MM 自动执行\n\n"
    "📌 指令：\n"
    "• /mys签到 或 /米游社签到  手动执行每日签到\n"
    "• /mys测试 或 /米游社测试  测试 Cookie 是否有效\n"
    "• /mys绑定 或 /米游社绑定  绑定会话以接收自动签到结果\n"
    "• /mys帮助 或 /米游社帮助  查看本帮助\n\n"
    "📌 如何获取 Cookie：\n"
    "手机端：打开米游社 App → 我的 → 设置 → 复制 Cookie（含 stuid/stoken，社区签到必需）\n"
    "电脑端：登录 mihoyo 相关网页后按 F12 → Network → 复制任意请求的 Cookie（可签到游戏）\n\n"
    "⚠️ 注意：触发验证码（1034）时需稍后手动签到；Cookie 过期后需在 WebUI 重新填写。"
)

# ---------------------------------------------------------------------------
# 签名工具
# ---------------------------------------------------------------------------


def _md5(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()


def _random_text(n: int) -> str:
    return "".join(random.choice(string.ascii_lowercase + string.digits) for _ in range(n))


def ds_v1(salt: str) -> str:
    """DS v1：t,r,md5('salt=...&t=...&r=...')"""
    t = str(int(time.time()))
    r = _random_text(6)
    sign = _md5(f"salt={salt}&t={t}&r={r}")
    return f"{t},{r},{sign}"


def ds_v2(salt: str, query: str = "", body: str = "") -> str:
    """DS v2：t,r,md5('salt=...&t=...&r=...&b=...&q=...')"""
    t = str(int(time.time()))
    r = str(random.randint(100001, 200000))
    sign = _md5(f"salt={salt}&t={t}&r={r}&b={body}&q={query}")
    return f"{t},{r},{sign}"


def ds_x4(query: str = "", body: str = "") -> str:
    """通行证（passport）接口使用的 DS v2 签名（x4 salt）。"""
    t = str(int(time.time()))
    r = str(random.randint(100000, 200000))
    sign = _md5(f"salt={PASSPORT_X4_SALT}&t={t}&r={r}&b={body}&q={query}")
    return f"{t},{r},{sign}"


# ---------------------------------------------------------------------------
# Cookie 工具
# ---------------------------------------------------------------------------


def _cookie_value(cookie: str, *names: str) -> str:
    if not cookie:
        return ""
    for name in names:
        m = re.search(rf"(?:^|;\s*){re.escape(name)}=([^;]+)", cookie)
        if m:
            return m.group(1).strip()
    return ""


def clean_cookie(cookie: str) -> str:
    """清理 Cookie：去除首尾引号、所有空白字符（空格/换行/制表符），规范化分隔符。

    用户从 App/网页复制 Cookie 时常会带入换行、引号或多余空格，
    若不清理会导致字段解析失败（表现为 -100 登录失效）。
    """
    if not cookie:
        return ""
    cookie = cookie.strip().strip('"').strip("'")
    cookie = re.sub(r"\s+", "", cookie)  # 移除所有空白
    cookie = re.sub(r";+", ";", cookie)  # 多个分号合并为一个
    return cookie.strip(";")


def build_stoken_cookie(cookie: str) -> str:
    """从完整 Cookie 中提取 stuid/stoken/mid 组成 App 社区签到使用的 Cookie。"""
    cookie = clean_cookie(cookie)
    if not cookie:
        return ""
    uid = _cookie_value(
        cookie,
        "stuid", "stuid_v2", "account_id", "account_id_v2", "ltuid", "ltuid_v2", "login_uid",
    )
    stoken = _cookie_value(cookie, "stoken", "stoken_v2")
    mid = _cookie_value(cookie, "mid", "mid_v2", "account_mid_v2", "ltmid_v2")
    if not uid or not stoken:
        return ""
    items = [f"stuid={uid}", f"stoken={stoken}"]
    if mid:
        items.append(f"mid={mid}")
    return ";".join(items)


def describe_award(awards: list[dict[str, Any]], index: int) -> str:
    if not awards:
        return "未知"
    index = max(0, min(int(index), len(awards) - 1))
    award = awards[index]
    return f"「{award.get('name', '未知')}」x{award.get('cnt', '?')}"


def make_qr_png(text: str) -> bytes:
    """将文本渲染为二维码 PNG 图片（字节）。"""
    import qrcode
    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=8,
        border=2,
    )
    qr.add_data(text)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


@register("astrbot_plugin_miyoushe_sign", "AstrBot Developer", "每天自动在米游社进行每日签到", "v1.1.0")
class MiyousheSignPlugin(Star):
    """米游社每日签到插件。"""

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self._http: Optional[aiohttp.ClientSession] = None
        self._auto_task: Optional[asyncio.Task] = None

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    async def initialize(self):
        """插件加载后自动调用：初始化设备信息并启动每日定时任务。"""
        try:
            self._ensure_device()
        except Exception as e:
            logger.error(f"【米游社签到】初始化设备信息失败: {e}")
        if self.config.get("auto_sign_enable", True):
            self._auto_task = asyncio.create_task(self._auto_sign_loop())
            logger.info("【米游社签到】每日自动签到任务已启动")
        logger.info("【米游社签到】插件初始化完成")

    async def terminate(self):
        """插件卸载/停用时调用。"""
        if self._auto_task:
            self._auto_task.cancel()
        if self._http and not self._http.closed:
            await self._http.close()

    # ------------------------------------------------------------------
    # 指令处理
    # ------------------------------------------------------------------

    @filter.command("米游社签到", alias={"mys签到", "mys_sign", "myssign"})
    async def manual_sign(self, event: AstrMessageEvent):
        """手动执行米游社每日签到"""
        self._save_bind(event.unified_msg_origin)
        yield event.plain_result("正在执行米游社每日签到，请稍候…")
        lines = await self.do_sign_all()
        yield event.plain_result("\n".join(lines))

    @filter.command("米游社测试", alias={"mys测试", "mys_test", "mystest"})
    async def test_cookie(self, event: AstrMessageEvent):
        """测试米游社 Cookie 是否有效"""
        self._save_bind(event.unified_msg_origin)
        result = await self._check_cookie()
        yield event.plain_result(result)

    @filter.command("米游社绑定", alias={"mys绑定", "mys_bind", "mysbind"})
    async def bind_session(self, event: AstrMessageEvent):
        """绑定当前会话，自动签到完成后将推送结果"""
        try:
            self.config["bind_umo"] = event.unified_msg_origin
            self.config.save_config()
            yield event.plain_result("✅ 已绑定当前会话，自动签到完成后将推送结果。")
        except Exception as e:
            logger.error(f"【米游社签到】绑定会话失败: {e}")
            yield event.plain_result(f"❌ 绑定失败：{e}")

    @filter.command("米游社帮助", alias={"mys帮助", "mys_help", "myshelp"})
    async def help_cmd(self, event: AstrMessageEvent):
        """查看米游社签到插件帮助"""
        yield event.plain_result(HELP_TEXT)

    @filter.command("米游社扫码登录", alias={"mys扫码", "mys_qr", "mysqr"})
    async def qr_login(self, event: AstrMessageEvent):
        """用米游社 App 扫码登录，自动获取并保存完整 Cookie"""
        yield event.plain_result("正在生成登录二维码…")
        try:
            url, ticket = await self._qr_fetch()
        except Exception as e:
            logger.error(f"【米游社签到】生成二维码失败: {e}")
            yield event.plain_result(f"❌ 生成二维码失败：{e}")
            return
        # 发送二维码图片（失败则回退为发送链接）
        try:
            png = make_qr_png(url)
            path = self._save_qr_image(png)
            yield event.image_result(path)
        except Exception as e:
            logger.warning(f"【米游社签到】生成二维码图片失败，改用链接: {e}")
            yield event.image_result(url)
        yield event.plain_result(
            "请用米游社 App 扫描上方二维码，并在手机上点击「确认登录」。\n"
            "正在等待扫码结果…（120 秒内有效，也可回复其他消息稍候）"
        )
        # 轮询等待用户扫码确认
        try:
            cred = await self._qr_wait(ticket)
        except asyncio.TimeoutError:
            yield event.plain_result("❌ 扫码超时，请重新发送 /mys扫码 再试。")
            return
        except Exception as e:
            yield event.plain_result(f"❌ 扫码失败：{e}")
            return
        # 用 stoken 换取 ltoken / cookie_token
        ltoken, cookie_token = "", ""
        try:
            ltoken = await self._get_ltoken(cred["stoken"], cred["mid"])
            cookie_token = await self._get_cookie_token(cred["stoken"], cred["mid"])
        except Exception as e:
            logger.warning(f"【米游社签到】换取 ltoken/cookie_token 失败: {e}")
        # 构建完整 Cookie 并保存
        full_cookie = self._build_full_cookie(
            cred["stuid"], cred["mid"], cred["stoken"], ltoken, cookie_token
        )
        self.config["cookie"] = full_cookie
        self.config.save_config()
        self._save_bind(event.unified_msg_origin)
        yield event.plain_result(
            "✅ 扫码登录成功！Cookie 已自动保存到插件配置。\n"
            "发送 /mys测试 验证，/mys签到 立即签到。"
        )

    # ------------------------------------------------------------------
    # 签到主流程
    # ------------------------------------------------------------------

    async def do_sign_all(self) -> list[str]:
        """执行全部签到（社区签到 + 游戏签到），返回结果文本行列表。"""
        lines = ["【米游社每日签到】"]
        cookie = clean_cookie(str(self.config.get("cookie", "") or ""))
        if not cookie:
            lines.append("❌ 未配置米游社 Cookie，请在 AstrBot WebUI 的插件配置中填写。")
            lines.append("💡 发送 /米游社帮助 查看如何获取 Cookie。")
            return lines
        if self.config.get("enable_community_sign", True):
            lines.append("── 社区签到 ──")
            lines.extend(await self._community_sign())
        if self.config.get("enable_game_sign", False):
            lines.append("── 游戏签到 ──")
            for game_key in self.config.get("games", ["genshin", "starrail", "zzz"]):
                if game_key in GAMES:
                    lines.extend(await self._game_sign(str(game_key)))
        return lines

    # ------------------------------------------------------------------
    # 社区签到
    # ------------------------------------------------------------------

    async def _community_sign(self) -> list[str]:
        lines: list[str] = []
        cookie = clean_cookie(str(self.config.get("cookie", "") or ""))
        stoken_cookie = build_stoken_cookie(cookie)
        if not stoken_cookie:
            lines.append(
                "⚠️ 社区签到需要 Cookie 包含 stoken（stuid/stoken），"
                "请从米游社 App 中复制完整 Cookie 后重新填写。"
            )
            return lines
        headers = self._bbs_headers(stoken_cookie)
        forums = self.config.get("community_forums", ["5"])
        for gid in forums:
            gid = str(gid).strip()
            name = FORUMS.get(gid, f"板块{gid}")
            body = json.dumps({"gids": gid}, separators=(",", ":"))
            headers["DS"] = ds_v2(BBS_X6_SALT, body=body)
            try:
                data = await self._post_json(BBS_SIGN_URL, headers=headers, data=body)
            except Exception as e:
                logger.error(f"【米游社签到】社区签到请求失败: {e}")
                lines.append(f"❌ {name} 签到失败（网络或接口异常）")
                await asyncio.sleep(1)
                continue
            code = data.get("retcode")
            msg = str(data.get("message", ""))
            if code == 0:
                lines.append(f"✅ {name} 社区签到成功")
            elif code == -5003:
                lines.append(f"⏭️ {name} 今日已签到")
            elif code == 1034:
                lines.append(f"⚠️ {name} 触发验证码，本次跳过（可稍后在米游社手动签到）")
            elif code in (-100, 1008, 10103, 10104):
                lines.append(
                    f"❌ {name} 登录失效（{msg}）：请重新登录米游社获取新 Cookie 后，"
                    "在插件配置中更新 `cookie` 并保存。"
                )
            else:
                lines.append(f"❌ {name} 签到失败：{msg}({code})")
            await asyncio.sleep(random.uniform(1, 2))
        return lines

    # ------------------------------------------------------------------
    # 游戏签到
    # ------------------------------------------------------------------

    async def _game_sign(self, game_key: str) -> list[str]:
        game = GAMES[game_key]
        name = game["name"]
        lines: list[str] = []
        cookie = clean_cookie(str(self.config.get("cookie", "") or ""))
        if not cookie:
            lines.append(f"❌ {name} 签到失败：未配置 Cookie")
            return lines
        headers = self._game_headers(game)
        # 1. 获取绑定角色
        try:
            roles_data = await self._get_json(
                ACCOUNT_ROLES_URL, headers=headers, params={"game_biz": game["game_biz"]}
            )
        except Exception as e:
            logger.error(f"【米游社签到】获取{name}绑定角色失败: {e}")
            return [f"❌ {name} 获取绑定角色失败（网络或接口异常）"]
        retcode = roles_data.get("retcode")
        if retcode != 0:
            msg = str(roles_data.get("message", ""))
            if retcode == -100:
                return [
                    f"❌ {name} 登录失效（{msg}）：请重新登录米游社获取新 Cookie 后，"
                    "在插件配置中更新 `cookie` 并保存。"
                ]
            return [f"❌ {name} 获取绑定角色失败：{msg}({retcode})"]
        roles = (roles_data.get("data") or {}).get("list") or []
        if not roles:
            return [f"⏭️ {name} 未找到绑定角色"]
        # 2. 获取签到奖励列表（用于展示奖励）
        awards = await self._get_awards(game, headers)
        # 3. 逐角色签到
        for role in roles:
            uid = str(role.get("game_uid") or "")
            region = str(role.get("region") or "")
            nickname = str(role.get("nickname") or uid)
            label = f"{name} {nickname}({uid})"
            info = await self._get_sign_info(game, headers, region, uid)
            if not info:
                lines.append(f"❌ {label} 查询签到状态失败")
                await asyncio.sleep(1)
                continue
            if info.get("first_bind"):
                lines.append(f"⚠️ {label} 首次绑定，请先在米游社手动签到一次")
                continue
            day_index = max(int(info.get("total_sign_day") or 1) - 1, 0)
            if info.get("is_sign"):
                lines.append(
                    f"⏭️ {label} 今日已签到，奖励 {describe_award(awards, day_index)}"
                )
                continue
            sign_data = await self._sign_game(game, headers, region, uid)
            code = sign_data.get("retcode")
            msg = str(sign_data.get("message", ""))
            if code == 0:
                lines.append(
                    f"✅ {label} 签到成功，奖励 {describe_award(awards, day_index + 1)}"
                )
            elif code == -5003:
                lines.append(
                    f"⏭️ {label} 今日已签到，奖励 {describe_award(awards, day_index)}"
                )
            elif code == 1034:
                lines.append(f"⚠️ {label} 触发验证码，本次跳过（可稍后在米游社手动签到）")
            else:
                lines.append(f"❌ {label} 签到失败：{msg}({code})")
            await asyncio.sleep(random.uniform(1, 2))
        return lines



    async def _get_awards(self, game: dict, headers: dict) -> list[dict]:
        try:
            data = await self._get_json(
                game["home_url"], headers=headers, params={"act_id": game["act_id"]}
            )
            if data.get("retcode") == 0:
                awards = (data.get("data") or {}).get("awards") or []
                return awards if isinstance(awards, list) else []
        except Exception as e:
            logger.error(f"【米游社签到】获取{game['name']}签到奖励失败: {e}")
        return []

    async def _get_sign_info(self, game: dict, headers: dict, region: str, uid: str) -> dict:
        try:
            data = await self._get_json(
                game["info_url"],
                headers=headers,
                params={"act_id": game["act_id"], "region": region, "uid": uid},
            )
            if data.get("retcode") == 0:
                return data.get("data") or {}
        except Exception as e:
            logger.error(f"【米游社签到】查询{game['name']}签到状态失败: {e}")
        return {}

    async def _sign_game(self, game: dict, headers: dict, region: str, uid: str) -> dict:
        try:
            return await self._post_json(
                game["sign_url"],
                headers=headers,
                json_data={"act_id": game["act_id"], "region": region, "uid": uid},
            )
        except Exception as e:
            logger.error(f"【米游社签到】{game['name']}签到请求失败: {e}")
            return {"retcode": -1, "message": str(e)}

    # ------------------------------------------------------------------
    # Cookie 测试
    # ------------------------------------------------------------------

    async def _check_cookie(self) -> str:
        cookie = clean_cookie(str(self.config.get("cookie", "") or ""))
        if not cookie:
            return "❌ 未配置 Cookie，请在插件配置中填写。"
        # 检测关键字段是否齐全，帮助定位问题
        has_uid = bool(
            _cookie_value(
                cookie,
                "stuid", "stuid_v2", "account_id", "account_id_v2",
                "ltuid", "ltuid_v2", "login_uid",
            )
        )
        has_stoken = bool(_cookie_value(cookie, "stoken", "stoken_v2"))
        has_mid = bool(_cookie_value(cookie, "mid", "mid_v2", "account_mid_v2", "ltmid_v2"))
        has_ltoken = bool(_cookie_value(cookie, "ltoken", "ltoken_v2"))
        field_info = (
            "字段检测："
            f"stuid={'✅' if has_uid else '❌'} | "
            f"stoken={'✅' if has_stoken else '❌'} | "
            f"mid={'✅' if has_mid else '❌'} | "
            f"ltoken={'✅' if has_ltoken else '❌'}"
        )
        headers = {
            "Accept": "application/json, text/plain, */*",
            "Origin": "https://webstatic.mihoyo.com",
            "User-Agent": MOBILE_UA,
            "Referer": "https://webstatic.mihoyo.com",
            "Accept-Language": "zh-CN,en-US;q=0.8",
            "X-Requested-With": "com.mihoyo.hyperion",
            "Cookie": cookie,
        }
        try:
            data = await self._get_json(
                BBS_TASKS_URL, headers=headers, params={"point_sn": "myb"}
            )
        except Exception as e:
            return f"❌ 请求失败：{e}\n{field_info}"
        code = data.get("retcode")
        if code == 0:
            d = data.get("data") or {}
            received = d.get("already_received_points", "?")
            total = d.get("total_points", "?")
            return (
                f"✅ Cookie 有效。米游币：今日已得 {received}，总计 {total}。\n{field_info}"
            )
        if code == -100:
            return (
                f"❌ Cookie 登录状态失效（-100）：请重新登录米游社获取新 Cookie。\n"
                f"{field_info}\n"
                f"💡 社区签到需要 App 版 Cookie（含 stuid/stoken）；"
                "游戏签到用网页 Cookie 即可。"
            )
        return f"❌ Cookie 无效或过期：{data.get('message', '')}({code})\n{field_info}"

    # ------------------------------------------------------------------
    # 每日自动签到
    # ------------------------------------------------------------------

    async def _auto_sign_loop(self):
        while True:
            try:
                cfg_time = str(self.config.get("auto_sign_time", "09:00")).strip() or "09:00"
                try:
                    hour, minute = int(cfg_time.split(":")[0]), int(cfg_time.split(":")[1])
                    hour = max(0, min(hour, 23))
                    minute = max(0, min(minute, 59))
                except Exception:
                    hour, minute = 9, 0
                now = datetime.now()
                target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
                if target <= now:
                    target += timedelta(days=1)
                logger.info(
                    f"【米游社签到】下次自动签到时间：{target.strftime('%Y-%m-%d %H:%M:%S')}"
                )
                await asyncio.sleep((target - now).total_seconds())
                await self._run_auto_sign()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"【米游社签到】自动签到循环异常：{e}")
                await asyncio.sleep(60)

    async def _run_auto_sign(self):
        try:
            lines = await self.do_sign_all()
            logger.info("【米游社签到】自动签到完成：\n" + "\n".join(lines))
            if not self.config.get("push_result", False):
                return
            umo = self.config.get("bind_umo", "")
            if not umo:
                logger.warning("【米游社签到】未绑定会话，跳过推送。发送 /米游社绑定 进行绑定。")
                return
            try:
                chain = MessageChain().message("\n".join(lines))
                await self.context.send_message(umo, chain)
            except Exception as e:
                logger.error(f"【米游社签到】推送签到结果失败：{e}")
        except Exception as e:
            logger.error(f"【米游社签到】自动签到执行异常：{e}")

    # ------------------------------------------------------------------
    # 扫码登录
    # ------------------------------------------------------------------

    def _passport_headers(self, body: str) -> dict:
        """passport 接口（生成/查询二维码、换取 token）使用的请求头。"""
        return {
            "User-Agent": PASSPORT_APP_UA,
            "Accept": "*/*",
            "Accept-Language": "zh-cn",
            "x-rpc-client_type": "3",
            "x-rpc-app_version": PASSPORT_APP_VERSION,
            "x-rpc-device_id": str(self.config.get("device_id", "")),
            "x-rpc-device_fp": str(self.config.get("device_fp", "")),
            "x-rpc-game_biz": "bbs_cn",
            "x-rpc-app_id": PASSPORT_APP_ID,
            "x-rpc-sdk_version": PASSPORT_APP_VERSION,
            "x-rpc-device_model": "Mi 14",
            "x-rpc-device_name": "Mihoyo Capture",
            "x-rpc-account_version": PASSPORT_APP_VERSION,
            "DS": ds_x4(body=body),
            "Content-Type": "application/json; charset=UTF-8",
        }

    async def _qr_fetch(self) -> tuple[str, str]:
        """创建登录二维码，返回 (二维码内容 url, ticket)。"""
        data = await self._post_json(
            QRCODE_FETCH_URL, headers=self._passport_headers("{}"), json_data={}
        )
        if data.get("retcode") != 0:
            raise RuntimeError(f"{data.get('message')}({data.get('retcode')})")
        d = data.get("data") or {}
        url = str(d.get("url") or "")
        ticket = str(d.get("ticket") or "")
        if not url or not ticket:
            raise RuntimeError("二维码接口未返回 url/ticket")
        return url, ticket

    async def _qr_wait(self, ticket: str, timeout: int = 120) -> dict:
        """轮询扫码状态，成功后返回 {stoken, mid, stuid}。"""
        started = time.time()
        while time.time() - started < timeout:
            body = json.dumps({"ticket": ticket}, separators=(",", ":"))
            data = await self._post_json(
                QRCODE_QUERY_URL, headers=self._passport_headers(body), json_data={"ticket": ticket}
            )
            if data.get("retcode") != 0:
                raise RuntimeError(
                    f"查询二维码状态失败：{data.get('message')}({data.get('retcode')})"
                )
            sd = data.get("data") or {}
            status = str(sd.get("status") or "")
            if status == "Confirmed":
                ui = sd.get("user_info") or {}
                mid = str(ui.get("mid") or "")
                stuid = str(ui.get("aid") or "")
                tokens = sd.get("tokens") or []
                stoken = str(tokens[0].get("token") or "") if tokens else ""
                if not stoken or not mid or not stuid:
                    raise RuntimeError("扫码结果缺少 stoken/mid/stuid")
                return {"stoken": stoken, "mid": mid, "stuid": stuid}
            await asyncio.sleep(2)
        raise asyncio.TimeoutError("扫码登录超时")

    async def _get_ltoken(self, stoken: str, mid: str) -> str:
        """用 stoken + mid 换取 ltoken（网页端登录凭证）。"""
        headers = {
            "User-Agent": f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) miHoYoBBS/{BBS_VERSION}",
            "x-rpc-app_version": BBS_VERSION,
            "x-rpc-client_type": "5",
            "X-Requested-With": "com.mihoyo.hyperion",
            "Referer": "https://webstatic.mihoyo.com",
            "x-rpc-device_id": str(self.config.get("device_id", "")),
            "x-rpc-device_fp": str(self.config.get("device_fp", "")),
            "Cookie": f"mid={mid};stoken={stoken}",
            "DS": ds_x4(query=f"stoken={stoken}"),
        }
        data = await self._get_json(
            LTOKEN_BY_STOKEN_URL, headers=headers, params={"stoken": stoken}
        )
        if data.get("retcode") != 0:
            raise RuntimeError(
                f"stoken 换 ltoken 失败：{data.get('message')}({data.get('retcode')})"
            )
        return str((data.get("data") or {}).get("ltoken") or "")

    async def _get_cookie_token(self, stoken: str, mid: str) -> str:
        """用 stoken + mid 换取 cookie_token（网页端登录凭证）。"""
        headers = {
            "User-Agent": f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) miHoYoBBS/{BBS_VERSION}",
            "x-rpc-app_version": BBS_VERSION,
            "x-rpc-client_type": "2",
            "X-Requested-With": "com.mihoyo.hyperion",
            "Referer": "https://webstatic.mihoyo.com",
            "x-rpc-device_id": str(self.config.get("device_id", "")),
            "x-rpc-device_fp": str(self.config.get("device_fp", "")),
            "x-rpc-aigis": "",
            "Cookie": f"mid={mid};stoken={stoken}",
            "DS": ds_x4(query=f"stoken={stoken}"),
        }
        data = await self._get_json(
            COOKIE_TOKEN_BY_STOKEN_URL, headers=headers, params={"stoken": stoken}
        )
        if data.get("retcode") != 0:
            raise RuntimeError(
                f"stoken 换 cookie_token 失败：{data.get('message')}({data.get('retcode')})"
            )
        return str((data.get("data") or {}).get("cookie_token") or "")

    def _build_full_cookie(self, uid: str, mid: str, stoken: str, ltoken: str, cookie_token: str) -> str:
        """构建同时兼容社区签到（stuid/stoken）和游戏签到（网页字段）的完整 Cookie。"""
        parts: list[str] = []
        if ltoken:
            parts.append(f"ltoken={ltoken}")
        if cookie_token:
            parts.append(f"cookie_token={cookie_token}")
        parts.extend(
            [
                f"account_id={uid}",
                f"account_id_v2={uid}",
                f"account_mid_v2={mid}",
                f"ltmid_v2={mid}",
                f"ltuid={uid}",
                f"ltuid_v2={uid}",
                f"login_uid={uid}",
                f"stuid={uid}",
                f"stoken={stoken}",
                f"mid={mid}",
            ]
        )
        return "; ".join(parts)

    def _save_qr_image(self, png: bytes) -> str:
        """把二维码 PNG 保存到 AstrBot data/temp 目录，返回本地路径。"""
        try:
            data_dir = str(self.context.get_config().get("data_dir", "data") or "data")
        except Exception:
            data_dir = "data"
        temp_dir = os.path.join(data_dir, "temp")
        os.makedirs(temp_dir, exist_ok=True)
        path = os.path.join(temp_dir, f"mys_qr_{uuid.uuid4().hex}.png")
        with open(path, "wb") as f:
            f.write(png)
        return path

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------

    def _save_bind(self, umo: str):
        """若尚未绑定会话，则将当前会话保存为推送目标。"""
        if umo and not self.config.get("bind_umo"):
            try:
                self.config["bind_umo"] = umo
                self.config.save_config()
            except Exception as e:
                logger.error(f"【米游社签到】保存会话绑定失败：{e}")

    def _ensure_device(self):
        """生成并保存设备标识（社区签到需要）。"""
        changed = False
        if not self.config.get("device_id"):
            self.config["device_id"] = str(uuid.uuid4()).upper()
            changed = True
        if not self.config.get("device_fp"):
            self.config["device_fp"] = "".join(
                random.choice("0123456789abcdef") for _ in range(13)
            )
            changed = True
        if changed:
            try:
                self.config.save_config()
            except Exception as e:
                logger.error(f"【米游社签到】保存设备信息失败：{e}")

    async def _session(self) -> aiohttp.ClientSession:
        if self._http is None or self._http.closed:
            self._http = aiohttp.ClientSession()
        return self._http

    async def _get_json(self, url: str, headers: dict, params: dict) -> dict:
        session = await self._session()
        async with session.get(
            url, headers=headers, params=params, timeout=aiohttp.ClientTimeout(total=30)
        ) as resp:
            return await resp.json(content_type=None)

    async def _post_json(self, url: str, headers: dict, json_data: Any = None, data: str = None) -> dict:
        session = await self._session()
        async with session.post(
            url,
            headers=headers,
            json=json_data,
            data=data,
            timeout=aiohttp.ClientTimeout(total=30),
        ) as resp:
            return await resp.json(content_type=None)

    # ------------------------------------------------------------------
    # 请求头构建
    # ------------------------------------------------------------------

    def _bbs_headers(self, stoken_cookie: str) -> dict:
        """米游社 App 社区签到使用的请求头。"""
        headers = {
            "cookie": stoken_cookie,
            "x-rpc-client_type": "2",
            "x-rpc-app_version": BBS_VERSION,
            "x-rpc-sys_version": "12",
            "x-rpc-channel": "miyousheluodi",
            "x-rpc-device_id": str(self.config.get("device_id", "")),
            "x-rpc-device_name": "Xiaomi MI 6",
            "x-rpc-device_model": "Mi 6",
            "x-rpc-h265_supported": "1",
            "Referer": "https://app.mihoyo.com",
            "Content-Type": "application/json; charset=UTF-8",
            "Host": "bbs-api.miyoushe.com",
            "x-rpc-verify_key": PASSPORT_APP_ID,
            "x-rpc-csm_source": "home",
            "User-Agent": "okhttp/4.9.3",
        }
        device_fp = str(self.config.get("device_fp", "") or "")
        if device_fp:
            headers["x-rpc-device_fp"] = device_fp
        return headers

    def _game_headers(self, game: dict) -> dict:
        """游戏签到使用的请求头。"""
        headers = {
            "Accept": "application/json, text/plain, */*",
            "DS": ds_v1(BBS_WEB_SALT),
            "x-rpc-channel": "miyousheluodi",
            "Origin": "https://act.mihoyo.com",
            "x-rpc-app_version": BBS_VERSION,
            "User-Agent": MOBILE_UA,
            "x-rpc-client_type": "5",
            "Referer": "https://act.mihoyo.com/",
            "Accept-Language": "zh-CN,en-US;q=0.8",
            "X-Requested-With": "com.mihoyo.hyperion",
            "Cookie": str(self.config.get("cookie", "") or ""),
            "x-rpc-device_id": str(self.config.get("device_id", "")),
        }
        headers.update(game.get("extra") or {})
        return headers


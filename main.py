# -*- coding: utf-8 -*-
"""AstrBot 米游社多用户游戏签到插件。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import random
import re
import string
import time
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import aiohttp

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star, StarTools, register


TAKUMI_API = "https://api-takumi.mihoyo.com"
ZZZ_API = "https://act-nap-api.mihoyo.com"
ROLES_URL = f"{TAKUMI_API}/binding/api/getUserGameRolesByCookie"
USER_AGENT = (
    "Mozilla/5.0 (Linux; Android 12; Unspecified Device) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 "
    "Chrome/103.0.5060.129 Mobile Safari/537.36 miHoYoBBS/{version}"
)

GAMES: dict[str, dict[str, str]] = {
    "hk4e_cn": {"name": "原神", "act_id": "e202311201442471", "api": TAKUMI_API, "path": "luna", "signgame": "hk4e"},
    "hkrpg_cn": {"name": "崩坏：星穹铁道", "act_id": "e202304121516551", "api": TAKUMI_API, "path": "luna", "signgame": ""},
    "nap_cn": {"name": "绝区零", "act_id": "e202406242138391", "api": ZZZ_API, "path": "luna/zzz", "signgame": "zzz"},
    "bh3_cn": {"name": "崩坏3", "act_id": "e202306201626331", "api": TAKUMI_API, "path": "luna", "signgame": ""},
    "nxx_cn": {"name": "未定事件簿", "act_id": "e202202251749321", "api": TAKUMI_API, "path": "luna", "signgame": ""},
    "bh2_cn": {"name": "崩坏学园2", "act_id": "e202203291431091", "api": TAKUMI_API, "path": "luna", "signgame": ""},
}
ALIASES = {
    "原神": "hk4e_cn", "genshin": "hk4e_cn", "ys": "hk4e_cn",
    "崩铁": "hkrpg_cn", "星穹铁道": "hkrpg_cn", "崩坏：星穹铁道": "hkrpg_cn", "starrail": "hkrpg_cn",
    "绝区零": "nap_cn", "zzz": "nap_cn",
    "崩坏3": "bh3_cn", "崩坏三": "bh3_cn", "bh3": "bh3_cn",
    "未定": "nxx_cn", "未定事件簿": "nxx_cn", "themis": "nxx_cn",
    "崩坏2": "bh2_cn", "崩坏学园2": "bh2_cn", "bh2": "bh2_cn",
}
BEIJING = ZoneInfo("Asia/Shanghai")


def clean_cookie(value: str) -> str:
    """Normalise a copied Cookie without changing its individual values."""
    value = (value or "").strip().strip("\"").strip("'")
    value = re.sub(r"[\r\n\t]", "", value)
    return re.sub(r";\s*", "; ", value).strip("; ")


def cookie_value(cookie: str, *keys: str) -> str:
    pairs: dict[str, str] = {}
    for item in cookie.split(";"):
        key, sep, value = item.strip().partition("=")
        if sep:
            pairs[key] = value
    return next((pairs[key] for key in keys if pairs.get(key)), "")


def ds_v1(salt: str) -> str:
    timestamp = str(int(time.time()))
    nonce = "".join(random.choices(string.ascii_lowercase + string.digits, k=6))
    digest = hashlib.md5(f"salt={salt}&t={timestamp}&r={nonce}".encode("utf-8")).hexdigest()
    return f"{timestamp},{nonce},{digest}"


class UserStore:
    """Atomic JSON persistence for per-chat-user account data."""

    def __init__(self, path: str):
        self.path = path
        self.data: dict[str, dict[str, Any]] = {}
        self._lock = asyncio.Lock()
        try:
            with open(path, "r", encoding="utf-8") as file:
                loaded = json.load(file)
            if isinstance(loaded, dict):
                self.data = loaded
        except FileNotFoundError:
            pass
        except Exception as exc:
            logger.error(f"米游社签到：读取用户数据失败：{exc}")

    async def save(self) -> None:
        async with self._lock:
            temporary = f"{self.path}.tmp"
            try:
                with open(temporary, "w", encoding="utf-8") as file:
                    json.dump(self.data, file, ensure_ascii=False, indent=2)
                os.replace(temporary, self.path)
            except Exception as exc:
                logger.error(f"米游社签到：保存用户数据失败：{exc}")


@register("astrbot_plugin_mihoyo_multi_sign", "Local", "米游社多用户游戏每日签到", "v1.0.0")
class MiyousheMultiSignPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.context = context
        self.config = config
        try:
            root = str(StarTools.get_data_dir())
        except Exception:
            root = os.path.dirname(os.path.abspath(__file__))
        self.data_dir = os.path.join(root, "miyoushe_multi_sign")
        os.makedirs(self.data_dir, exist_ok=True)
        self.store = UserStore(os.path.join(self.data_dir, "users.json"))
        self.session: aiohttp.ClientSession | None = None
        self.schedule_task: asyncio.Task | None = None
        self.sign_lock = asyncio.Lock()

    @filter.on_astrbot_loaded()
    async def on_loaded(self):
        if not self.schedule_task or self.schedule_task.done():
            self.schedule_task = asyncio.create_task(self._scheduler())
        logger.info("米游社多用户签到插件已加载")

    async def terminate(self):
        if self.schedule_task:
            self.schedule_task.cancel()
        if self.session and not self.session.closed:
            await self.session.close()

    def _sender(self, event: AstrMessageEvent) -> str:
        return str(event.get_sender_id())

    @staticmethod
    def _argument(event: AstrMessageEvent, command: str) -> str:
        message = (getattr(event, "message_str", "") or "").strip()
        index = message.find(command)
        return message[index + len(command):].strip() if index >= 0 else ""

    def _user(self, sender: str) -> dict[str, Any]:
        return self.store.data.setdefault(sender, {"accounts": [], "active_index": 0, "umo": ""})

    def _active_account(self, sender: str) -> dict[str, Any] | None:
        user = self.store.data.get(sender) or {}
        accounts = user.get("accounts") or []
        if not accounts:
            return None
        index = user.get("active_index", 0)
        if not isinstance(index, int) or not 0 <= index < len(accounts):
            user["active_index"] = 0
            index = 0
        return accounts[index]

    def _enabled_games(self, requested: str = "") -> list[str]:
        if requested:
            game = ALIASES.get(requested.lower())
            return [game] if game else []
        settings = self.config.get("games") or {}
        if not isinstance(settings, dict):
            return list(GAMES)
        return [biz for biz in GAMES if settings.get(biz, True)]

    async def _http(self) -> aiohttp.ClientSession:
        if self.session is None or self.session.closed:
            timeout = aiohttp.ClientTimeout(total=25)
            self.session = aiohttp.ClientSession(timeout=timeout)
        return self.session

    def _headers(self, cookie: str, game: dict[str, str] | None = None) -> dict[str, str]:
        version = str(self.config.get("app_version", "2.106.2"))
        headers = {
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json;charset=UTF-8",
            "Cookie": cookie,
            "DS": ds_v1(str(self.config.get("web_ds_salt", "G1ktdwFL4IyGkHuuWSmz0wUe9Db9scyK"))),
            "Origin": "https://act.mihoyo.com",
            "Referer": "https://act.mihoyo.com/",
            "User-Agent": USER_AGENT.format(version=version),
            "X-Requested-With": "com.mihoyo.hyperion",
            "x-rpc-app_version": version,
            "x-rpc-client_type": "5",
            "x-rpc-channel": "miyousheluodi",
        }
        if game and game["signgame"]:
            headers["x-rpc-signgame"] = game["signgame"]
        return headers

    async def _request(self, method: str, url: str, cookie: str, *, params: dict | None = None,
                       payload: dict | None = None, game: dict[str, str] | None = None) -> dict:
        retries = max(1, min(int(self.config.get("api_retries", 2)), 5))
        last_error = "请求失败"
        for attempt in range(retries):
            try:
                session = await self._http()
                async with session.request(method, url, params=params, json=payload,
                                           headers=self._headers(cookie, game)) as response:
                    raw = await response.text()
                    try:
                        result = json.loads(raw)
                    except json.JSONDecodeError:
                        raise RuntimeError(f"HTTP {response.status} 返回了非 JSON 内容")
                    if response.status >= 500:
                        raise RuntimeError(f"HTTP {response.status}")
                    return result
            except (aiohttp.ClientError, asyncio.TimeoutError, RuntimeError) as exc:
                last_error = str(exc)
                if attempt + 1 < retries:
                    await asyncio.sleep(attempt + 1)
        raise RuntimeError(last_error)

    async def _roles(self, cookie: str, game_biz: str) -> tuple[list[dict], str | None]:
        result = await self._request("GET", ROLES_URL, cookie, params={"game_biz": game_biz})
        if result.get("retcode") != 0:
            return [], str(result.get("message") or f"错误码 {result.get('retcode')}")
        roles = (result.get("data") or {}).get("list") or []
        return [role for role in roles if role.get("game_uid") and role.get("region")], None

    async def _validate_cookie(self, cookie: str) -> tuple[str, dict[str, list[dict]], str | None]:
        cookie = clean_cookie(cookie)
        uid = cookie_value(cookie, "stuid", "stuid_v2", "account_id", "account_id_v2", "ltuid", "ltuid_v2", "login_uid")
        if not cookie or not uid:
            return "", {}, "Cookie 缺少登录 UID 字段，请复制完整 Cookie"
        found: dict[str, list[dict]] = {}
        login_error: str | None = None
        for biz in GAMES:
            try:
                roles, error = await self._roles(cookie, biz)
                if roles:
                    found[biz] = roles
                elif error and not login_error:
                    login_error = error
            except Exception as exc:
                if not login_error:
                    login_error = str(exc)
        if not found:
            return "", {}, f"Cookie 未验证通过：{login_error or '未查询到任何游戏角色'}"
        return uid, found, None

    def _urls(self, game: dict[str, str]) -> tuple[str, str]:
        base = f"{game['api']}/event/{game['path']}"
        return f"{base}/info", f"{base}/sign"

    async def _sign_role(self, cookie: str, game_biz: str, role: dict) -> str:
        game = GAMES[game_biz]
        name = game["name"]
        uid, region = str(role["game_uid"]), str(role["region"])
        display = f"{name} {role.get('nickname') or uid}({uid})"
        params = {"act_id": game["act_id"], "region": region, "uid": uid, "lang": "zh-cn"}
        info_url, sign_url = self._urls(game)
        try:
            info_result = await self._request("GET", info_url, cookie, params=params, game=game)
        except Exception as exc:
            return f"【{display}】网络错误：{exc}"
        code = info_result.get("retcode")
        if code in (-100, -101, 10001):
            return f"【{display}】Cookie 已失效，请重新绑定"
        if code != 0:
            return f"【{display}】查询失败：{info_result.get('message', code)}"
        info = info_result.get("data") or {}
        total = int(info.get("total_sign_day") or 0)
        if info.get("is_sign") is True or int(info.get("sign_cnt_missing") or 1) == 0:
            return f"【{display}】今日已签到（本月 {total} 天）"
        try:
            signed = await self._request("POST", sign_url, cookie, payload={
                "act_id": game["act_id"], "region": region, "uid": uid
            }, game=game)
        except Exception as exc:
            return f"【{display}】网络错误：{exc}"
        code = signed.get("retcode")
        if code == 0:
            award = (signed.get("data") or {}).get("award") or {}
            suffix = f"，奖励 {award.get('name')} x{award.get('cnt')}" if award else ""
            return f"【{display}】签到成功（本月 {total + 1} 天）{suffix}"
        if code == -5003:
            return f"【{display}】今日已签到（本月 {total} 天）"
        if code == 1034:
            return f"【{display}】触发验证码，请在米游社官方页面手动完成验证"
        if code in (-100, -101, 10001):
            return f"【{display}】Cookie 已失效，请重新绑定"
        return f"【{display}】签到失败：{signed.get('message', code)}"

    async def _sign_account(self, account: dict[str, Any], games: list[str]) -> list[str]:
        cookie = account.get("cookie", "")
        cached_roles = account.setdefault("roles", {})
        lines: list[str] = []
        for biz in games:
            try:
                roles, error = await self._roles(cookie, biz)
                if error:
                    lines.append(f"【{GAMES[biz]['name']}】{error}")
                    continue
                cached_roles[biz] = roles
                if not roles:
                    lines.append(f"【{GAMES[biz]['name']}】未找到绑定角色")
                    continue
                for role in roles:
                    lines.append(await self._sign_role(cookie, biz, role))
                    await asyncio.sleep(random.uniform(0.4, 0.9))
            except Exception as exc:
                lines.append(f"【{GAMES[biz]['name']}】请求异常：{exc}")
        return lines

    @filter.command("米游社绑定", alias={"mys绑定"})
    async def bind(self, event: AstrMessageEvent):
        cookie = clean_cookie(self._argument(event, "绑定"))
        if not cookie:
            yield event.plain_result("用法：/mys绑定 <完整 Cookie>。请在私聊中执行，避免泄露账号凭据。")
            return
        yield event.plain_result("正在验证 Cookie 并绑定账号…")
        uid, roles, error = await self._validate_cookie(cookie)
        if error:
            yield event.plain_result(f"绑定失败：{error}")
            return
        sender = self._sender(event)
        user = self._user(sender)
        accounts = user["accounts"]
        account = {"uid": uid, "cookie": cookie, "roles": roles, "bound_at": int(time.time())}
        index = next((i for i, item in enumerate(accounts) if item.get("uid") == uid), None)
        if index is None:
            accounts.append(account)
            index = len(accounts) - 1
            action = "已新增"
        else:
            accounts[index] = account
            action = "已更新"
        user["active_index"] = index
        user["umo"] = event.unified_msg_origin
        await self.store.save()
        games = "、".join(GAMES[biz]["name"] for biz in roles)
        yield event.plain_result(f"{action}账号 {uid}，识别到：{games}。自动签到结果将只推送到当前会话。")

    @filter.command("米游社签到", alias={"mys签到"})
    async def sign(self, event: AstrMessageEvent):
        sender = self._sender(event)
        account = self._active_account(sender)
        if not account:
            yield event.plain_result("尚未绑定账号。请私聊发送：/mys绑定 <完整 Cookie>")
            return
        requested = self._argument(event, "签到")
        games = self._enabled_games(requested)
        if not games:
            yield event.plain_result("未识别游戏。支持：原神、崩铁、绝区零、崩坏3、未定、崩坏2。")
            return
        yield event.plain_result("正在签到，请稍候…")
        async with self.sign_lock:
            lines = await self._sign_account(account, games)
            await self.store.save()
        yield event.plain_result("米游社签到结果\n" + "\n".join(lines))

    @filter.command("米游社测试", alias={"mys测试"})
    async def test(self, event: AstrMessageEvent):
        account = self._active_account(self._sender(event))
        if not account:
            yield event.plain_result("尚未绑定账号。请先使用 /mys绑定 <完整 Cookie>。")
            return
        uid, roles, error = await self._validate_cookie(account.get("cookie", ""))
        if error:
            yield event.plain_result(f"Cookie 测试失败：{error}")
            return
        account["roles"] = roles
        await self.store.save()
        yield event.plain_result(f"Cookie 有效，账号 {uid}，游戏：" + "、".join(GAMES[b]["name"] for b in roles))

    @filter.command("米游社推送到这里", alias={"mys推送到这里", "mys推送"})
    async def bind_push_target(self, event: AstrMessageEvent):
        """Set this conversation as the current user's automatic-result target."""
        sender = self._sender(event)
        if not self._active_account(sender):
            yield event.plain_result("尚未绑定账号。请先在私聊中使用 /mys绑定 <完整 Cookie>。")
            return
        self._user(sender)["umo"] = event.unified_msg_origin
        await self.store.save()
        yield event.plain_result("已将自动签到结果推送到当前会话。")

    @filter.command("米游社我的", alias={"mys我的"})
    async def accounts(self, event: AstrMessageEvent):
        user = self.store.data.get(self._sender(event)) or {}
        items = user.get("accounts") or []
        if not items:
            yield event.plain_result("尚未绑定账号。")
            return
        active = user.get("active_index", 0)
        lines = ["已绑定米游社账号："]
        for index, account in enumerate(items):
            games = "、".join(GAMES[b]["name"] for b in (account.get("roles") or {}) if b in GAMES) or "待检测"
            marker = "（当前）" if index == active else ""
            lines.append(f"{index + 1}. {account.get('uid', '?')} {marker} - {games}")
        yield event.plain_result("\n".join(lines))

    @filter.command("米游社切换", alias={"mys切换"})
    async def switch(self, event: AstrMessageEvent):
        user = self.store.data.get(self._sender(event)) or {}
        accounts = user.get("accounts") or []
        try:
            index = int(self._argument(event, "切换")) - 1
        except ValueError:
            index = -1
        if not 0 <= index < len(accounts):
            yield event.plain_result("请输入有效序号。可用 /mys我的 查看账号列表。")
            return
        user["active_index"] = index
        await self.store.save()
        yield event.plain_result(f"已切换到账号 {index + 1}：{accounts[index].get('uid', '?')}")

    @filter.command("米游社解绑", alias={"mys解绑"})
    async def unbind(self, event: AstrMessageEvent):
        sender = self._sender(event)
        user = self.store.data.get(sender) or {}
        accounts = user.get("accounts") or []
        if not accounts:
            yield event.plain_result("尚未绑定账号。")
            return
        if self._argument(event, "解绑").lower() in {"全部", "all"}:
            del self.store.data[sender]
            message = "已解绑当前用户的全部账号。"
        else:
            index = user.get("active_index", 0)
            removed = accounts.pop(index)
            if not accounts:
                del self.store.data[sender]
            else:
                user["active_index"] = min(index, len(accounts) - 1)
            message = f"已解绑账号 {removed.get('uid', '?')}。"
        await self.store.save()
        yield event.plain_result(message)

    async def _scheduler(self):
        while True:
            try:
                raw = str(self.config.get("sign_time", "09:00"))
                hour, minute = (int(part) for part in raw.split(":", 1))
                if not (0 <= hour <= 23 and 0 <= minute <= 59):
                    raise ValueError
            except (ValueError, TypeError):
                hour, minute = 9, 0
            now = datetime.now(BEIJING)
            target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if target <= now:
                target += timedelta(days=1)
            try:
                await asyncio.sleep((target - now).total_seconds())
                await self._auto_sign_all()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error(f"米游社自动签到任务异常：{exc}")
                await asyncio.sleep(60)

    async def _auto_sign_all(self):
        games = self._enabled_games()
        if not games:
            logger.warning("米游社自动签到：未启用任何游戏")
            return
        for sender, user in list(self.store.data.items()):
            accounts = user.get("accounts") or []
            if not accounts:
                continue
            report = ["米游社自动签到结果"]
            async with self.sign_lock:
                for index, account in enumerate(accounts):
                    report.append(f"账号 {index + 1}（{account.get('uid', '?')}）")
                    report.extend(await self._sign_account(account, games))
                await self.store.save()
            text = "\n".join(report)
            logger.info(f"米游社自动签到 {sender}：\n{text}")
            if self.config.get("push_result", True) and user.get("umo"):
                try:
                    await self.context.send_message(user["umo"], MessageChain().message(text))
                except Exception as exc:
                    logger.warning(f"米游社签到结果推送失败：{exc}")

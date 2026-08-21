"""
astrbot_plugin_points - 群积分签到系统

面向群聊的积分系统：每日签到、积分排行榜、积分抽奖、积分商店兑换，
并提供 Web 管理页面（嵌入 AstrBot 仪表盘）用于配置奖励/抽奖/商品。

许可证: MIT
"""

import asyncio
import json
import os
import random
from datetime import date, datetime, timedelta

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star
from astrbot.api import logger
from astrbot.api.web import json_response, error_response, request

try:
    from renderer import CardRenderer
except Exception:
    CardRenderer = None

PLUGIN_NAME = "astrbot_plugin_points"
DATA_FILE = "points_data.json"

DEFAULT_DATA = {
    "version": 1,
    "settings": {
        "signin_points": 10,      # 每次签到基础积分
        "streak_bonus_max": 7,    # 连续签到每日额外加成的上限
        "lottery_cost": 10,       # 每次抽奖消耗的积分
        "lottery_min": 1,         # 抽奖单次最低奖励
        "lottery_max": 30,        # 抽奖单次最高奖励
        "rank_top": 10,           # 排行榜默认展示人数
        "whitelist_groups": [],   # 空=不限制；非空则仅允许列出的群使用
        "font_path": "",          # 自定义中文字体路径（可选），留空自动探测系统字体
        "use_image": True,        # 是否输出图片卡片（排行榜 / 积分）
    },
    "users": {},      # key = "<group_id>|<user_id>"
    "products": [],   # {id, name, desc, cost, stock, icon}
}


class PointsStore:
    """积分数据的存取。数据持久化在 AstrBot 的 data 目录下，使用 JSON 文件保存。

    并发安全说明：所有"读-改-写"操作都在 self.lock 内完成，写盘用内部
    同步方法 _dump（不重复加锁，避免死锁）。
    """

    def __init__(self, data_dir: str):
        os.makedirs(data_dir, exist_ok=True)
        self.path = os.path.join(data_dir, DATA_FILE)
        self.lock = asyncio.Lock()
        self.data = self._load()

    def _load(self):
        if os.path.exists(self.path):
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                # 兼容旧数据：缺省键补默认值
                for key, value in DEFAULT_DATA.items():
                    loaded.setdefault(key, json.loads(json.dumps(value)))
                return loaded
            except Exception as exc:
                logger.error(f"[积分系统] 读取数据文件失败，使用默认数据: {exc}")
        return json.loads(json.dumps(DEFAULT_DATA))

    def _dump(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)

    # ------------------------------------------------------------------ #
    # 用户
    # ------------------------------------------------------------------ #
    @staticmethod
    def _key(group_id, user_id):
        return f"{group_id}|{user_id}"

    def _ensure_user(self, group_id, user_id, name=""):
        key = self._key(group_id, user_id)
        user = self.data["users"].get(key)
        if user is None:
            user = {
                "group_id": str(group_id),
                "user_id": str(user_id),
                "name": name or "未知用户",
                "points": 0,
                "total_earned": 0,   # 累计获得积分
                "total_spent": 0,    # 累计花费积分
                "signin_count": 0,
                "streak": 0,
                "last_signin": None,
                "history": [],       # 最近积分流水 [{time, delta, reason}]
                "purchases": [],
            }
            self.data["users"][key] = user
        elif name and user["name"] in ("未知用户", ""):
            user["name"] = name
        return user

    def _record(self, user, delta, reason):
        """记录一笔积分变动流水（delta 为净变化，与 points 变化量一致）。"""
        user.setdefault("history", []).append({
            "time": datetime.now().strftime("%m-%d %H:%M"),
            "delta": delta,
            "reason": reason,
        })
        # 仅保留最近 20 条，避免文件无限膨胀
        user["history"] = user["history"][-20:]

    async def signin(self, group_id, user_id, name):
        async with self.lock:
            user = self._ensure_user(group_id, user_id, name)
            today = date.today().isoformat()
            yesterday = (date.today() - timedelta(days=1)).isoformat()
            if user["last_signin"] == today:
                return {"ok": False, "reason": "already",
                        "points": user["points"], "streak": user["streak"]}

            user["streak"] = user["streak"] + 1 if user["last_signin"] == yesterday else 1
            base = int(self.data["settings"].get("signin_points", 10))
            bonus = min(max(user["streak"] - 1, 0),
                        int(self.data["settings"].get("streak_bonus_max", 7)))
            reward = base + bonus
            user["points"] += reward
            user["signin_count"] += 1
            user["total_earned"] = user.get("total_earned", 0) + reward
            user["last_signin"] = today
            self._record(user, reward, f"签到（连续 {user['streak']} 天）")
            self._dump()

        return {"ok": True, "reward": reward, "bonus": bonus,
                "streak": user["streak"], "points": user["points"],
                "signin_count": user["signin_count"]}

    async def lottery(self, group_id, user_id, name):
        async with self.lock:
            user = self._ensure_user(group_id, user_id, name)
            cost = int(self.data["settings"].get("lottery_cost", 10))
            if user["points"] < cost:
                return {"ok": False, "reason": "insufficient",
                        "need": cost, "points": user["points"]}
            low = int(self.data["settings"].get("lottery_min", 1))
            high = int(self.data["settings"].get("lottery_max", 30))
            reward = random.randint(low, high)
            jackpot = random.random() < 0.01  # 1% 概率 3 倍彩蛋
            if jackpot:
                reward *= 3
            user["points"] = user["points"] - cost + reward
            user["total_earned"] = user.get("total_earned", 0) + reward
            user["total_spent"] = user.get("total_spent", 0) + cost
            self._record(user, reward - cost, "抽奖")
            self._dump()

        return {"ok": True, "cost": cost, "reward": reward,
                "jackpot": jackpot, "points": user["points"]}

    async def add_points(self, group_id, user_id, name, delta):
        """管理员调整积分。delta 可为负。"""
        async with self.lock:
            user = self._ensure_user(group_id, user_id, name)
            user["points"] = max(0, user["points"] + delta)
            if delta >= 0:
                user["total_earned"] = user.get("total_earned", 0) + delta
            else:
                user["total_spent"] = user.get("total_spent", 0) - delta
            self._record(user, delta, "管理员调整")
            self._dump()
        return {"ok": True, "points": user["points"], "delta": delta}

    async def redeem(self, group_id, user_id, name, product_id, count=1):
        async with self.lock:
            user = self._ensure_user(group_id, user_id, name)
            if int(count) < 1:
                return {"ok": False, "reason": "bad_count"}
            product = next(
                (p for p in self.data["products"] if p["id"] == product_id), None)
            if product is None:
                return {"ok": False, "reason": "not_found"}
            cost = int(product["cost"]) * int(count)
            if user["points"] < cost:
                return {"ok": False, "reason": "insufficient",
                        "need": cost, "points": user["points"]}
            stock = product.get("stock")
            if stock not in (None, "") and int(stock) != -1 and int(stock) < int(count):
                return {"ok": False, "reason": "out_of_stock", "stock": int(stock)}

            user["points"] -= cost
            user["total_spent"] = user.get("total_spent", 0) + cost
            if stock not in (None, "") and int(stock) != -1:
                product["stock"] = int(stock) - int(count)
            user["purchases"].append({
                "product_id": product_id,
                "name": product.get("name", product_id),
                "cost": cost,
                "count": int(count),
                "time": datetime.now().isoformat(timespec="seconds"),
            })
            self._record(user, -cost, f"兑换 {product['name']}")
            self._dump()

        return {"ok": True, "name": product["name"], "cost": cost,
                "count": int(count), "points": user["points"]}

    def ranking(self, group_id=None, top=None):
        top = top or int(self.data["settings"].get("rank_top", 10))
        users = list(self.data["users"].values())
        if group_id is not None:
            users = [u for u in users if str(u["group_id"]) == str(group_id)]
        users.sort(key=lambda u: u["points"], reverse=True)
        return users[:top]

    # ------------------------------------------------------------------ #
    # 设置与商品（主要被 Web 面板调用）
    # ------------------------------------------------------------------ #
    async def update_settings(self, patch: dict):
        async with self.lock:
            for key, value in patch.items():
                if key == "whitelist_groups":
                    self.data["settings"][key] = [str(x).strip() for x in value]
                elif key == "font_path":
                    self.data["settings"][key] = str(value or "").strip()
                elif key == "use_image":
                    self.data["settings"][key] = bool(value)
                elif key in ("signin_points", "streak_bonus_max", "lottery_cost",
                             "lottery_min", "lottery_max", "rank_top"):
                    self.data["settings"][key] = max(0, int(value))
            self._dump()
        return dict(self.data["settings"])

    async def upsert_product(self, product: dict):
        async with self.lock:
            product_id = str(product.get("id", "")).strip()
            if not product_id:
                return None
            clean = {
                "id": product_id,
                "name": str(product.get("name", product_id)).strip(),
                "desc": str(product.get("desc", "")).strip(),
                "cost": max(0, int(product.get("cost", 0))),
                "stock": self._norm_stock(product.get("stock")),
                "icon": str(product.get("icon", "")).strip(),
            }
            for idx, p in enumerate(self.data["products"]):
                if p["id"] == product_id:
                    self.data["products"][idx] = clean
                    break
            else:
                self.data["products"].append(clean)
            self._dump()
        return clean

    @staticmethod
    def _norm_stock(stock):
        if stock in (None, "", "-1"):
            return -1  # -1 表示不限量
        return max(0, int(stock))

    async def delete_product(self, product_id: str):
        async with self.lock:
            before = len(self.data["products"])
            self.data["products"] = [
                p for p in self.data["products"] if p["id"] != product_id]
            deleted = before - len(self.data["products"])
            if deleted:
                self._dump()
        return deleted


class PointsPlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self.store = PointsStore(self._data_dir())
        self.renderer = CardRenderer(
            os.path.join(self._data_dir(), "img")) if CardRenderer else None
        self._register_web_api()
        logger.info("[积分系统] 插件已加载，数据文件: %s", self.store.path)

    def _apply_font(self):
        if self.renderer:
            self.renderer.set_font(
                self.store.data["settings"].get("font_path", "") or "")

    def _data_dir(self):
        try:
            return self.context.get_data_dir()
        except Exception:
            return self.context.data_dir

    # ------------------------------------------------------------------ #
    # 通用工具
    # ------------------------------------------------------------------ #
    @staticmethod
    def _ids(event: AstrMessageEvent):
        group_id = str(getattr(event.message_obj, "group_id", "") or "")
        sender = getattr(event.message_obj, "sender", None)
        user_id = ""
        for attr in ("user_id", "userId"):
            if sender is not None and getattr(sender, attr, None):
                user_id = str(getattr(sender, attr))
                break
        if not user_id:
            user_id = event.get_sender_id() if hasattr(event, "get_sender_id") else ""
        if not user_id:
            user_id = event.get_sender_name()
        return group_id, user_id

    def _allowed(self, group_id: str) -> bool:
        if not group_id:
            return True  # 私聊不限制，白名单只约束群聊
        wl = self.store.data["settings"].get("whitelist_groups") or []
        if not wl:
            return True
        return str(group_id) in [str(x) for x in wl]

    @staticmethod
    def _plain(event: AstrMessageEvent, text: str):
        return event.plain_result(text)

    # ------------------------------------------------------------------ #
    # 群聊指令
    # ------------------------------------------------------------------ #
    @filter.command("签到")
    async def cmd_signin(self, event: AstrMessageEvent):
        group_id, user_id = self._ids(event)
        if not self._allowed(group_id):
            yield self._plain(event, "本群未开启积分系统。")
            return
        name = event.get_sender_name()
        result = await self.store.signin(group_id, user_id, name)
        if result["ok"]:
            bonus_txt = f"（含连续签到加成 +{result['bonus']}）" if result["bonus"] else ""
            yield self._plain(
                event,
                f"签到成功！本次 +{result['reward']} 积分{bonus_txt}\n"
                f"已连续签到 {result['streak']} 天，当前积分 {result['points']}\n"
                f"累计签到 {result['signin_count']} 次。")
        else:
            yield self._plain(
                event,
                f"你今天已经签到过了，明天再来吧。当前积分 {result['points']}")

    @filter.command("积分")
    async def cmd_points(self, event: AstrMessageEvent):
        group_id, user_id = self._ids(event)
        if not self._allowed(group_id):
            yield self._plain(event, "本群未开启积分系统。")
            return
        name = event.get_sender_name()
        user = await self._ensure_async_user(group_id, user_id, name)
        earned = user.get("total_earned", 0)
        spent = user.get("total_spent", 0)
        lines = [
            f"{user['name']} 的积分：{user['points']}",
            f"累计获得 {earned}，累计花费 {spent}",
            f"连续签到 {user['streak']} 天，累计签到 {user['signin_count']} 次",
        ]
        recent = (user.get("history") or [])[-5:]
        if recent:
            lines.append("")
            lines.append("最近记录：")
            for row in reversed(recent):
                sign = "+" if row["delta"] > 0 else ""
                lines.append(f"  {row.get('time', '')}  {sign}{row['delta']}  {row.get('reason', '')}")
        use_img = self.store.data["settings"].get("use_image", True)
        if use_img and self.renderer:
            self._apply_font()
            img = self.renderer.render_profile(user)
            if img:
                yield event.image_result(img)
                return
        yield self._plain(event, "\n".join(lines))

    async def _ensure_async_user(self, group_id, user_id, name):
        user = self.store.get_user(group_id, user_id)
        if user is None:
            # 仅查询不应产生数据，直接返回一份空视图
            return {"name": name or user_id, "points": 0, "streak": 0, "signin_count": 0}
        return user

    @filter.command("排行榜")
    async def cmd_ranking(self, event: AstrMessageEvent, top: int = 0):
        group_id, _ = self._ids(event)
        if not self._allowed(group_id):
            yield self._plain(event, "本群未开启积分系统。")
            return
        limit = top if top and top > 0 else int(
            self.store.data["settings"].get("rank_top", 10))
        limit = max(1, min(limit, 50))
        rows = self.store.ranking(group_id=group_id or None, top=limit)
        if not rows:
            yield self._plain(event, "还没有任何积分记录，快让大家来签到吧。")
            return
        lines = ["积分排行榜"]
        for idx, u in enumerate(rows, start=1):
            name = (u["name"] or "未知用户")[:12]
            lines.append(f"{idx}. {name} —— {u['points']} 积分")
        use_img = self.store.data["settings"].get("use_image", True)
        if use_img and self.renderer:
            self._apply_font()
            img = self.renderer.render_ranking(
                rows, f"群 {group_id}" if group_id else "全站")
            if img:
                yield event.image_result(img)
                return
        yield self._plain(event, "\n".join(lines))

    @filter.command("抽奖")
    async def cmd_lottery(self, event: AstrMessageEvent):
        group_id, user_id = self._ids(event)
        if not self._allowed(group_id):
            yield self._plain(event, "本群未开启积分系统。")
            return
        name = event.get_sender_name()
        result = await self.store.lottery(group_id, user_id, name)
        if result["ok"]:
            jackpot_txt = "\n触发彩蛋，本次奖励翻三倍。" if result["jackpot"] else ""
            yield self._plain(
                event,
                f"花费 {result['cost']} 积分，抽中 +{result['reward']} 积分。{jackpot_txt}"
                f"\n当前积分 {result['points']}")
        else:
            yield self._plain(
                event,
                f"积分不足，抽奖需要 {result['need']} 积分（当前 {result['points']}）。"
                f"先签到攒积分吧。")

    @filter.command("商店")
    async def cmd_shop(self, event: AstrMessageEvent):
        products = self.store.data["products"]
        if not products:
            yield self._plain(event, "商店暂时没有上架任何商品。")
            return
        lines = ["—— 积分商店 ——"]
        for p in products:
            product_icon = p.get("icon") or ""
            stock = "不限量" if p.get("stock") in (None, -1) else f"剩 {p['stock']} 件"
            icon_txt = f"{product_icon} " if product_icon else ""
            lines.append(f"\n{icon_txt}{p['name']}\n"
                         f"  价格 {p['cost']} 积分 | {stock}\n"
                         f"  编号 {p['id']} · {p.get('desc', '') or '无描述'}")
        lines.append("\n兑换方式：/兑换 <编号> [数量]")
        yield self._plain(event, "\n".join(lines))

    @filter.command("兑换")
    async def cmd_redeem(self, event: AstrMessageEvent, product_id: str, count: int = 1):
        group_id, user_id = self._ids(event)
        if not self._allowed(group_id):
            yield self._plain(event, "本群未开启积分系统。")
            return
        name = event.get_sender_name()
        result = await self.store.redeem(group_id, user_id, name, product_id, count)
        if result["ok"]:
            yield self._plain(
                event,
                f"兑换成功！你用 {result['cost']} 积分兑换了 "
                f"{result['name']} ×{result['count']}。\n当前剩余 {result['points']} 积分。")
        elif result["reason"] == "not_found":
            yield self._plain(event, "找不到这个商品编号，试试 /商店 查看。")
        elif result["reason"] == "insufficient":
            yield self._plain(
                event, f"积分不足，需要 {result['need']} 积分（当前 {result['points']}）。")
        elif result["reason"] == "bad_count":
            yield self._plain(event, "兑换数量需要是正整数。")
        elif result["reason"] == "out_of_stock":
            yield self._plain(event, "抱歉，该商品缺货了（剩余不足）。")

    @filter.command("我的背包")
    async def cmd_backpack(self, event: AstrMessageEvent):
        group_id, user_id = self._ids(event)
        if not self._allowed(group_id):
            yield self._plain(event, "本群未开启积分系统。")
            return
        user = self.store.get_user(group_id, user_id)
        purchases = (user or {}).get("purchases") or []
        if not purchases:
            yield self._plain(event, "背包里还没有任何物品，去 /商店 看看能兑换什么。")
            return
        agg = {}
        for p in purchases:
            pkey = p.get("product_id") or "?"
            item = agg.setdefault(pkey, {"name": p.get("name", pkey),
                                         "count": 0, "cost": 0})
            item["count"] += p.get("count", 1)
            item["cost"] += p.get("cost", 0)
        lines = [f"{user['name']} 的背包："]
        for item in agg.values():
            lines.append(f"  {item['name']}  x{item['count']}   累计花费 {item['cost']} 积分")
        yield self._plain(event, "\n".join(lines))

    @filter.command("积分帮助")
    async def cmd_help(self, event: AstrMessageEvent):
        yield self._plain(
            event,
            "积分系统指令：\n"
            "/签到         每日签到领取积分\n"
            "/积分         查看我的积分与历史记录\n"
            "/我的背包     查看已兑换的物品\n"
            "/排行榜 [N]   查看积分排行榜\n"
            "/抽奖         花费积分抽奖\n"
            "/商店         查看商店商品\n"
            "/兑换 <编号> [数量]  使用积分兑换\n"
            "管理员可在插件管理页面配置奖励与商品。")

    # ------------------------------------------------------------------ #
    # Web 管理面板 API
    # ------------------------------------------------------------------ #
    def _register_web_api(self):
        register = self.context.register_web_api
        register(f"/{PLUGIN_NAME}/overview", self.api_overview, ["GET"], "积分系统总览")
        register(f"/{PLUGIN_NAME}/ranking", self.api_ranking, ["GET"], "积分排行榜")
        register(f"/{PLUGIN_NAME}/users", self.api_users, ["GET"], "用户列表")
        register(f"/{PLUGIN_NAME}/users/adjust", self.api_adjust_points, ["POST"], "调整积分")
        register(f"/{PLUGIN_NAME}/products", self.api_products, ["GET", "POST"], "商品读写")
        register(f"/{PLUGIN_NAME}/products/<product_id>/delete", self.api_product_delete,
                 ["POST"], "删除商品")
        register(f"/{PLUGIN_NAME}/settings", self.api_settings, ["GET", "POST"], "设置读写")

    def _guard(self):
        if not request.username:
            return error_response("未授权，请通过 AstrBot 面板访问", status_code=401)
        return None

    async def api_overview(self):
        err = self._guard()
        if err:
            return err
        users = self.store.data["users"]
        products = self.store.data["products"]
        today = date.today().isoformat()
        today_signins = sum(1 for u in users.values() if u["last_signin"] == today)
        return json_response({
            "total_users": len(users),
            "total_points": sum(u["points"] for u in users.values()),
            "today_signins": today_signins,
            "product_count": len(products),
            "settings": dict(self.store.data["settings"]),
        })

    async def api_ranking(self):
        err = self._guard()
        if err:
            return err
        group_id = request.query.get("group_id", "", type=str)
        top = request.query.get("top", 20, type=int)
        rows = self.store.ranking(group_id or None, max(1, min(top, 100)))
        return json_response({"rows": rows})

    async def api_users(self):
        err = self._guard()
        if err:
            return err
        keyword = request.query.get("keyword", "", type=str)
        group_id = request.query.get("group_id", "", type=str)
        all_users = list(self.store.data["users"].values())
        if group_id:
            all_users = [u for u in all_users if str(u["group_id"]) == str(group_id)]
        if keyword:
            kw = keyword.lower()
            all_users = [u for u in all_users
                         if kw in str(u.get("name", "")).lower()
                         or kw in str(u.get("user_id", "")).lower()]
        all_users.sort(key=lambda u: u["points"], reverse=True)
        return json_response({
            "count": len(all_users),
            "users": all_users[:200],
            "groups": sorted({str(u["group_id"]) for u in self.store.data["users"].values()}),
        })

    async def api_adjust_points(self):
        err = self._guard()
        if err:
            return err
        body = await request.json(default={}) or {}
        group_id = body.get("group_id", "")
        user_id = body.get("user_id", "")
        name = body.get("name", "")
        try:
            delta = int(body.get("delta", 0))
        except (TypeError, ValueError):
            return error_response("delta 必须是整数")
        if not user_id or delta == 0:
            return error_response("user_id 与 delta 必填")
        result = await self.store.add_points(group_id, user_id, name, delta)
        return json_response(result)

    async def api_products(self):
        err = self._guard()
        if err:
            return err
        if request.method == "POST":
            body = await request.json(default={}) or {}
            saved = await self.store.upsert_product(body)
            if saved is None:
                return error_response("商品 id 不能为空")
            return json_response({"saved": saved})
        return json_response({"products": self.store.data["products"]})

    async def api_product_delete(self, product_id: str):
        err = self._guard()
        if err:
            return err
        deleted = await self.store.delete_product(product_id)
        if not deleted:
            return error_response("商品不存在")
        return json_response({"deleted": deleted})

    async def api_settings(self):
        err = self._guard()
        if err:
            return err
        if request.method == "POST":
            body = await request.json(default={}) or {}
            saved = await self.store.update_settings(body)
            return json_response({"settings": saved})
        return json_response({"settings": dict(self.store.data["settings"])})
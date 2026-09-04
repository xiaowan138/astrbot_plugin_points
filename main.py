"""
astrbot_plugin_points - 群积分签到系统

面向群聊的积分系统：每日签到、积分排行榜、积分抽奖、积分商店兑换、
每日任务、成就、周/月赛季榜、兑换核销与管理员审计，
并提供 Web 管理页面（嵌入 AstrBot 仪表盘）用于配置一切。

许可证: MIT
"""

import asyncio
import json
import os
import random
import re
import uuid
from datetime import datetime, timedelta, timezone

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star
from astrbot.api import logger
from astrbot.api.web import json_response, error_response, request

try:
    from renderer import CardRenderer
except Exception:
    CardRenderer = None

# 可选依赖：旧版 AstrBot 缺少时对应功能自动降级，不影响插件加载
try:
    from astrbot.api.event import EventMessageType
except Exception:
    EventMessageType = None

try:
    from astrbot.core.message.message_event_result import MessageChain
except Exception:
    try:
        from astrbot.api.event import MessageChain
    except Exception:
        MessageChain = None


def _evt_all():
    """事件监听装饰器，缺依赖时退化为空装饰器。"""
    try:
        return filter.event_message_type(EventMessageType.ALL)
    except Exception:
        return lambda fn: fn


PLUGIN_NAME = "astrbot_plugin_points"
DATA_FILE = "points_data.json"

DEFAULT_DATA = {
    "version": 2,
    "settings": {
        "signin_points": 10,      # 每次签到基础积分
        "streak_bonus_max": 7,    # 连续签到每日额外加成的上限
        "lottery_cost": 10,       # 每次抽奖消耗的积分
        "lottery_min": 1,         # 抽奖单次最低奖励
        "lottery_max": 30,        # 抽奖单次最高奖励
        "rank_top": 10,           # 排行榜默认展示人数
        "whitelist_groups": [],   # 空=不限制；非空则仅允许列出的群使用
        "font_path": "",          # 自定义中文字体路径（可选），留空自动探测系统字体
        "use_image": True,        # 是否输出图片卡片（排行榜 / 积分 / 商店）
        "admin_user_ids": [],     # 超级管理员（可执行管理指令；群主/管理员默认也可）
        "lottery_daily_limit": 0, # 每日抽奖次数上限，0=不限
        "milestone_rewards": {"7": 50, "30": 300, "365": 3650},  # 连续签到里程碑 {天数: 积分}
        "allow_private": False,   # 是否允许私聊使用积分系统
        "timezone_offset": 8,     # UTC 偏移小时数（决定"今日"的日界）
        "reminder_enabled": False,    # 每日签到提醒
        "reminder_time": "21:00",     # 提醒时间 HH:MM（按 timezone_offset）
        "reminder_platform": "aiocqhttp",  # 发送提醒使用的平台适配器
        "chat_reward_enabled": False,  # 发言赚积分开关
        "chat_points_per_msg": 1,      # 每条发言积分
        "chat_daily_points": 20,       # 每日发言积分上限
        "task_chat_count": 10,         # 每日任务：发言条数
        "task_chat_reward": 5,         # 每日任务：发言奖励
        "task_lottery_reward": 5,      # 每日任务：抽奖 1 次奖励
        "task_signin_reward": 5,       # 每日任务：签到奖励
        "season_rewards": [300, 200, 100],  # 赛季榜冠亚季军奖励
        "achievements_enabled": True,  # 成就系统开关
        "lottery_use_pool": False,     # 抽奖是否使用自定义奖池
    },
    "users": {},        # key = "<group_id>|<user_id>"
    "products": [],     # {id, name, desc, cost, stock, icon, need_verify}
    "prizes": [],       # {id, name, points, weight}
    "redemptions": [],  # {id, group_id, user_id, name, product_name, count, cost, time, status}
    "audit_log": [],    # {time, admin_id, admin_name, action, detail}
    "seasons": [],      # 归档的赛季榜
}

# 成就定义（静态，条件随数据自动检查）
ACHIEVEMENT_DEFS = [
    {"id": "sign7", "name": "初来乍到", "desc": "累计签到 7 天", "reward": 20,
     "cond": lambda u: u.get("signin_count", 0) >= 7},
    {"id": "sign30", "name": "坚持一月", "desc": "累计签到 30 天", "reward": 100,
     "cond": lambda u: u.get("signin_count", 0) >= 30},
    {"id": "sign100", "name": "百日筑基", "desc": "累计签到 100 天", "reward": 500,
     "cond": lambda u: u.get("signin_count", 0) >= 100},
    {"id": "streak7", "name": "七连击", "desc": "连续签到 7 天", "reward": 50,
     "cond": lambda u: u.get("streak", 0) >= 7},
    {"id": "streak30", "name": "全勤之星", "desc": "连续签到 30 天", "reward": 300,
     "cond": lambda u: u.get("streak", 0) >= 30},
    {"id": "earn1000", "name": "千分大户", "desc": "累计获得 1000 积分", "reward": 100,
     "cond": lambda u: u.get("total_earned", 0) >= 1000},
    {"id": "spend500", "name": "消费达人", "desc": "累计消费 500 积分", "reward": 50,
     "cond": lambda u: u.get("total_spent", 0) >= 500},
    {"id": "gift1", "name": "慷慨解囊", "desc": "首次赠送积分", "reward": 20,
     "cond": lambda u: u.get("gift_count", 0) >= 1},
]


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

    # ------------------------------------------------------------------ #
    # 基础
    # ------------------------------------------------------------------ #
    def _load(self):
        if os.path.exists(self.path):
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                # 兼容旧数据：缺省键补默认值
                for key, value in DEFAULT_DATA.items():
                    loaded.setdefault(key, json.loads(json.dumps(value)))
                for key, value in DEFAULT_DATA["settings"].items():
                    loaded["settings"].setdefault(key, json.loads(json.dumps(value)))
                # 旧版本库存可能是字符串 "-1"，统一归一为整数
                for p in loaded.get("products", []):
                    p["stock"] = self._norm_stock(p.get("stock"))
                    p.setdefault("need_verify", False)
                return loaded
            except Exception as exc:
                logger.error(f"[积分系统] 读取数据文件失败，使用默认数据: {exc}")
        return json.loads(json.dumps(DEFAULT_DATA))

    def _dump(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)

    def _tz(self):
        try:
            off = int(self.data["settings"].get("timezone_offset", 8))
        except (TypeError, ValueError):
            off = 8
        return timezone(timedelta(hours=off))

    def _today(self):
        """按配置时区返回今天。"""
        return datetime.now(timezone.utc).astimezone(self._tz()).date()

    def _now(self):
        return datetime.now(timezone.utc).astimezone(self._tz())

    @staticmethod
    def _week_key(day):
        iso = day.isocalendar()
        return f"{iso[0]}-W{iso[1]:02d}"

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
                "lottery_date": None,
                "lottery_count": 0,
                "chat_date": None,
                "chat_count": 0,
                "chat_points_date": None,
                "chat_points": 0,
                "gift_count": 0,
                "tasks": {"date": None, "claimed": []},
                "achievements": [],
                "period": {"week": "", "week_points": 0, "month": "", "month_points": 0},
            }
            self.data["users"][key] = user
        elif name and user["name"] in ("未知用户", ""):
            user["name"] = name
        return user

    def get_user(self, group_id, user_id):
        """只读查询用户；不存在返回 None（不创建数据）。"""
        return self.data["users"].get(self._key(group_id, user_id))

    def _record(self, user, delta, reason):
        """记录一笔积分变动流水（delta 为净变化，与 points 变化量一致）。"""
        user.setdefault("history", []).append({
            "time": self._now().strftime("%m-%d %H:%M"),
            "delta": delta,
            "reason": reason,
        })
        # 仅保留最近 20 条，避免文件无限膨胀
        user["history"] = user["history"][-20:]

    def _earn(self, user, amount):
        """统一入口：入账积分并累计到周/月赛季。"""
        user["total_earned"] = user.get("total_earned", 0) + amount
        self._bump_period(user, amount)

    def _bump_period(self, user, amount):
        if amount <= 0:
            return
        today = self._today()
        wk, mn = self._week_key(today), today.strftime("%Y-%m")
        p = user.setdefault("period", {})
        if p.get("week") != wk:
            p["week"] = wk
            p["week_points"] = 0
        if p.get("month") != mn:
            p["month"] = mn
            p["month_points"] = 0
        p["week_points"] = p.get("week_points", 0) + amount
        p["month_points"] = p.get("month_points", 0) + amount

    # ------------------------------------------------------------------ #
    # 成就
    # ------------------------------------------------------------------ #
    def _check_achievements(self, user):
        if not self.data["settings"].get("achievements_enabled", True):
            return []
        new = []
        for _ in range(3):  # 允许奖励积分再触发下一档成就，最多 3 轮
            got = False
            for d in ACHIEVEMENT_DEFS:
                if any(a.get("id") == d["id"] for a in user.get("achievements") or []):
                    continue
                if d["cond"](user):
                    user.setdefault("achievements", []).append(
                        {"id": d["id"], "time": self._now().isoformat(timespec="seconds")})
                    r = int(d["reward"])
                    user["points"] += r
                    self._earn(user, r)
                    self._record(user, r, f"成就「{d['name']}」")
                    new.append({"name": d["name"], "reward": r})
                    got = True
            if not got:
                break
        return new

    # ------------------------------------------------------------------ #
    # 每日任务
    # ------------------------------------------------------------------ #
    def _task_defs(self):
        s = self.data["settings"]
        return {
            "signin": ("每日签到", int(s.get("task_signin_reward", 5))),
            "chat": (f"发言 {int(s.get('task_chat_count', 10))} 条",
                     int(s.get("task_chat_reward", 5))),
            "lottery": ("抽奖 1 次", int(s.get("task_lottery_reward", 5))),
        }

    def task_view(self, group_id, user_id):
        """只读返回今日任务完成状态。"""
        user = self.get_user(group_id, user_id) or {}
        s = self.data["settings"]
        today = self._today().isoformat()
        t = user.get("tasks") or {}
        claimed = t.get("claimed", []) if t.get("date") == today else []
        chat_n = user.get("chat_count", 0) if user.get("chat_date") == today else 0
        lot_n = user.get("lottery_count", 0) if user.get("lottery_date") == today else 0
        rows = []
        for key, (label, reward) in self._task_defs().items():
            if key == "signin":
                done = user.get("last_signin") == today
                progress = "1/1"
            elif key == "chat":
                need = int(s.get("task_chat_count", 10))
                done = chat_n >= need
                progress = f"{min(chat_n, need)}/{need}"
            else:
                done = lot_n >= 1
                progress = f"{min(lot_n, 1)}/1"
            rows.append({"key": key, "label": label, "reward": reward,
                         "done": done, "claimed": key in claimed, "progress": progress})
        return rows

    async def claim_task(self, group_id, user_id, name, key):
        alias = {"签到": "signin", "发言": "chat", "抽奖": "lottery"}
        key = alias.get(str(key).strip(), str(key).strip())
        async with self.lock:
            user = self._ensure_user(group_id, user_id, name)
            s = self.data["settings"]
            today = self._today().isoformat()
            t = user.setdefault("tasks", {})
            if t.get("date") != today:
                user["tasks"] = t = {"date": today, "claimed": []}
            defs = self._task_defs()
            if key not in defs:
                return {"ok": False, "reason": "unknown"}
            if key in t["claimed"]:
                return {"ok": False, "reason": "claimed"}
            if key == "signin":
                done = user.get("last_signin") == today
            elif key == "chat":
                done = (user.get("chat_date") == today
                        and user.get("chat_count", 0) >= int(s.get("task_chat_count", 10)))
            else:
                done = (user.get("lottery_date") == today
                        and user.get("lottery_count", 0) >= 1)
            if not done:
                return {"ok": False, "reason": "not_done", "label": defs[key][0]}
            reward = defs[key][1]
            user["points"] += reward
            self._earn(user, reward)
            self._record(user, reward, "每日任务奖励")
            t["claimed"].append(key)
            ach = self._check_achievements(user)
            self._dump()
        return {"ok": True, "reward": reward, "label": defs[key][0],
                "points": user["points"], "achievements": ach}

    async def on_chat(self, group_id, user_id, name):
        """群消息监听回调：累计发言数 / 发言积分 /（任务进度由查询时实时计算）。"""
        s = self.data["settings"]
        async with self.lock:
            # 未开启发言奖励且用户尚未注册时跳过，避免大群把潜水者都写进数据文件
            if (self.get_user(group_id, user_id) is None
                    and not s.get("chat_reward_enabled")):
                return 0
            user = self._ensure_user(group_id, user_id, name)
            today = self._today().isoformat()
            if user.get("chat_date") != today:
                user["chat_date"] = today
                user["chat_count"] = 0
            user["chat_count"] = user.get("chat_count", 0) + 1
            awarded = 0
            if s.get("chat_reward_enabled"):
                per = max(0, int(s.get("chat_points_per_msg", 1)))
                cap = max(0, int(s.get("chat_daily_points", 20)))
                used = user.get("chat_points", 0) if user.get("chat_points_date") == today else 0
                give = min(per, max(0, cap - used))
                if give > 0:
                    user["points"] += give
                    self._earn(user, give)
                    self._record(user, give, "发言奖励")
                    user["chat_points_date"] = today
                    user["chat_points"] = used + give
                    awarded = give
            # 纯计数的消息不逐条写盘，仅在产生积分或首条时落盘
            if awarded or user["chat_count"] == 1:
                self._dump()
        return awarded

    # ------------------------------------------------------------------ #
    # 核心玩法
    # ------------------------------------------------------------------ #
    async def signin(self, group_id, user_id, name):
        async with self.lock:
            user = self._ensure_user(group_id, user_id, name)
            today = self._today().isoformat()
            yesterday = (self._today() - timedelta(days=1)).isoformat()
            if user["last_signin"] == today:
                return {"ok": False, "reason": "already",
                        "points": user["points"], "streak": user["streak"]}

            user["streak"] = user["streak"] + 1 if user["last_signin"] == yesterday else 1
            base = int(self.data["settings"].get("signin_points", 10))
            bonus = min(max(user["streak"] - 1, 0),
                        int(self.data["settings"].get("streak_bonus_max", 7)))
            base_reward = base + bonus
            _config = self.data["settings"].get("milestone_rewards") or {}
            milestone_bonus = int(_config.get(str(user["streak"]), 0) or 0)
            reward = base_reward + milestone_bonus
            user["points"] += reward
            user["signin_count"] += 1
            self._earn(user, reward)
            user["last_signin"] = today
            self._record(user, reward, f"签到（连续 {user['streak']} 天）")
            ach = self._check_achievements(user)
            self._dump()

        return {"ok": True, "reward": reward, "bonus": bonus,
                "streak": user["streak"], "points": user["points"],
                "signin_count": user["signin_count"],
                "milestone_day": (int(user["streak"]) if milestone_bonus else None),
                "milestone_bonus": milestone_bonus, "achievements": ach}

    def _pick_prize(self):
        """按权重抽取自定义奖池；无可用奖池返回 None。"""
        prizes = [p for p in self.data.get("prizes", [])
                  if int(p.get("weight", 1) or 0) > 0]
        if not prizes:
            return None
        total = sum(int(p["weight"]) for p in prizes)
        pick, acc = random.uniform(0, total), 0
        for p in prizes:
            acc += int(p["weight"])
            if pick <= acc:
                return p
        return prizes[-1]

    async def lottery(self, group_id, user_id, name):
        async with self.lock:
            user = self._ensure_user(group_id, user_id, name)
            cost = int(self.data["settings"].get("lottery_cost", 10))
            _limit = int(self.data["settings"].get("lottery_daily_limit", 0) or 0)
            today = self._today().isoformat()
            if _limit > 0:
                if user.get("lottery_date") != today:
                    user["lottery_date"] = today
                    user["lottery_count"] = 0
                if user.get("lottery_count", 0) >= _limit:
                    return {"ok": False, "reason": "daily_limit", "limit": _limit,
                            "points": user["points"]}
            if user["points"] < cost:
                return {"ok": False, "reason": "insufficient",
                        "need": cost, "points": user["points"]}

            prize = (self._pick_prize()
                     if self.data["settings"].get("lottery_use_pool") else None)
            if prize is not None:
                reward = max(0, int(prize.get("points", 0)))
                prize_name = prize.get("name", "")
                jackpot = False
            else:
                low = int(self.data["settings"].get("lottery_min", 1))
                high = int(self.data["settings"].get("lottery_max", 30))
                reward = random.randint(low, high)
                prize_name = ""
                jackpot = random.random() < 0.01  # 1% 概率 3 倍彩蛋
                if jackpot:
                    reward *= 3
            user["points"] = user["points"] - cost + reward
            if reward:
                self._earn(user, reward)
            user["total_spent"] = user.get("total_spent", 0) + cost
            user["lottery_date"] = today
            user["lottery_count"] = user.get("lottery_count", 0) + 1
            self._record(user, reward - cost,
                         f"抽奖「{prize_name}」" if prize_name else "抽奖")
            ach = self._check_achievements(user)
            self._dump()

        return {"ok": True, "cost": cost, "reward": reward,
                "jackpot": jackpot, "prize_name": prize_name,
                "points": user["points"],
                "count_today": user.get("lottery_count", 1),
                "limit": _limit, "achievements": ach}

    async def add_points(self, group_id, user_id, name, delta):
        """管理员调整积分。delta 可为负；扣分最多扣到 0，账目按实际扣减记录。"""
        async with self.lock:
            user = self._ensure_user(group_id, user_id, name)
            applied = int(delta)
            if applied >= 0:
                user["points"] += applied
                self._earn(user, applied)
            else:
                actual = min(-applied, user["points"])  # 最多扣到 0
                user["points"] -= actual
                user["total_spent"] = user.get("total_spent", 0) + actual
                applied = -actual
            if applied != 0:
                self._record(user, applied, "管理员调整")
            ach = self._check_achievements(user)
            self._dump()
        return {"ok": True, "points": user["points"], "delta": applied,
                "achievements": ach}

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
                "time": self._now().isoformat(timespec="seconds"),
            })
            self._record(user, -cost, f"兑换 {product['name']}")
            redemption_id = None
            if product.get("need_verify"):
                redemption_id = "R" + uuid.uuid4().hex[:6].upper()
                self.data.setdefault("redemptions", []).append({
                    "id": redemption_id,
                    "group_id": str(group_id),
                    "user_id": user["user_id"],
                    "name": user["name"],
                    "product_name": product.get("name", product_id),
                    "count": int(count),
                    "cost": cost,
                    "time": self._now().isoformat(timespec="seconds"),
                    "status": "pending",
                })
            ach = self._check_achievements(user)
            self._dump()

        return {"ok": True, "name": product["name"], "cost": cost,
                "count": int(count), "points": user["points"],
                "redemption_id": redemption_id, "achievements": ach}

    def confirm_redemption(self, redemption_id):
        """核销：pending → confirmed。返回条目或 None。"""
        for r in self.data.get("redemptions", []):
            if r.get("id") == redemption_id:
                if r.get("status") == "pending":
                    r["status"] = "confirmed"
                    r["confirmed_time"] = self._now().isoformat(timespec="seconds")
                    self._dump()
                return r
        return None

    def pending_redemptions(self, group_id=None):
        rows = [r for r in self.data.get("redemptions", [])
                if r.get("status") == "pending"]
        if group_id:
            rows = [r for r in rows if str(r.get("group_id")) == str(group_id)]
        return rows

    def ranking(self, group_id=None, top=None):
        top = top or int(self.data["settings"].get("rank_top", 10))
        users = list(self.data["users"].values())
        if group_id is not None:
            users = [u for u in users if str(u["group_id"]) == str(group_id)]
        users.sort(key=lambda u: u["points"], reverse=True)
        return users[:top]

    # ------------------------------------------------------------------ #
    # 赛季榜
    # ------------------------------------------------------------------ #
    def period_ranking(self, period, group_id=None, top=10):
        """period: "week" | "month"，按本周期内新增积分排序。"""
        today = self._today()
        kk = "week" if period == "week" else "month"
        kf = "week_points" if period == "week" else "month_points"
        cur = self._week_key(today) if period == "week" else today.strftime("%Y-%m")
        users = [u for u in self.data["users"].values()
                 if (u.get("period") or {}).get(kk) == cur]
        if group_id:
            users = [u for u in users if str(u["group_id"]) == str(group_id)]
        users.sort(key=lambda u: (u.get("period") or {}).get(kf, 0), reverse=True)
        return users[:top]

    def _season_rewards(self):
        raw = self.data["settings"].get("season_rewards") or []
        if isinstance(raw, str):
            raw = [x.strip() for x in raw.replace("，", ",").split(",")]
        out = []
        for x in raw:
            try:
                out.append(max(0, int(x)))
            except (TypeError, ValueError):
                continue
        return out[:10]

    async def settle_season(self, period, group_id=None):
        """归档当前周期榜单，发放冠亚季奖励，并清零该周期计数。

        同一周期只允许结算一次；本周期没有任何积分产出时不结算，
        防止重复结算导致"凭空发奖"。
        """
        async with self.lock:
            kk = "week" if period == "week" else "month"
            kf = "week_points" if period == "week" else "month_points"
            today = self._today()
            label = self._week_key(today) if period == "week" else today.strftime("%Y-%m")
            for arch in self.data.get("seasons", []):
                if arch.get("period") == period and arch.get("key") == label:
                    return {"ok": False, "reason": "already_settled", "key": label}
            rewards = self._season_rewards()
            rows = [u for u in self.period_ranking(period, group_id, len(rewards))
                    if (u.get("period") or {}).get(kf, 0) > 0]
            if not rows:
                return {"ok": False, "reason": "no_activity", "key": label}
            arch = {
                "period": period, "key": label,
                "time": self._now().isoformat(timespec="seconds"),
                "ranking": [{"name": u.get("name"), "user_id": u.get("user_id"),
                             "points": (u.get("period") or {}).get(kf, 0)} for u in rows],
            }
            granted = []
            for u, r in zip(rows, rewards):
                if r > 0:
                    u["points"] += r
                    self._earn(u, r)
                    self._record(u, r, f"{'周' if period == 'week' else '月'}榜赛季奖励")
                    granted.append({"name": u.get("name"), "reward": r})
            for u in self.data["users"].values():
                if group_id and str(u["group_id"]) != str(group_id):
                    continue
                u.setdefault("period", {})[kf] = 0
            self.data.setdefault("seasons", []).append(arch)
            self._dump()
        return {"ok": True, "archived": arch, "granted": granted}

    # ------------------------------------------------------------------ #
    # 赠送 / 补签到 / 重置
    # ------------------------------------------------------------------ #
    async def transfer(self, group_id, from_uid, from_name, to_uid, to_name, amount):
        """用户间赠送积分。from_uid 转账给 to_uid，需在同一群。"""
        async with self.lock:
            if int(amount) < 1:
                return {"ok": False, "reason": "bad_amount"}
            if str(from_uid) and str(from_uid) == str(to_uid):
                return {"ok": False, "reason": "self"}
            frm = self._ensure_user(group_id, from_uid, from_name)
            if frm["points"] < int(amount):
                return {"ok": False, "reason": "insufficient", "points": frm["points"]}
            to = self._ensure_user(group_id, to_uid, to_name)
            frm["points"] -= int(amount)
            frm["total_spent"] = frm.get("total_spent", 0) + int(amount)
            frm["gift_count"] = frm.get("gift_count", 0) + 1
            to["points"] += int(amount)
            self._earn(to, int(amount))
            self._record(frm, -int(amount), f"赠送 {to['name'] or to_uid}")
            self._record(to, int(amount), f"收到 {frm['name'] or from_uid} 的赠送")
            ach_f = self._check_achievements(frm)
            ach_t = self._check_achievements(to)
            self._dump()
        return {"ok": True, "amount": int(amount), "to": to["name"],
                "from_points": frm["points"],
                "achievements": ach_f + ach_t}

    async def backfill_signin(self, group_id, user_id, name):
        """管理员补签到：与正常签到一致地累计连续天数并发放奖励。"""
        async with self.lock:
            user = self._ensure_user(group_id, user_id, name)
            today = self._today().isoformat()
            yesterday = (self._today() - timedelta(days=1)).isoformat()
            if user["last_signin"] == today:
                return {"ok": False, "reason": "already", "points": user["points"]}
            user["streak"] = user["streak"] + 1 if user["last_signin"] == yesterday else 1
            base = int(self.data["settings"].get("signin_points", 10))
            bonus = min(max(user["streak"] - 1, 0),
                        int(self.data["settings"].get("streak_bonus_max", 7)))
            _config = self.data["settings"].get("milestone_rewards") or {}
            milestone_bonus = int(_config.get(str(user["streak"]), 0) or 0)
            reward = base + bonus + milestone_bonus
            user["points"] += reward
            user["signin_count"] += 1
            self._earn(user, reward)
            user["last_signin"] = today
            self._record(user, reward, "管理员补签到")
            ach = self._check_achievements(user)
            self._dump()
        return {"ok": True, "reward": reward, "points": user["points"],
                "streak": user["streak"], "achievements": ach}

    async def reset_user(self, group_id, user_id):
        """清空一个用户的全部群积分数据。"""
        async with self.lock:
            key = self._key(group_id, user_id)
            existed = key in self.data["users"]
            self.data["users"].pop(key, None)
            if existed:
                self._dump()
        return existed

    # ------------------------------------------------------------------ #
    # 审计日志
    # ------------------------------------------------------------------ #
    def add_audit(self, admin_id, admin_name, action, detail):
        self.data.setdefault("audit_log", []).append({
            "time": self._now().isoformat(timespec="seconds"),
            "admin_id": str(admin_id or ""),
            "admin_name": str(admin_name or ""),
            "action": action,
            "detail": str(detail),
        })
        self.data["audit_log"] = self.data["audit_log"][-200:]
        self._dump()

    # ------------------------------------------------------------------ #
    # 设置、商品、奖池（主要被 Web 面板调用）
    # ------------------------------------------------------------------ #
    async def update_settings(self, patch: dict):
        async with self.lock:
            for key, value in patch.items():
                if key == "whitelist_groups":
                    self.data["settings"][key] = [str(x).strip() for x in value
                                                  if str(x).strip()]
                elif key == "admin_user_ids":
                    if isinstance(value, (list, tuple)):
                        self.data["settings"][key] = [str(x).strip() for x in value
                                                      if str(x).strip()]
                    else:
                        self.data["settings"][key] = [str(x).strip() for x in
                                                      str(value or "").replace("，", ",").split(",")
                                                      if str(x).strip()]
                elif key == "font_path":
                    self.data["settings"][key] = str(value or "").strip()
                elif key == "reminder_platform":
                    self.data["settings"][key] = str(value or "").strip() or "aiocqhttp"
                elif key == "reminder_time":
                    v = str(value or "").strip()
                    if re.fullmatch(r"\d{1,2}:\d{2}", v):
                        hh, mm = v.split(":")
                        if 0 <= int(hh) <= 23 and 0 <= int(mm) <= 59:
                            self.data["settings"][key] = f"{int(hh):02d}:{mm}"
                elif key == "season_rewards":
                    self.data["settings"][key] = self._parse_rewards(value)
                elif key == "milestone_rewards":
                    self.data["settings"][key] = self._parse_milestones(value)
                elif key in ("use_image", "allow_private", "chat_reward_enabled",
                             "reminder_enabled", "achievements_enabled",
                             "lottery_use_pool"):
                    self.data["settings"][key] = bool(value)
                elif key == "timezone_offset":
                    v = self._safe_int(value)
                    if v is not None:
                        self.data["settings"][key] = max(-12, min(14, v))
                elif key in ("signin_points", "streak_bonus_max", "lottery_cost",
                             "lottery_min", "lottery_max", "rank_top",
                             "lottery_daily_limit", "task_chat_count",
                             "task_chat_reward", "task_lottery_reward",
                             "task_signin_reward", "chat_points_per_msg",
                             "chat_daily_points"):
                    v = self._safe_int(value)
                    if v is not None:
                        self.data["settings"][key] = max(0, v)
            # 抽奖区间保护：min 不能大于 max
            s = self.data["settings"]
            if int(s.get("lottery_min", 0)) > int(s.get("lottery_max", 0)):
                s["lottery_min"], s["lottery_max"] = s["lottery_max"], s["lottery_min"]
            self._dump()
        return dict(self.data["settings"])

    @staticmethod
    def _parse_rewards(value):
        raw = value if isinstance(value, (list, tuple)) else \
            str(value or "").replace("，", ",").split(",")
        out = []
        for x in raw:
            try:
                out.append(max(0, int(str(x).strip())))
            except (TypeError, ValueError):
                continue
        return out[:10]

    @staticmethod
    def _safe_int(value):
        """解析整数；非法输入返回 None（调用方据此跳过该项，保留原值）。"""
        try:
            return int(str(value).strip())
        except (TypeError, ValueError):
            return None

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
                "need_verify": bool(product.get("need_verify")),
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
        return max(-1, int(stock))

    async def delete_product(self, product_id: str):
        async with self.lock:
            before = len(self.data["products"])
            self.data["products"] = [
                p for p in self.data["products"] if p["id"] != product_id]
            deleted = before - len(self.data["products"])
            if deleted:
                self._dump()
        return deleted

    async def upsert_prize(self, prize: dict):
        async with self.lock:
            prize_id = str(prize.get("id", "")).strip()
            if not prize_id:
                return None
            clean = {
                "id": prize_id,
                "name": str(prize.get("name", prize_id)).strip(),
                "points": max(0, int(prize.get("points", 0))),
                "weight": max(1, int(prize.get("weight", 1))),
            }
            prizes = self.data.setdefault("prizes", [])
            for idx, p in enumerate(prizes):
                if p["id"] == prize_id:
                    prizes[idx] = clean
                    break
            else:
                prizes.append(clean)
            self._dump()
        return clean

    async def delete_prize(self, prize_id: str):
        async with self.lock:
            before = len(self.data.get("prizes", []))
            self.data["prizes"] = [
                p for p in self.data.get("prizes", []) if p["id"] != prize_id]
            deleted = before - len(self.data["prizes"])
            if deleted:
                self._dump()
        return deleted

    @staticmethod
    def _parse_milestones(value):
        """把 {7:50,...} 或字符串 "7:50,30:300" 解析为 {str: int}。"""
        out = {}
        pairs = []
        if isinstance(value, dict):
            pairs = list(value.items())
        elif isinstance(value, str):
            for part in str(value).replace("，", ",").split(","):
                part = part.strip()
                if ":" in part:
                    pairs.append(tuple(part.split(":", 1)))
                elif "=" in part:
                    pairs.append(tuple(part.split("=", 1)))
        for k, v in pairs:
            try:
                out[str(int(k))] = max(0, int(v))
            except (TypeError, ValueError):
                continue
        return out


class PointsPlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self.store = PointsStore(self._data_dir())
        self.renderer = CardRenderer(
            os.path.join(self._data_dir(), "img")) if CardRenderer else None
        self._bg_task = None
        self._last_reminder_day = None
        self._register_web_api()
        logger.info("[积分系统] 插件已加载，数据文件: %s", self.store.path)

    async def initialize(self):
        """生命周期钩子：启动后台提醒任务。"""
        try:
            self._bg_task = asyncio.create_task(self._reminder_loop())
        except Exception as exc:
            logger.warning(f"[积分系统] 签到提醒任务启动失败：{exc}")

    async def terminate(self):
        if self._bg_task:
            self._bg_task.cancel()
            self._bg_task = None

    async def _reminder_loop(self):
        """每 30 秒检查一次是否到达提醒时间（配 2 分钟容忍窗口，不会漏发）。"""
        while True:
            try:
                await asyncio.sleep(30)
                await self._try_remind()
            except asyncio.CancelledError:
                return
            except Exception as exc:
                logger.debug(f"[积分系统] 提醒循环异常：{exc}")

    async def _try_remind(self):
        s = self.store.data["settings"]
        if not s.get("reminder_enabled"):
            return
        target = str(s.get("reminder_time", "21:00"))
        m = re.fullmatch(r"(\d{1,2}):(\d{2})", target)
        if not m:
            return
        now = self.store._now()
        # 容忍窗口：轮询间隔漂移也不会跳过目标分钟
        target_min = int(m.group(1)) * 60 + int(m.group(2))
        now_min = now.hour * 60 + now.minute
        if not 0 <= now_min - target_min < 2:
            return
        today = now.date().isoformat()
        if self._last_reminder_day == today:
            return
        self._last_reminder_day = today
        groups = [str(g) for g in (s.get("whitelist_groups") or [])]
        if not groups:
            return  # 未配置白名单时不盲目打扰
        platform = str(s.get("reminder_platform") or "aiocqhttp")
        if MessageChain is None:
            logger.warning("[积分系统] 当前版本不支持主动发消息，签到提醒已跳过")
            return
        text = "⏰ 今天还没签到的小伙伴记得 /签到 领积分哦～"
        for gid in groups:
            try:
                umo = f"{platform}:GroupMessage:{gid}"
                await self.context.send_message(umo, MessageChain().message(text))
            except Exception as exc:
                logger.warning(f"[积分系统] 向群 {gid} 发送提醒失败：{exc}")

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
            # 私聊是否可用由开关决定，默认关闭避免"私聊积分平行空间"
            return bool(self.store.data["settings"].get("allow_private", False))
        wl = self.store.data["settings"].get("whitelist_groups") or []
        if not wl:
            return True
        return str(group_id) in [str(x) for x in wl]

    @staticmethod
    def _plain(event: AstrMessageEvent, text: str):
        return event.plain_result(text)

    @staticmethod
    def _ach_text(achievements):
        if not achievements:
            return ""
        return "\n" + "".join(
            f"🏆 解锁成就「{a['name']}」+{a['reward']} 积分\n" for a in achievements)

    @staticmethod
    def _extract_at_ids(event: AstrMessageEvent):
        """提取消息中所有被 @ 的用户 ID，跨平台兼容 aiocqhttp / qq_official。"""
        ats = []
        try:
            for seg in event.message:
                if getattr(seg, "type", "") != "at":
                    continue
                data = getattr(seg, "data", None) or {}
                for key in ("qq", "user_id", "open_id", "openid", "id"):
                    if data.get(key) is not None:
                        ats.append(str(data[key]))
                        break
        except Exception:
            pass
        return ats

    @staticmethod
    def _resolve_target(event: AstrMessageEvent, first_arg, second_arg):
        """解析管理/赠送目标与数量：优先取 @，否则取数字参数。"""
        ats = PointsPlugin._extract_at_ids(event)
        if ats:
            return ats[0], PointsPlugin._to_int(first_arg)
        if str(first_arg or "").strip().isdigit():
            return str(first_arg).strip(), PointsPlugin._to_int(second_arg)
        return None, PointsPlugin._to_int(first_arg)

    @staticmethod
    def _to_int(val):
        try:
            return int(val)
        except (TypeError, ValueError):
            return 0

    def _is_admin(self, event: AstrMessageEvent, user_id: str) -> bool:
        ids = self.store.data["settings"].get("admin_user_ids") or []
        if user_id and user_id in [str(x) for x in ids]:
            return True
        sender = getattr(event.message_obj, "sender", None)
        if sender is None:
            return False
        # 角色字段名在不同平台各不相同：role / permission / auth...
        role = str(self._get_field(sender, "role", "permission", "authority") or "").strip()
        if role.lower() in ("owner", "admin", "administrator",
                            "群主", "管理员", "管理員", "manage"):
            return True
        # 部分协议用布尔字段标识群主/管理员
        for flag in ("is_owner", "is_admin", "is_operator",
                     "is_group_owner", "is_group_admin", "is_effective_admin"):
            if self._get_field(sender, flag) is True:
                return True
        return False

    @staticmethod
    def _get_field(obj, *names, default=None):
        """从 sender 提取字段，兼容「对象属性」与「dict」两种实现。"""
        if obj is None:
            return default
        if isinstance(obj, dict):
            for n in names:
                if obj.get(n) is not None:
                    return obj[n]
        else:
            for n in names:
                v = getattr(obj, n, None)
                if v is not None:
                    return v
        return default

    # ------------------------------------------------------------------ #
    # 群消息监听：发言计数 / 发言积分
    # ------------------------------------------------------------------ #
    if EventMessageType is not None:
        @_evt_all()
        async def _on_chat_message(self, event: AstrMessageEvent):
            try:
                group_id, user_id = self._ids(event)
                if not group_id or not user_id or not self._allowed(group_id):
                    return
                text = getattr(event, "message_str", "") or ""
                if text.startswith("/"):
                    return
                await self.store.on_chat(group_id, user_id,
                                         event.get_sender_name() or "")
            except Exception:
                pass

    # ------------------------------------------------------------------ #
    # 群聊指令
    # ------------------------------------------------------------------ #
    @filter.command("签到")
    async def cmd_signin(self, event: AstrMessageEvent):
        group_id, user_id = self._ids(event)
        if not self._allowed(group_id):
            yield self._plain(event, "积分系统仅在群聊开放。")
            return
        result = await self.store.signin(group_id, user_id, event.get_sender_name())
        if result["ok"]:
            bonus_txt = f"（含连续签到加成 +{result['bonus']}）" if result["bonus"] else ""
            m_txt = (f"\n🎉 连续签到 {result['milestone_day']} 天里程碑，额外 +{result['milestone_bonus']} 积分！"
                     if result.get("milestone_bonus") else "")
            yield self._plain(
                event,
                f"签到成功！本次 +{result['reward']} 积分{bonus_txt}\n"
                f"已连续签到 {result['streak']} 天，当前积分 {result['points']}\n"
                f"累计签到 {result['signin_count']} 次。{m_txt}"
                f"{self._ach_text(result.get('achievements'))}")
        else:
            yield self._plain(
                event,
                f"你今天已经签到过了，明天再来吧。当前积分 {result['points']}")

    @filter.command("积分")
    async def cmd_points(self, event: AstrMessageEvent):
        group_id, user_id = self._ids(event)
        if not self._allowed(group_id):
            yield self._plain(event, "积分系统仅在群聊开放。")
            return
        user = self.store.get_user(group_id, user_id)
        if user is None:
            yield self._plain(event, "你还没有任何积分记录，先 /签到 开始吧。")
            return
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

    @filter.command("排行榜")
    async def cmd_ranking(self, event: AstrMessageEvent, top: str = ""):
        group_id, _ = self._ids(event)
        if not self._allowed(group_id):
            yield self._plain(event, "积分系统仅在群聊开放。")
            return
        try:
            top_n = int(str(top).strip()) if str(top).strip() else 0
        except (TypeError, ValueError):
            top_n = 0
        limit = top_n if top_n > 0 else int(
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

    @filter.command("周榜")
    async def cmd_week_rank(self, event: AstrMessageEvent):
        async for r in self._period_rank_cmd(event, "week", "本周"):
            yield r

    @filter.command("月榜")
    async def cmd_month_rank(self, event: AstrMessageEvent):
        async for r in self._period_rank_cmd(event, "month", "本月"):
            yield r

    async def _period_rank_cmd(self, event, period, label):
        group_id, _ = self._ids(event)
        if not self._allowed(group_id):
            yield self._plain(event, "积分系统仅在群聊开放。")
            return
        rows = self.store.period_ranking(period, group_id or None, 10)
        if not rows:
            yield self._plain(event, f"{label}还没有积分产出，快让大家活跃起来。")
            return
        kf = "week_points" if period == "week" else "month_points"
        lines = [f"{label}积分榜（按新增积分）"]
        for idx, u in enumerate(rows, start=1):
            name = (u["name"] or "未知用户")[:12]
            lines.append(f"{idx}. {name} —— {(u.get('period') or {}).get(kf, 0)} 积分")
        yield self._plain(event, "\n".join(lines))

    @filter.command("抽奖")
    async def cmd_lottery(self, event: AstrMessageEvent):
        group_id, user_id = self._ids(event)
        if not self._allowed(group_id):
            yield self._plain(event, "积分系统仅在群聊开放。")
            return
        result = await self.store.lottery(group_id, user_id, event.get_sender_name())
        if result["ok"]:
            if result.get("prize_name"):
                reward_txt = (f"抽中「{result['prize_name']}」"
                              + (f" +{result['reward']} 积分" if result["reward"] else "，谢谢参与"))
            else:
                reward_txt = f"抽中 +{result['reward']} 积分"
                if result["jackpot"]:
                    reward_txt += "\n触发彩蛋，本次奖励翻三倍。"
            extra = f"（今日已抽 {result['count_today']} 次）" if result["limit"] else ""
            yield self._plain(
                event,
                f"花费 {result['cost']} 积分，{reward_txt}\n"
                f"当前积分 {result['points']} {extra}"
                f"{self._ach_text(result.get('achievements'))}")
        elif result["reason"] == "daily_limit":
            yield self._plain(
                event,
                f"今日抽奖次数已用完（上限 {result['limit']} 次），明天再来吧。")
        else:
            yield self._plain(
                event,
                f"积分不足，抽奖需要 {result['need']} 积分（当前 {result['points']}）。"
                f"先签到攒积分吧。")

    @filter.command("商店")
    async def cmd_shop(self, event: AstrMessageEvent):
        group_id, _ = self._ids(event)
        if not self._allowed(group_id):
            yield self._plain(event, "积分系统仅在群聊开放。")
            return
        products = self.store.data["products"]
        if not products:
            yield self._plain(event, "商店暂时没有上架任何商品。")
            return
        use_img = self.store.data["settings"].get("use_image", True)
        if use_img and self.renderer:
            self._apply_font()
            img = self.renderer.render_shop(products)
            if img:
                yield event.image_result(img)
                return
        lines = ["—— 积分商店 ——"]
        for p in products:
            product_icon = p.get("icon") or ""
            stock = "不限量" if p.get("stock") in (None, -1) else f"剩 {p['stock']} 件"
            icon_txt = f"{product_icon} " if product_icon else ""
            verify_txt = "｜需核销" if p.get("need_verify") else ""
            lines.append(f"\n{icon_txt}{p['name']}\n"
                         f"  价格 {p['cost']} 积分 | {stock}{verify_txt}\n"
                         f"  编号 {p['id']} · {p.get('desc', '') or '无描述'}")
        lines.append("\n兑换方式：/兑换 <编号> [数量]")
        yield self._plain(event, "\n".join(lines))

    @filter.command("兑换")
    async def cmd_redeem(self, event: AstrMessageEvent,
                         product_id: str = "", count: str = "1"):
        group_id, user_id = self._ids(event)
        if not self._allowed(group_id):
            yield self._plain(event, "积分系统仅在群聊开放。")
            return
        try:
            n = int(str(count).strip())
            if n < 1:
                raise ValueError
        except (TypeError, ValueError):
            yield self._plain(event, "兑换数量需要是正整数，例如 /兑换 gift 2。")
            return
        result = await self.store.redeem(
            group_id, user_id, event.get_sender_name(), product_id, n)
        if result["ok"]:
            verify_txt = (f"\n兑换码：{result['redemption_id']}（请联系管理员核销）"
                          if result.get("redemption_id") else "")
            yield self._plain(
                event,
                f"兑换成功！你用 {result['cost']} 积分兑换了 "
                f"{result['name']} ×{result['count']}。\n"
                f"当前剩余 {result['points']} 积分。{verify_txt}"
                f"{self._ach_text(result.get('achievements'))}")
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
            yield self._plain(event, "积分系统仅在群聊开放。")
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

    @filter.command("任务")
    async def cmd_tasks(self, event: AstrMessageEvent):
        group_id, user_id = self._ids(event)
        if not self._allowed(group_id):
            yield self._plain(event, "积分系统仅在群聊开放。")
            return
        rows = self.store.task_view(group_id, user_id)
        lines = ["今日任务（完成后 /领取任务 领取积分）："]
        for r in rows:
            status = "✅ 已领取" if r["claimed"] else ("可领取！" if r["done"] else r["progress"])
            lines.append(f"  {r['label']}  +{r['reward']}  —— {status}")
        yield self._plain(event, "\n".join(lines))

    @filter.command("领取任务")
    async def cmd_claim_task(self, event: AstrMessageEvent, key: str = ""):
        group_id, user_id = self._ids(event)
        if not self._allowed(group_id):
            yield self._plain(event, "积分系统仅在群聊开放。")
            return
        alias = {"签到": "signin", "发言": "chat", "抽奖": "lottery"}
        key = alias.get(str(key).strip(), str(key).strip())
        if not key:
            yield self._plain(event, "用法：/领取任务 签到|发言|抽奖")
            return
        result = await self.store.claim_task(
            group_id, user_id, event.get_sender_name(), key)
        if result["ok"]:
            yield self._plain(
                event,
                f"✔ 任务「{result['label']}」完成，+{result['reward']} 积分，"
                f"当前 {result['points']} 积分。"
                f"{self._ach_text(result.get('achievements'))}")
        elif result["reason"] == "claimed":
            yield self._plain(event, "这个任务今天已经领过了。")
        elif result["reason"] == "not_done":
            yield self._plain(event, f"任务「{result['label']}」还没完成，加油！")
        else:
            yield self._plain(event, "没有这个任务，可用：签到 / 发言 / 抽奖。")

    @filter.command("成就")
    async def cmd_achievements(self, event: AstrMessageEvent):
        group_id, user_id = self._ids(event)
        if not self._allowed(group_id):
            yield self._plain(event, "积分系统仅在群聊开放。")
            return
        user = self.store.get_user(group_id, user_id) or {}
        earned_ids = {a.get("id") for a in user.get("achievements") or []}
        lines = ["成就列表："]
        for d in ACHIEVEMENT_DEFS:
            mark = "🏆" if d["id"] in earned_ids else "🔒"
            lines.append(f"  {mark} {d['name']}（{d['desc']}）+{d['reward']} 积分")
        got = len(earned_ids)
        lines.append(f"\n已解锁 {got}/{len(ACHIEVEMENT_DEFS)}。")
        yield self._plain(event, "\n".join(lines))

    @filter.command("赠送")
    async def cmd_gift(self, event: AstrMessageEvent, arg1: str = "", arg2: str = ""):
        group_id, user_id = self._ids(event)
        if not self._allowed(group_id):
            yield self._plain(event, "积分系统仅在群聊开放。")
            return
        target, amount = self._resolve_target(event, arg1, arg2)
        if not target or amount < 1:
            yield self._plain(event, "用法：/赠送 @群友 数量")
            return
        if str(target) == str(user_id):
            yield self._plain(event, "不能把积分送给自己哦。")
            return
        result = await self.store.transfer(
            group_id, user_id, event.get_sender_name(), target, "", amount)
        if result["ok"]:
            yield self._plain(
                event,
                f"✔ 已赠送 {result['amount']} 积分给 {result['to'] or target}，"
                f"你当前剩余 {result['from_points']} 积分。"
                f"{self._ach_text(result.get('achievements'))}")
        elif result["reason"] == "insufficient":
            yield self._plain(
                event, f"积分不足，你当前只有 {result['points']} 积分。先签到攒积分吧。")
        else:
            yield self._plain(event, "赠送失败，请检查数量和目标。")

    # ------------------------------------------------------------------ #
    # 管理员指令
    # ------------------------------------------------------------------ #
    @filter.command("加积分")
    async def cmd_admin_add(self, event: AstrMessageEvent, arg1: str = "", arg2: str = ""):
        group_id, user_id = self._ids(event)
        if not self._is_admin(event, user_id):
            yield self._plain(event, "仅群主/管理员（或已配置超级管理员）可执行此操作。")
            return
        target, amount = self._resolve_target(event, arg1, arg2)
        if not target or amount < 1:
            yield self._plain(event, "用法：/加积分 @用户 数量 或 /加积分 用户ID 数量")
            return
        result = await self.store.add_points(group_id, target, "", amount)
        self.store.add_audit(user_id, event.get_sender_name() or "",
                             "加积分", f"为 {target} +{amount}")
        yield self._plain(
            event,
            f"✔ 已为 {target} 增加 {amount} 积分，其当前积分 {result['points']}。"
            f"{self._ach_text(result.get('achievements'))}")

    @filter.command("扣积分")
    async def cmd_admin_sub(self, event: AstrMessageEvent, arg1: str = "", arg2: str = ""):
        group_id, user_id = self._ids(event)
        if not self._is_admin(event, user_id):
            yield self._plain(event, "仅群主/管理员（或已配置超级管理员）可执行此操作。")
            return
        target, amount = self._resolve_target(event, arg1, arg2)
        if not target or amount < 1:
            yield self._plain(event, "用法：/扣积分 @用户 数量 或 /扣积分 用户ID 数量")
            return
        result = await self.store.add_points(group_id, target, "", -amount)
        self.store.add_audit(user_id, event.get_sender_name() or "",
                             "扣积分", f"为 {target} -{amount}")
        yield self._plain(event, f"✔ 已为 {target} 扣除 {amount} 积分，其当前积分 {result['points']}。")

    @filter.command("补签到")
    async def cmd_admin_backfill(self, event: AstrMessageEvent, arg1: str = ""):
        group_id, user_id = self._ids(event)
        if not self._is_admin(event, user_id):
            yield self._plain(event, "仅群主/管理员（或已配置超级管理员）可执行此操作。")
            return
        ats = self._extract_at_ids(event)
        target = ats[0] if ats else str(arg1 or "").strip()
        if not target:
            yield self._plain(event, "用法：/补签到 @用户 或 /补签到 用户ID")
            return
        result = await self.store.backfill_signin(group_id, target, "")
        if result["ok"]:
            self.store.add_audit(user_id, event.get_sender_name() or "",
                                 "补签到", f"为 {target} 补签到")
            yield self._plain(
                event,
                f"✔ 已为 {target} 补签到，+{result['reward']} 积分，"
                f"当前 {result['points']} 积分。"
                f"{self._ach_text(result.get('achievements'))}")
        else:
            yield self._plain(event, f"{target} 今天已经签到过了，无需补签。")

    @filter.command("重置用户")
    async def cmd_admin_reset(self, event: AstrMessageEvent, arg1: str = ""):
        group_id, user_id = self._ids(event)
        if not self._is_admin(event, user_id):
            yield self._plain(event, "仅群主/管理员（或已配置超级管理员）可执行此操作。")
            return
        ats = self._extract_at_ids(event)
        target = ats[0] if ats else str(arg1 or "").strip()
        if not target:
            yield self._plain(event, "用法：/重置用户 用户ID")
            return
        existed = await self.store.reset_user(group_id, target)
        if existed:
            self.store.add_audit(user_id, event.get_sender_name() or "",
                                 "重置用户", f"清空 {target} 数据")
            yield self._plain(event, f"已清空用户 {target} 在本群的积分数据。")
        else:
            yield self._plain(event, f"没有找到用户 {target} 在本群的积分数据。")

    @filter.command("待核销")
    async def cmd_admin_pending(self, event: AstrMessageEvent):
        group_id, user_id = self._ids(event)
        if not self._is_admin(event, user_id):
            yield self._plain(event, "仅群主/管理员（或已配置超级管理员）可执行此操作。")
            return
        rows = self.store.pending_redemptions(group_id or None)
        if not rows:
            yield self._plain(event, "没有待核销的兑换。")
            return
        lines = ["待核销兑换："]
        for r in rows[:20]:
            lines.append(f"  {r['id']}  {r['name']} · {r['product_name']}"
                         f" ×{r['count']}  {r.get('time', '')}")
        lines.append("\n核销方式：/核销 <编号>")
        yield self._plain(event, "\n".join(lines))

    @filter.command("核销")
    async def cmd_admin_confirm(self, event: AstrMessageEvent, rid: str = ""):
        group_id, user_id = self._ids(event)
        if not self._is_admin(event, user_id):
            yield self._plain(event, "仅群主/管理员（或已配置超级管理员）可执行此操作。")
            return
        rid = str(rid or "").strip().upper()
        if not rid:
            yield self._plain(event, "用法：/核销 <兑换码>")
            return
        async with self.store.lock:
            row = self.store.confirm_redemption(rid)
        if row:
            self.store.add_audit(user_id, event.get_sender_name() or "",
                                 "核销", f"{rid} {row['name']} {row['product_name']}")
            yield self._plain(
                event,
                f"✔ 已核销 {rid}：{row['name']} 的 {row['product_name']} ×{row['count']}。")
        else:
            yield self._plain(event, f"找不到兑换码 {rid}。")

    @filter.command("赛季结算")
    async def cmd_admin_settle(self, event: AstrMessageEvent, period: str = "周"):
        group_id, user_id = self._ids(event)
        if not self._is_admin(event, user_id):
            yield self._plain(event, "仅群主/管理员（或已配置超级管理员）可执行此操作。")
            return
        period = "week" if str(period).strip() in ("周", "周榜", "week") else "month"
        label = "周榜" if period == "week" else "月榜"
        result = await self.store.settle_season(period, group_id or None)
        if not result.get("ok"):
            if result.get("reason") == "already_settled":
                yield self._plain(event, f"{label}（{result['key']}）已结算过，请勿重复结算。")
            else:
                yield self._plain(event, f"{label}（{result['key']}）本周期没有积分产出，无需结算。")
            return
        arch = result["archived"]
        self.store.add_audit(user_id, event.get_sender_name() or "",
                             "赛季结算", f"{label} {arch['key']}")
        lines = [f"✔ {label}（{arch['key']}）已结算归档："]
        for i, r in enumerate(arch["ranking"], start=1):
            lines.append(f"  {i}. {r['name']} —— {r['points']} 积分")
        if result["granted"]:
            lines.append("已发放奖励：" + "、".join(
                f"{g['name']} +{g['reward']}" for g in result["granted"]))
        else:
            lines.append("本周期没有需要发奖的用户。")
        yield self._plain(event, "\n".join(lines))

    @filter.command("审计日志")
    async def cmd_admin_audit(self, event: AstrMessageEvent):
        group_id, user_id = self._ids(event)
        if not self._is_admin(event, user_id):
            yield self._plain(event, "仅群主/管理员（或已配置超级管理员）可执行此操作。")
            return
        rows = (self.store.data.get("audit_log") or [])[-10:]
        if not rows:
            yield self._plain(event, "暂无管理员操作记录。")
            return
        lines = ["最近的管理员操作："]
        for r in reversed(rows):
            who = r.get("admin_name") or r.get("admin_id") or "?"
            lines.append(f"  {r.get('time', '')}  {who}  {r.get('action')}  {r.get('detail')}")
        yield self._plain(event, "\n".join(lines))

    @filter.command("积分帮助")
    async def cmd_help(self, event: AstrMessageEvent):
        yield self._plain(
            event,
            "积分系统指令：\n"
            "/签到         每日签到领取积分\n"
            "/积分         查看我的积分与历史记录\n"
            "/任务         查看今日任务进度\n"
            "/领取任务 签到|发言|抽奖   领取任务奖励\n"
            "/成就         查看成就列表\n"
            "/排行榜 [N]   查看积分排行榜\n"
            "/周榜 /月榜   本周期新增积分榜\n"
            "/抽奖         花费积分抽奖\n"
            "/商店         查看商店商品\n"
            "/兑换 <编号> [数量]  使用积分兑换\n"
            "/我的背包     查看已兑换的物品\n"
            "/赠送 @群友 数量    把积分转给群友\n"
            "管理员指令：\n"
            "/加积分 /扣积分 @用户 数量\n"
            "/补签到 @用户\n"
            "/重置用户 用户ID\n"
            "/待核销 · /核销 <兑换码>\n"
            "/赛季结算 周|月\n"
            "/审计日志\n"
            "管理员可在插件页面配置奖励、抽奖、任务与赛季。")

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
        register(f"/{PLUGIN_NAME}/prizes", self.api_prizes, ["GET", "POST"], "奖池读写")
        register(f"/{PLUGIN_NAME}/prizes/<prize_id>/delete", self.api_prize_delete,
                 ["POST"], "删除奖池项")
        register(f"/{PLUGIN_NAME}/redemptions", self.api_redemptions, ["GET"], "核销列表")
        register(f"/{PLUGIN_NAME}/redemptions/<rid>/confirm", self.api_redemption_confirm,
                 ["POST"], "确认核销")
        register(f"/{PLUGIN_NAME}/audit", self.api_audit, ["GET"], "审计日志")
        register(f"/{PLUGIN_NAME}/export", self.api_export, ["GET"], "导出数据")
        register(f"/{PLUGIN_NAME}/settle", self.api_settle, ["POST"], "结算赛季榜")
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
        today = self.store._today().isoformat()
        today_signins = sum(1 for u in users.values() if u.get("last_signin") == today)
        return json_response({
            "total_users": len(users),
            "total_points": sum(u["points"] for u in users.values()),
            "today_signins": today_signins,
            "product_count": len(products),
            "pending_redemptions": len(self.store.pending_redemptions()),
            "seasons_count": len(self.store.data.get("seasons") or []),
            "audit_tail": (self.store.data.get("audit_log") or [])[-10:][::-1],
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
        self.store.add_audit(request.username, "", "面板调整积分",
                             f"为 {name or user_id} {delta:+d}")
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

    async def api_prizes(self):
        err = self._guard()
        if err:
            return err
        if request.method == "POST":
            body = await request.json(default={}) or {}
            saved = await self.store.upsert_prize(body)
            if saved is None:
                return error_response("奖池 id 不能为空")
            return json_response({"saved": saved})
        return json_response({"prizes": self.store.data.get("prizes", [])})

    async def api_prize_delete(self, prize_id: str):
        err = self._guard()
        if err:
            return err
        deleted = await self.store.delete_prize(prize_id)
        if not deleted:
            return error_response("奖池项不存在")
        return json_response({"deleted": deleted})

    async def api_redemptions(self):
        err = self._guard()
        if err:
            return err
        rows = list(reversed(self.store.data.get("redemptions") or []))
        return json_response({"rows": rows[:200]})

    async def api_redemption_confirm(self, rid: str):
        err = self._guard()
        if err:
            return err
        async with self.store.lock:
            row = self.store.confirm_redemption(str(rid).strip().upper())
        if not row:
            return error_response("兑换码不存在")
        self.store.add_audit(request.username, "", "面板核销",
                             f"{row['id']} {row['name']} {row['product_name']}")
        return json_response({"confirmed": row})

    async def api_audit(self):
        err = self._guard()
        if err:
            return err
        return json_response({"rows": list(reversed(
            self.store.data.get("audit_log") or []))[:100]})

    async def api_export(self):
        err = self._guard()
        if err:
            return err
        return json_response({"data": self.store.data,
                              "exported_at": self.store._now().isoformat(timespec="seconds")})

    async def api_settle(self):
        err = self._guard()
        if err:
            return err
        body = await request.json(default={}) or {}
        period = str(body.get("period", "week"))
        if period not in ("week", "month"):
            return error_response("period 必须是 week 或 month")
        result = await self.store.settle_season(period, body.get("group_id") or None)
        self.store.add_audit(request.username, "", "面板赛季结算", period)
        return json_response(result)

    async def api_settings(self):
        err = self._guard()
        if err:
            return err
        if request.method == "POST":
            body = await request.json(default={}) or {}
            saved = await self.store.update_settings(body)
            return json_response({"settings": saved})
        return json_response({"settings": dict(self.store.data["settings"])})

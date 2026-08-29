"""图片卡片渲染模块。

用 Pillow 渲染"积分排行榜""我的积分""积分商店"图片卡片，供群聊发送。
依赖可选的 Pillow 库；Pillow 未安装或找不到中文字体时，所有渲染方法
返回 None，调用方会自动回退为纯文本输出，保证插件在弱环境下仍可用。

文件名带随机后缀，多群并发渲染不会互相覆盖；超过 15 分钟的旧图会被
自动清理。
"""

import os
import time
import uuid

from astrbot.api import logger

# 常见中文字体候选路径（Windows / macOS / Linux）
_FONT_CANDIDATES = [
    "C:/Windows/Fonts/msyh.ttc",
    "C:/Windows/Fonts/msyh.ttf",
    "C:/Windows/Fonts/simhei.ttf",
    "C:/Windows/Fonts/simsun.ttc",
    "/System/Library/Fonts/PingFang.ttc",
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
]

_BG = (245, 246, 248)      # 页面底色
_CARD = (255, 255, 255)    # 卡片白
_INK = (33, 37, 43)        # 主文字
_MUTED = (130, 138, 150)   # 次要文字
_LINE = (228, 231, 236)    # 分隔线
_ACCENT = (76, 111, 189)   # 强调（条形/数字）
_ACCENT_2 = (94, 164, 131) # 次级强调（正值）
_RED = (209, 90, 90)       # 负值

_IMG_TTL = 900  # 旧图保留 15 分钟


class CardRenderer:
    def __init__(self, img_dir: str):
        self.img_dir = img_dir
        self.available = False
        self._font_path = None  # 用户通过设置指定的字体
        try:
            from PIL import Image, ImageDraw, ImageFont
            self.Image, self.ImageDraw, self.ImageFont = (
                Image, ImageDraw, ImageFont)
            self.available = True
        except Exception as exc:
            logger.warning(f"[积分系统] 未安装 Pillow，图片输出不可用：{exc}")

    def set_font(self, path: str = ""):
        self._font_path = (path or "").strip() or None

    def _resolve_font(self):
        if self._font_path and os.path.exists(self._font_path):
            return self._font_path
        for path in _FONT_CANDIDATES:
            if os.path.exists(path):
                return path
        return None

    def _new_canvas(self, width, height):
        return self.Image.new("RGB", (width, height), _BG)

    def _text_color(self, idx):
        """前 3 名用强调色，其余常规。"""
        return (_ACCENT_2, _ACCENT, _INK)[idx] if idx < 3 else _INK

    def _draw_ranking_rows(self, draw, font_md, font_sm, rows, top, start_y, max_points):
        y = start_y
        for i, u in enumerate(rows[:top]):
            name = (u.get("name") or "未知用户")[:12]
            pts = int(u.get("points", 0))
            bar = 0 if max_points <= 0 else int(pts / max_points * 480)
            draw.text((40, y), f"{i + 1}", font=font_md,
                      fill=self._text_color(i))
            draw.text((84, y), name, font=font_md, fill=_INK)
            draw.text((560, y), str(pts), font=font_md, fill=_INK,
                      anchor="rm")
            draw.text((570, y + 2), "积分", font=font_sm, fill=_MUTED)
            if bar > 0:
                y_ln = y + 34
                bar_color = self._text_color(i) if i < 3 else (199, 206, 218)
                draw.rectangle([86, y_ln, 86 + bar, y_ln + 6], fill=bar_color)
            y += 52
        return y

    def render_ranking(self, rows, subtitle=""):
        """渲染积分排行榜，返回图片路径；失败返回 None。"""
        if not self.available:
            return None
        font_path = self._resolve_font()
        if not font_path:
            logger.debug("[积分系统] 未找到中文字体，排行榜改用文本输出")
            return None
        md = self.ImageFont.truetype(font_path, 26)
        sm = self.ImageFont.truetype(font_path, 15)
        title_font = self.ImageFont.truetype(font_path, 30)

        top = min(len(rows), 20)
        width, height = 640, 150 + top * 52
        img = self._new_canvas(width, height)
        draw = self.ImageDraw.Draw(img)

        draw.rectangle([0, 0, width, 150], fill=_CARD)
        draw.text((40, 34), "积分排行榜", font=title_font, fill=_INK)
        if subtitle:
            draw.text((40, 80), subtitle, font=sm, fill=_MUTED)
        draw.line([0, 146, width, 146], fill=_LINE, width=2)

        max_points = max((int(u.get("points", 0)) for u in rows[:top]), default=1)
        self._draw_ranking_rows(draw, md, sm, rows, top, 170, max_points)
        return self._save(img, "ranking")

    def render_profile(self, user, subtitle=""):
        """渲染个人积分卡，返回图片路径；失败返回 None。"""
        if not self.available:
            return None
        font_path = self._resolve_font()
        if not font_path:
            logger.debug("[积分系统] 未找到中文字体，积分卡改用文本输出")
            return None
        md = self.ImageFont.truetype(font_path, 26)
        sm = self.ImageFont.truetype(font_path, 15)
        big = self.ImageFont.truetype(font_path, 54)
        title_font = self.ImageFont.truetype(font_path, 30)

        recent = (user.get("history") or [])[-4:]
        # 统计区 2x2 网格，避免第四列贴右缘被裁切；高度按流水行数动态扩展
        stats_top = 238
        if recent:
            height = stats_top + 128 + 14 + len(recent) * 26 + 26
        else:
            height = stats_top + 118
        width = 640
        img = self._new_canvas(width, height)
        draw = self.ImageDraw.Draw(img)

        # 顶部积分区
        draw.rectangle([0, 0, width, 210], fill=_ACCENT)
        draw.text((40, 34), user.get("name", "我")[:12] + " 的积分",
                  font=title_font, fill=(255, 255, 255))
        if subtitle:
            draw.text((40, 80), subtitle, font=sm, fill=(225, 230, 242))
        pts = int(user.get("points", 0))
        draw.text((40, 100), str(pts), font=big, fill=(255, 255, 255))
        draw.text((40, 168), "积分", font=sm, fill=(225, 230, 242))

        # 统计 2x2
        stats = [
            ("累计获得", f"{user.get('total_earned', 0)}"),
            ("累计花费", f"{user.get('total_spent', 0)}"),
            ("连续签到", f"{user.get('streak', 0)} 天"),
            ("累计签到", f"{user.get('signin_count', 0)} 次"),
        ]
        for i, (label, value) in enumerate(stats):
            x = 40 + (i % 2) * 300
            y = stats_top + (i // 2) * 58
            draw.text((x, y), label, font=sm, fill=_MUTED)
            draw.text((x, y + 20), value, font=md, fill=_INK)

        if recent:
            yy = stats_top + 128
            draw.line([40, yy - 14, width - 40, yy - 14], fill=_LINE, width=1)
            for row in reversed(recent):
                delta = row["delta"]
                color = _ACCENT_2 if delta > 0 else (_RED if delta < 0 else _MUTED)
                sign = "+" if delta > 0 else ""
                draw.text((40, yy),
                          f"{row.get('time','')}  {sign}{delta}  {row.get('reason','')}",
                          font=sm, fill=color)
                yy += 26
        return self._save(img, "profile")

    def render_shop(self, products, subtitle="兑换方式：/兑换 编号 数量"):
        """渲染积分商店卡片，返回图片路径；失败返回 None。"""
        if not self.available:
            return None
        font_path = self._resolve_font()
        if not font_path:
            logger.debug("[积分系统] 未找到中文字体，商店改用文本输出")
            return None
        md = self.ImageFont.truetype(font_path, 24)
        sm = self.ImageFont.truetype(font_path, 15)
        title_font = self.ImageFont.truetype(font_path, 30)

        rows = products[:16]  # 图片最多展示 16 个，避免过长
        row_h = 84
        width, height = 640, 150 + len(rows) * row_h + 20
        img = self._new_canvas(width, height)
        draw = self.ImageDraw.Draw(img)

        draw.rectangle([0, 0, width, 150], fill=_CARD)
        draw.text((40, 34), "积分商店", font=title_font, fill=_INK)
        if subtitle:
            draw.text((40, 84), subtitle, font=sm, fill=_MUTED)
        draw.line([0, 146, width, 146], fill=_LINE, width=2)

        y = 170
        for p in rows:
            stock = p.get("stock")
            stock_txt = "不限量" if stock in (None, -1, "") else f"剩 {stock} 件"
            verify = " · 需核销" if p.get("need_verify") else ""
            icon = (p.get("icon") or "🛒") + " "
            draw.text((40, y), icon + str(p.get("name", ""))[:14],
                      font=md, fill=_INK)
            draw.text((560, y + 4), f"{p.get('cost', 0)} 积分", font=md,
                      fill=_ACCENT, anchor="rm")
            draw.text((40, y + 36), f"编号 {p.get('id', '')} · {stock_txt}{verify}",
                      font=sm, fill=_MUTED)
            desc = (p.get("desc") or "无描述")
            if len(desc) > 30:
                desc = desc[:30] + "…"
            draw.text((40, y + 58), desc, font=sm, fill=_MUTED)
            y += row_h
            if y < height - 12:
                draw.line([40, y - 14, width - 40, y - 14], fill=_LINE, width=1)
        if len(products) > 16:
            draw.text((40, y), f"…还有 {len(products) - 16} 个商品未展示",
                      font=sm, fill=_MUTED)
        return self._save(img, "shop")

    def _save(self, img, base):
        try:
            d = self.img_dir or "."
            os.makedirs(d, exist_ok=True)
            # 唯一文件名：多群并发渲染不会互相覆盖
            path = os.path.join(d, f"{base}_{uuid.uuid4().hex[:8]}.png")
            img.save(path)
            self._cleanup(d, base)
            return path
        except Exception as exc:
            logger.error(f"[积分系统] 生成图片失败：{exc}")
            return None

    @staticmethod
    def _cleanup(d, base):
        """清理过期旧图，避免目录无限膨胀。"""
        try:
            now = time.time()
            for f in os.listdir(d):
                if f.startswith(base + "_") and f.endswith(".png"):
                    p = os.path.join(d, f)
                    if now - os.path.getmtime(p) > _IMG_TTL:
                        os.remove(p)
        except Exception:
            pass

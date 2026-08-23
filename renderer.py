"""图片卡片渲染模块。

用 Pillow 渲染"积分排行榜"与"我的积分"图片卡片，供群聊发送。
依赖可选的 Pillow 库；Pillow 未安装或找不到中文字体时，所有渲染方法
返回 None，调用方会自动回退为纯文本输出，保证插件在弱环境下仍可用。
"""

import os

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
        return self._save(img, "ranking.png")

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
        height = 380 + len(recent) * 24 + 16
        width, height = 640, height
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

        # 下方统计
        y = 240
        stats = [
            ("累计获得", f"{user.get('total_earned', 0)} 积分"),
            ("累计花费", f"{user.get('total_spent', 0)} 积分"),
            ("连续签到", f"{user.get('streak', 0)} 天"),
            ("累计签到", f"{user.get('signin_count', 0)} 次"),
        ]
        col_x = [40, 220, 400, 560]
        for (label, value), x in zip(stats, col_x):
            draw.text((x, y - 6), label, font=sm, fill=_MUTED)
            draw.text((x, y + 18), str(value), font=md, fill=_INK)

        if recent:
            yy = y + 74
            draw.line([40, yy - 14, width - 40, yy - 14], fill=_LINE, width=1)
            for row in reversed(recent):
                delta = row["delta"]
                color = _ACCENT_2 if delta > 0 else (_RED if delta < 0 else _MUTED)
                sign = "+" if delta > 0 else ""
                draw.text((40, yy),
                          f"{row.get('time','')}  {sign}{delta}  {row.get('reason','')}",
                          font=sm, fill=color)
                yy += 24
        return self._save(img, "profile.png")

    def _save(self, img, name):
        try:
            self.img_dir = self.img_dir or "."
            os.makedirs(self.img_dir, exist_ok=True)
            path = os.path.join(self.img_dir, name)
            img.save(path)
            return path
        except Exception as exc:
            logger.error(f"[积分系统] 生成图片失败：{exc}")
            return None
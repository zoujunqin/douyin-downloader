"""
human.py - 模拟真人操作鼠标键盘的工具模块

根据小红书后台行为检测维度设计，覆盖三大类检测：

一、鼠标行为：
  - 移动轨迹（贝塞尔曲线 + 折线角度分布）
  - 移动速度（缓入缓出 + 速度方差 + 峰值分布）
  - 点击精度（落点偏移，非正中心）
  - 点击时长（mousedown→mouseup 间隔）
  - 点击间隔（两次点击之间的时间分布与方差）
  - 双击行为（双击间隔 + 误触概率）
  - 悬停行为（hover 停留时长 + 位置分布）
  - 滚动方式（分段 delta + 加速度曲线）
  - 滚动节奏（分段停顿 + 阅读停留点）
  - 鼠标空闲（无操作时微抖动）

二、键盘行为：
  - 击键间隔 IKI（正态分布）
  - 按键时长（keydown→keyup）
  - 输入节奏指纹（常见字符组合加速）
  - 退格频率（偶尔打错再删除）
  - 粘贴行为（Ctrl+V 前有自然停顿）
  - 快捷键使用（带随机前后停顿）

三、页面交互行为：
  - 页面停留时长
  - 阅读深度（滚动百分比 + 分段停留）
  - 视口注视点（鼠标跟随阅读位置）
  - 表单填写顺序（字段间停顿）
  - 下拉框操作（打开→选择延迟）
  - 图片浏览（在图片区域停留）
  - 链接悬停（到达链接前的轨迹）

用法：
    from human import HumanInput

    hi = HumanInput()
    hi.click(500, 300)
    hi.type_text("hello world")
    hi.read_scroll(screen_w, screen_h, total_scrolls=10)
    hi.idle_jitter(duration=3.0)
    hi.browse_page(screen_w, screen_h)
"""

import time
import math
import random
import pyautogui


class HumanInput:
    """模拟真人鼠标键盘及页面交互操作"""

    def __init__(
        self,
        move_duration_range=(0.6, 1.2),
        click_pause_range=(0.15, 0.4),
        click_hold_range=(0.04, 0.12),
        type_interval_range=(0.10, 0.36),
        key_hold_range=(0.05, 0.13),
    ):
        """
        参数：
            move_duration_range: 鼠标移动总时长范围（秒）
            click_pause_range:   到达目标后停顿范围（秒），模拟人眼确认
            click_hold_range:    鼠标按下到松开的时长范围（秒）
            type_interval_range: 打字时每个字符之间的间隔范围（秒）
            key_hold_range:      单个按键 keydown→keyup 的持续时长范围（秒）
        """
        self.move_duration_range = move_duration_range
        self.click_pause_range = click_pause_range
        self.click_hold_range = click_hold_range
        self.type_interval_range = type_interval_range
        self.key_hold_range = key_hold_range

        # 记录上次点击时间，用于生成自然的点击间隔
        self._last_click_time = 0.0

    # ═══════════════════════════════════════════════
    #  一、鼠标行为
    # ═══════════════════════════════════════════════

    # ─────────────── 贝塞尔曲线基础 ───────────────

    @staticmethod
    def _bezier_point(t, p0, p1, p2, p3):
        """三阶贝塞尔曲线上 t 处的点"""
        u = 1 - t
        return (
            u**3 * p0[0] + 3 * u**2 * t * p1[0] + 3 * u * t**2 * p2[0] + t**3 * p3[0],
            u**3 * p0[1] + 3 * u**2 * t * p1[1] + 3 * u * t**2 * p2[1] + t**3 * p3[1],
        )

    @staticmethod
    def _generate_control_points(start, end):
        """根据起止点生成两个随机控制点，让曲线自然弯曲"""
        dx = end[0] - start[0]
        dy = end[1] - start[1]
        dist = math.hypot(dx, dy)
        offset = max(50, dist * random.uniform(0.2, 0.45))

        cp1 = (
            start[0] + dx * random.uniform(0.2, 0.4) + random.uniform(-offset, offset) * 0.5,
            start[1] + dy * random.uniform(0.2, 0.4) + random.uniform(-offset, offset) * 0.5,
        )
        cp2 = (
            start[0] + dx * random.uniform(0.6, 0.8) + random.uniform(-offset, offset) * 0.5,
            start[1] + dy * random.uniform(0.6, 0.8) + random.uniform(-offset, offset) * 0.5,
        )
        return cp1, cp2

    @staticmethod
    def _ease_in_out(t):
        """缓入缓出：开头和结尾慢，中间快（模拟真人手部加减速）"""
        if t < 0.5:
            return 4 * t * t * t
        else:
            return 1 - (-2 * t + 2) ** 3 / 2

    @staticmethod
    def _click_offset(radius=5):
        """
        【点击精度】生成一个随机偏移量，模拟人点击时不会精确命中元素中心。
        偏移服从二维正态分布，大多数落在半径 radius 像素内。
        """
        ox = random.gauss(0, radius / 2)
        oy = random.gauss(0, radius / 2)
        return int(ox), int(oy)

    # ─────────────── 鼠标移动 ───────────────

    def move_to(self, x, y, duration_range=None):
        """
        【移动轨迹 + 移动速度】仿真人鼠标移动到 (x, y)
        - 贝塞尔曲线路径（路径曲率、折线角度自然分布）
        - 缓入缓出速度曲线（加减速、速度方差、峰值分布）
        - 微小随机抖动，越接近终点越小
        """
        if duration_range is None:
            duration_range = self.move_duration_range

        start = pyautogui.position()
        end = (x, y)
        cp1, cp2 = self._generate_control_points(start, end)

        duration = random.uniform(*duration_range)
        steps = max(40, int(duration * 120))  # ~120 fps 采样

        prev_time = time.perf_counter()
        for i in range(1, steps + 1):
            t = self._ease_in_out(i / steps)
            bx, by = self._bezier_point(t, start, cp1, cp2, end)

            # 微抖动，越接近终点越小
            progress = i / steps
            jitter = max(0, (1 - progress)) * random.gauss(0, 1.5)
            bx += jitter
            by += jitter

            pyautogui.moveTo(int(bx), int(by), _pause=False)

            target_dt = duration / steps
            elapsed = time.perf_counter() - prev_time
            if elapsed < target_dt:
                time.sleep(target_dt - elapsed)
            prev_time = time.perf_counter()

        # 精确落点
        pyautogui.moveTo(x, y, _pause=False)

    # ─────────────── 鼠标点击 ───────────────

    def _natural_click_interval(self):
        """
        【点击间隔】确保两次点击之间有自然的时间间隔。
        间隔服从对数正态分布，均值约 0.8 秒。
        """
        now = time.perf_counter()
        elapsed = now - self._last_click_time
        min_interval = random.lognormvariate(-0.3, 0.4)  # 约 0.5~1.5 秒
        min_interval = max(0.2, min(min_interval, 2.0))
        if elapsed < min_interval:
            time.sleep(min_interval - elapsed)

    def click(self, x, y, offset_radius=5):
        """
        【点击精度 + 点击时长 + 点击间隔】仿真人单击
        - 落点有随机偏移（非正中心）
        - mousedown→mouseup 间隔随机
        - 两次点击间有自然间隔
        """
        self._natural_click_interval()
        ox, oy = self._click_offset(offset_radius)
        self.move_to(x + ox, y + oy)
        time.sleep(random.uniform(*self.click_pause_range))
        pyautogui.mouseDown()
        time.sleep(random.uniform(*self.click_hold_range))
        pyautogui.mouseUp()
        self._last_click_time = time.perf_counter()

    def double_click(self, x, y, offset_radius=5):
        """
        【双击行为】仿真人双击
        - 双击间隔 40~120ms（真人范围）
        - 约 3% 概率产生轻微误触（第二次落点偏移更大）
        """
        self._natural_click_interval()
        ox, oy = self._click_offset(offset_radius)
        self.move_to(x + ox, y + oy)
        time.sleep(random.uniform(*self.click_pause_range))

        # 第一次点击
        pyautogui.mouseDown()
        time.sleep(random.uniform(0.03, 0.08))
        pyautogui.mouseUp()

        # 双击间隔
        time.sleep(random.uniform(0.04, 0.12))

        # 第二次点击（偶尔轻微偏移，模拟误触）
        if random.random() < 0.03:
            ox2, oy2 = self._click_offset(offset_radius * 2)
            pyautogui.moveTo(x + ox2, y + oy2, _pause=False)

        pyautogui.mouseDown()
        time.sleep(random.uniform(0.03, 0.08))
        pyautogui.mouseUp()
        self._last_click_time = time.perf_counter()

    def right_click(self, x, y, offset_radius=5):
        """仿真人右键单击"""
        self._natural_click_interval()
        ox, oy = self._click_offset(offset_radius)
        self.move_to(x + ox, y + oy)
        time.sleep(random.uniform(*self.click_pause_range))
        pyautogui.mouseDown(button="right")
        time.sleep(random.uniform(*self.click_hold_range))
        pyautogui.mouseUp(button="right")
        self._last_click_time = time.perf_counter()

    # ─────────────── 悬停行为 ───────────────

    def hover(self, x, y, hover_duration_range=(0.5, 2.0), offset_radius=5):
        """
        【悬停行为】移动到目标并悬停一段时间，模拟阅读/查看。
        悬停期间有极微小抖动（真人手不会完全静止）。
        """
        ox, oy = self._click_offset(offset_radius)
        self.move_to(x + ox, y + oy)

        hover_time = random.uniform(*hover_duration_range)
        end_time = time.perf_counter() + hover_time
        while time.perf_counter() < end_time:
            # 极微小抖动（1~2px 范围）
            jx = random.gauss(0, 0.8)
            jy = random.gauss(0, 0.8)
            cx, cy = pyautogui.position()
            pyautogui.moveTo(int(cx + jx), int(cy + jy), _pause=False)
            time.sleep(random.uniform(0.1, 0.3))

    # ─────────────── 滚动 ───────────────

    def scroll(self, clicks, x=None, y=None):
        """
        【滚动方式 + 滚动节奏】仿真人滚动滚轮。
        - 分段小幅滚动（delta 值随机）
        - 滚动加速度曲线：开始慢 → 中间快 → 结束慢
        - 分段停顿，模拟阅读停留点
        """
        if x is not None and y is not None:
            self.move_to(x, y, duration_range=(0.3, 0.6))
            time.sleep(random.uniform(0.1, 0.3))

        direction = 1 if clicks > 0 else -1
        remaining = abs(clicks)
        total = remaining
        scrolled = 0

        while remaining > 0:
            # 加速度曲线：开头和结尾步长小，中间步长大，每步随机变化
            progress = scrolled / total if total > 0 else 0.5
            if progress < 0.2 or progress > 0.8:
                step = min(remaining, random.randint(90, 360))
            else:
                step = min(remaining, random.randint(180, 720))

            pyautogui.scroll(step * direction)
            remaining -= step
            scrolled += step

            # 滚动间隔：偶尔长停顿（模拟阅读停留点）
            if random.random() < 0.15:
                time.sleep(random.uniform(0.5, 1.5))  # 阅读停留
            else:
                time.sleep(random.uniform(0.03, 0.15))

    def read_scroll(self, screen_width, screen_height, total_scrolls=None,
                    area=(0.12, 0.95, 0.22, 0.78)):
        """
        【阅读深度 + 滚动节奏】模拟真人阅读式滚动。
        向下滚动，期间鼠标跟随内容区域移动，偶尔停下来"阅读"。
        """
        if total_scrolls is None:
            total_scrolls = random.randint(5, 15)

        left = int(screen_width * area[0])
        right = int(screen_width * area[1])
        top = int(screen_height * area[2])
        bottom = int(screen_height * area[3])

        for i in range(total_scrolls):
            # 鼠标在内容区域随机位置（模拟视口注视点）
            rx = random.randint(left, right)
            ry = random.randint(top, bottom)
            self.move_to(rx, ry, duration_range=(0.2, 0.5))

            # 每次滚动量随机变化，不固定
            scroll_amount = random.randint(20, 120)
            pyautogui.scroll(-scroll_amount)  # 向下滚

            # 阅读停留（越到后面停留越短，模拟越看越快）
            base_pause = max(0.3, 1.5 - i * 0.08)
            time.sleep(random.uniform(base_pause * 0.5, base_pause * 1.5))

    # ─────────────── 鼠标空闲 ───────────────

    def idle_jitter(self, duration=3.0):
        """
        【鼠标空闲】无操作期间产生微抖动，模拟手放在鼠标上的自然晃动。
        真人即使不操作，鼠标也不会绝对静止。
        """
        end_time = time.perf_counter() + duration
        while time.perf_counter() < end_time:
            cx, cy = pyautogui.position()
            jx = random.gauss(0, 0.3)
            jy = random.gauss(0, 0.3)
            nx = max(0, int(cx + jx))
            ny = max(0, int(cy + jy))
            pyautogui.moveTo(nx, ny, _pause=False)
            time.sleep(random.uniform(0.8, 2.5))

    # ═══════════════════════════════════════════════
    #  二、键盘行为
    # ═══════════════════════════════════════════════

    def _iki_delay(self, prev_char, curr_char):
        """
        【击键间隔 IKI + 输入节奏指纹】
        根据字符对计算击键间隔：
        - 常见字符组合（ing, the, ion 等）间隔更短（肌肉记忆）
        - 其他字符间隔服从正态分布
        """
        fast_combos = {
            "th", "he", "in", "ng", "er", "an", "re", "on", "en", "at",
            "ed", "nd", "to", "it", "is", "or", "ar", "te", "al", "ou",
            "io", "le", "se", "st", "ti",
        }
        pair = (prev_char + curr_char).lower()

        if pair in fast_combos:
            # 常用组合更快
            delay = max(0.03, random.gauss(0.07, 0.02))
        else:
            # 一般字符，正态分布
            base = random.uniform(*self.type_interval_range)
            delay = max(0.03, random.gauss(base, base * 0.25))

        return delay

    def _key_press_with_duration(self, key):
        """
        【按键时长】模拟 keydown→keyup 的真实持续时间。
        """
        pyautogui.keyDown(key)
        time.sleep(random.uniform(*self.key_hold_range))
        pyautogui.keyUp(key)

    def type_text(self, text, interval_range=None, typo_rate=0.02):
        """
        【击键间隔 + 按键时长 + 输入节奏指纹 + 退格频率】
        仿真人打字：
        - 随机 1-3 个字符为一组输入（中文使用剪贴板粘贴）
        - 每组之间的间隔随机快慢，模拟真人节奏波动
        - 偶尔打错再退格删除（typo_rate 控制概率）
        - 偶尔随机删除已输入的 1-2 个字再重新打（模拟修改措辞）
        """
        if interval_range is None:
            interval_range = self.type_interval_range

        import pyperclip

        # 将文本按随机 1-2 个字符分组
        chunks = []
        pos = 0
        while pos < len(text):
            chunk_size = random.randint(1, 2)
            chunks.append(text[pos:pos + chunk_size])
            pos += chunk_size

        # 随机选一个速度模式（整段打字过程中会切换 2-4 次）
        # fast: 打字飞快  normal: 正常  slow: 犹豫/思考
        speed_modes = ["fast", "normal", "slow"]
        current_speed = random.choice(speed_modes)
        switch_interval = random.randint(2, 5)  # 每隔几组切换一次速度

        typed_len = 0  # 已输入的字符总数（用于随机删除判断）

        for chunk_idx, chunk in enumerate(chunks):
            # 每隔几组随机切换速度模式
            if chunk_idx > 0 and chunk_idx % switch_interval == 0:
                current_speed = random.choice(speed_modes)
                switch_interval = random.randint(2, 5)

            # 判断这一组是否触发打错（概率约 typo_rate），位置随机
            will_typo = typed_len > 0 and random.random() < typo_rate

            # 判断这一组是否全是非 ASCII（中文等）
            has_non_ascii = any(ord(c) > 127 for c in chunk)

            if will_typo:
                # 在这组字符的随机位置插入打错：
                # 先正确输入前半部分 → 打错 1-3 个字符 → 发现 → 删除 → 重新打后半部分
                typo_pos = random.randint(0, len(chunk))  # 打错发生在第几个字符之后
                before = chunk[:typo_pos]
                after = chunk[typo_pos:]

                # 输入前半部分（正确）
                if before:
                    if any(ord(c) > 127 for c in before):
                        pyperclip.copy(before)
                        pyautogui.hotkey("ctrl", "v")
                    else:
                        for ci, char in enumerate(before):
                            pyautogui.write(char, interval=0)
                            if ci < len(before) - 1:
                                time.sleep(random.uniform(0.04, 0.16))
                    time.sleep(random.uniform(0.04, 0.12))

                # 打出 1-3 个错误字符
                wrong_count = random.randint(1, 3)
                wrong_chars = ''.join(
                    random.choice("abcdefghijklmnopqrstuvwxyz1234567890")
                    for _ in range(wrong_count)
                )
                pyperclip.copy(wrong_chars)
                pyautogui.hotkey("ctrl", "v")

                # 短暂停顿后发现打错
                time.sleep(random.uniform(0.3, 0.8))

                # 删除错误字符
                for _ in range(wrong_count):
                    pyautogui.press("backspace")
                    time.sleep(random.uniform(0.05, 0.15))
                time.sleep(random.uniform(0.15, 0.4))

                # 重新输入后半部分（正确）
                if after:
                    if any(ord(c) > 127 for c in after):
                        pyperclip.copy(after)
                        pyautogui.hotkey("ctrl", "v")
                    else:
                        for ci, char in enumerate(after):
                            pyautogui.write(char, interval=0)
                            if ci < len(after) - 1:
                                time.sleep(random.uniform(0.04, 0.16))

            elif has_non_ascii:
                # 包含中文字符，整组用剪贴板粘贴
                pyperclip.copy(chunk)
                pyautogui.hotkey("ctrl", "v")
            else:
                # 纯 ASCII，逐字符输入（保留字符对间隔）
                for ci, char in enumerate(chunk):
                    pyautogui.write(char, interval=0)
                    if ci < len(chunk) - 1:
                        # 同组内字符间隔很短
                        time.sleep(random.uniform(0.04, 0.16))

            typed_len += len(chunk)

            # 组间间隔：根据当前速度模式决定
            if chunk_idx < len(chunks) - 1:
                if current_speed == "fast":
                    delay = max(0.06, random.gauss(0.12, 0.04))
                elif current_speed == "slow":
                    delay = max(0.30, random.gauss(1.0, 0.30))
                else:  # normal
                    base = random.uniform(*interval_range) * 2
                    delay = max(0.10, random.gauss(base, base * 0.25))

                # 偶尔在组间加一个较长的思考停顿（模拟想下一个词）
                if random.random() < 0.08:
                    delay += random.uniform(0.5, 1.5)

                time.sleep(delay)

    def paste_text(self, text):
        """
        【粘贴行为】模拟真人粘贴：先停顿思考，再 Ctrl+V。
        粘贴前后都有自然延迟。
        """
        import pyperclip
        pyperclip.copy(text)
        # 粘贴前停顿（模拟人在看剪贴板内容/思考）
        time.sleep(random.uniform(0.3, 0.8))
        pyautogui.hotkey("ctrl", "v")
        # 粘贴后短暂确认
        time.sleep(random.uniform(0.2, 0.5))

    def hotkey(self, *keys):
        """
        【快捷键使用】按下组合键，前后带自然停顿。
        """
        time.sleep(random.uniform(0.05, 0.15))
        pyautogui.hotkey(*keys)
        time.sleep(random.uniform(0.05, 0.15))

    def press(self, key):
        """
        【按键时长】按下单个键，带 keydown→keyup 时长。
        """
        self._key_press_with_duration(key)

    # ═══════════════════════════════════════════════
    #  三、页面交互行为
    # ═══════════════════════════════════════════════

    def browse_page(self, screen_width, screen_height,
                    area=(0.12, 0.95, 0.22, 0.78)):
        """
        【页面停留 + 阅读深度 + 视口注视点 + 图片浏览】
        模拟完整的真人浏览页面行为：
        1. 先在页面上随机移动鼠标（浏览）
        2. 然后阅读式滚动
        3. 期间穿插空闲微抖动
        """
        # 阶段1：随机浏览（鼠标漫游）
        self.wander(screen_width, screen_height,
                    count=random.randint(2, 4), area=area)

        # 阶段2：偶尔空闲停留
        if random.random() < 0.5:
            self.idle_jitter(duration=random.uniform(1.0, 3.0))

        # 阶段3：阅读式滚动
        if random.random() < 0.6:
            self.read_scroll(screen_width, screen_height,
                             total_scrolls=random.randint(3, 8), area=area)

    def wander(self, screen_width, screen_height, count=None,
               area=(0.12, 0.95, 0.22, 0.78)):
        """
        【视口注视点 + 图片浏览】
        在屏幕指定区域内随机移动鼠标若干次，模拟真人浏览。
        偶尔在某个位置悬停更久（模拟看图片/内容）。
        """
        if count is None:
            count = random.randint(2, 5)

        left = int(screen_width * area[0])
        right = int(screen_width * area[1])
        top = int(screen_height * area[2])
        bottom = int(screen_height * area[3])

        for i in range(count):
            rx = random.randint(left, right)
            ry = random.randint(top, bottom)
            self.move_to(rx, ry, duration_range=(0.4, 0.9))

            # 30% 概率长时间停留（看图片/详细内容）
            if random.random() < 0.3:
                self.hover(rx, ry, hover_duration_range=(1.0, 3.0), offset_radius=3)
            else:
                time.sleep(random.uniform(0.3, 1.2))

    def hover_link(self, x, y):
        """
        【链接悬停】模拟鼠标接近链接前的自然轨迹。
        先移到链接附近（不是直接精确到达），然后慢慢调整到链接上。
        """
        # 先移到目标附近（偏移 20~50px）
        near_x = x + random.randint(-50, 50)
        near_y = y + random.randint(-30, 30)
        self.move_to(near_x, near_y, duration_range=(0.3, 0.7))
        time.sleep(random.uniform(0.1, 0.3))

        # 然后精确移到链接上（短距离，慢速）
        ox, oy = self._click_offset(3)
        self.move_to(x + ox, y + oy, duration_range=(0.15, 0.35))

        # 悬停一下（阅读链接文字）
        time.sleep(random.uniform(0.3, 1.0))

    def switch_away_and_back(self, away_duration_range=(3.0, 15.0)):
        """
        【Tab切换频率】模拟真人切换到其他窗口（如看微信、查资料），
        停留随机时间后再切回来。

        流程：
        1. Alt+Tab 切走
        2. 在其他窗口停留随机时间（期间有微抖动，模拟在操作）
        3. Alt+Tab 切回原窗口

        参数：
            away_duration_range: 离开时长范围（秒），默认 3~15 秒
        """
        print("[模拟] 切换到其他窗口...")
        # 切走前短暂停顿（人不会瞬间切换）
        time.sleep(random.uniform(0.2, 0.5))
        pyautogui.hotkey("alt", "tab")
        time.sleep(random.uniform(0.5, 1.0))  # 等窗口切换动画

        # 在其他窗口停留
        away_time = random.uniform(*away_duration_range)
        print(f"[模拟] 在其他窗口停留 {away_time:.1f} 秒...")

        # 停留期间模拟在其他窗口有操作（微抖动 + 偶尔滚动/点击）
        end_time = time.perf_counter() + away_time
        while time.perf_counter() < end_time:
            action = random.random()
            if action < 0.6:
                # 大部分时间只是鼠标微抖（在看内容）
                cx, cy = pyautogui.position()
                jx = random.gauss(0, 1.0)
                jy = random.gauss(0, 1.0)
                pyautogui.moveTo(max(0, int(cx + jx)), max(0, int(cy + jy)), _pause=False)
                time.sleep(random.uniform(0.3, 0.8))
            elif action < 0.8:
                # 偶尔滚动一下
                pyautogui.scroll(random.choice([-100, -50, 50, 100]))
                time.sleep(random.uniform(0.5, 1.5))
            else:
                # 偶尔鼠标移动一段距离
                sw, sh = pyautogui.size()
                rx = random.randint(int(sw * 0.1), int(sw * 0.9))
                ry = random.randint(int(sh * 0.1), int(sh * 0.9))
                self.move_to(rx, ry, duration_range=(0.3, 0.6))
                time.sleep(random.uniform(0.3, 1.0))

        # 切回原窗口
        print("[模拟] 切回原窗口...")
        time.sleep(random.uniform(0.1, 0.4))
        pyautogui.hotkey("alt", "tab")
        time.sleep(random.uniform(0.5, 1.0))  # 等窗口切换动画

    def copy_selection(self):
        """选中内容后 Ctrl+C 复制，带自然停顿"""
        time.sleep(random.uniform(0.2, 0.5))
        pyautogui.hotkey("ctrl", "c")
        time.sleep(random.uniform(0.1, 0.3))

    def undo(self):
        """Ctrl+Z 撤销，人偶尔会反悔"""
        time.sleep(random.uniform(0.3, 0.8))
        pyautogui.hotkey("ctrl", "z")
        time.sleep(random.uniform(0.2, 0.5))

    # ─────────────── 滚回顶部 / 犹豫回滚 ───────────────

    def scroll_back_up(self, clicks=None):
        """
        【回滚】看到一半突然往回滚，真人经常回头看之前的内容。
        """
        if clicks is None:
            clicks = random.randint(150, 400)
        self.scroll(clicks)  # 正数=向上滚
        time.sleep(random.uniform(0.5, 2.0))

    def scroll_to_top(self):
        """按 Home 键回到页面顶部"""
        time.sleep(random.uniform(0.2, 0.5))
        pyautogui.press("home")
        time.sleep(random.uniform(0.3, 0.8))

    def scroll_to_bottom(self):
        """按 End 键滚到页面底部"""
        time.sleep(random.uniform(0.2, 0.5))
        pyautogui.press("end")
        time.sleep(random.uniform(0.3, 0.8))

    # ─────────────── 犹豫/取消 行为 ───────────────

    def hesitate_click(self, x, y):
        """
        【犹豫点击】移到按钮附近 → 犹豫停顿 → 鼠标移开 → 再移回来点击。
        真人经常在点击前犹豫。
        """
        # 移到目标附近
        near_x = x + random.randint(-30, 30)
        near_y = y + random.randint(-20, 20)
        self.move_to(near_x, near_y, duration_range=(0.4, 0.8))
        time.sleep(random.uniform(0.5, 1.5))  # 犹豫

        # 鼠标移走（反悔/思考）
        away_x = x + random.choice([-1, 1]) * random.randint(80, 200)
        away_y = y + random.choice([-1, 1]) * random.randint(50, 150)
        sw, sh = pyautogui.size()
        away_x = max(10, min(away_x, sw - 10))
        away_y = max(10, min(away_y, sh - 10))
        self.move_to(away_x, away_y, duration_range=(0.3, 0.6))
        time.sleep(random.uniform(0.8, 2.5))  # 想了想

        # 移回来点击
        self.click(x, y)

    def aborted_click(self, x, y):
        """
        【放弃点击】移向目标但中途放弃，鼠标移到别处。
        真人有时候移向一个按钮，想了想又没点。
        """
        # 移到目标附近但不精确
        near_x = x + random.randint(-15, 15)
        near_y = y + random.randint(-10, 10)
        self.move_to(near_x, near_y, duration_range=(0.4, 0.8))
        time.sleep(random.uniform(0.3, 1.0))

        # 移走，没点击
        sw, sh = pyautogui.size()
        away_x = random.randint(int(sw * 0.1), int(sw * 0.9))
        away_y = random.randint(int(sh * 0.1), int(sh * 0.9))
        self.move_to(away_x, away_y, duration_range=(0.3, 0.7))

    # ─────────────── 鼠标移出窗口 ───────────────

    def mouse_leave_window(self, duration_range=(1.0, 4.0)):
        """
        【鼠标离开页面区域】真人有时鼠标会移出浏览器窗口区域
        （移到任务栏、桌面边缘等），过一会再回来。
        """
        sw, sh = pyautogui.size()
        # 移到屏幕边缘
        edge = random.choice(["bottom", "right", "top"])
        if edge == "bottom":
            ex, ey = random.randint(int(sw * 0.2), int(sw * 0.8)), sh - random.randint(1, 5)
        elif edge == "right":
            ex, ey = sw - random.randint(1, 5), random.randint(int(sh * 0.2), int(sh * 0.8))
        else:
            ex, ey = random.randint(int(sw * 0.2), int(sw * 0.8)), random.randint(1, 5)

        self.move_to(ex, ey, duration_range=(0.3, 0.7))
        time.sleep(random.uniform(*duration_range))

        # 回到页面内容区域
        back_x = random.randint(int(sw * 0.15), int(sw * 0.85))
        back_y = random.randint(int(sh * 0.2), int(sh * 0.7))
        self.move_to(back_x, back_y, duration_range=(0.4, 0.8))

    # ─────────────── 综合随机行为 ───────────────

    def random_human_noise(self, screen_width, screen_height,
                           area=(0.12, 0.95, 0.22, 0.78)):
        """
        【综合随机噪声】从一组真人常见的"无意义"小动作中随机执行一个。
        在自动化流程的间隙调用，增加行为的不可预测性。

        可能的动作：
        - 鼠标空闲微抖动
        - 随机漫游
        - 回滚一下又滚回去
        - 鼠标移出页面又回来
        - 犹豫式移动（移向某处又移开）
        - 切换窗口再切回来
        - 点一下地址栏再按 Esc
        """
        actions = [
            ("idle_jitter", 0.25),
            ("wander", 0.20),
            ("scroll_back_up", 0.10),
            ("mouse_leave", 0.10),
            ("aborted_click", 0.10),
            ("switch_away", 0.10),
            ("nothing", 0.07),
        ]
        names, weights = zip(*actions)
        choice = random.choices(names, weights=weights, k=1)[0]

        left = int(screen_width * area[0])
        right = int(screen_width * area[1])
        top = int(screen_height * area[2])
        bottom = int(screen_height * area[3])

        if choice == "idle_jitter":
            self.idle_jitter(duration=random.uniform(1.0, 3.0))
        elif choice == "wander":
            self.wander(screen_width, screen_height, count=random.randint(1, 3), area=area)
        elif choice == "scroll_back_up":
            self.scroll_back_up(clicks=random.randint(100, 250))
        elif choice == "mouse_leave":
            self.mouse_leave_window(duration_range=(1.0, 3.0))
        elif choice == "aborted_click":
            rx = random.randint(left, right)
            ry = random.randint(top, bottom)
            self.aborted_click(rx, ry)
        elif choice == "switch_away":
            self.switch_away_and_back(away_duration_range=(2.0, 8.0))
        else:
            # 什么都不做，只是短暂发呆
            time.sleep(random.uniform(0.5, 2.0))

    # ─────────────── 手离开鼠标（完全静止） ───────────────

    def hands_off(self, duration_range=(3.0, 20.0)):
        """
        【手离开鼠标】真人有时会放下鼠标去喝水、看手机、思考，
        此时鼠标完全静止不动（与 idle_jitter 不同，这里是真正静止）。
        检测系统如果发现鼠标一直在微抖反而不正常，
        真人是有完全静止期的。
        """
        duration = random.uniform(*duration_range)
        time.sleep(duration)

    # ─────────────── 误触缩放再恢复 ───────────────

    def accidental_zoom(self):
        """
        【误触缩放】Ctrl+滚轮不小心缩放了页面，发现后 Ctrl+0 恢复。
        真人偶尔会按住 Ctrl 滚轮导致页面缩放。
        """
        # 不小心缩放
        zoom_direction = random.choice([1, -1])
        zoom_steps = random.randint(1, 3)
        pyautogui.keyDown("ctrl")
        time.sleep(random.uniform(0.05, 0.15))
        for _ in range(zoom_steps):
            pyautogui.scroll(zoom_direction)
            time.sleep(random.uniform(0.05, 0.15))
        pyautogui.keyUp("ctrl")

        # 发现页面缩放了，停顿一下
        time.sleep(random.uniform(0.5, 1.5))

        # Ctrl+0 恢复默认缩放
        pyautogui.hotkey("ctrl", "0")
        time.sleep(random.uniform(0.3, 0.8))

    # ─────────────── Ctrl+F 搜索 ───────────────

    def find_and_close(self, keyword=None):
        """
        【页面搜索】Ctrl+F 打开搜索框，输入几个字看看，然后 Esc 关闭。
        真人有时候想找页面上的某个内容。
        """
        random_keywords = ["视频", "上传", "发布", "设置", "标题", "简介"]
        if keyword is None:
            keyword = random.choice(random_keywords)

        pyautogui.hotkey("ctrl", "f")
        time.sleep(random.uniform(0.5, 1.0))

        # 逐字输入关键词
        self.type_text(keyword)
        time.sleep(random.uniform(1.0, 3.0))  # 看看搜索结果

        # 关闭搜索框
        pyautogui.press("escape")
        time.sleep(random.uniform(0.3, 0.8))

    # ─────────────── 鼠标扫视文字（模拟阅读） ───────────────

    def scan_text(self, x, y, line_width=400, lines=3):
        """
        【扫视阅读】鼠标在一段文字区域左右来回移动，
        模拟人眼跟随鼠标阅读的习惯（很多人看网页时鼠标跟着文字走）。
        """
        for line in range(lines):
            # 从左到右
            start_x = x
            end_x = x + random.randint(int(line_width * 0.7), line_width)
            curr_y = y + line * random.randint(18, 28)

            self.move_to(start_x, curr_y, duration_range=(0.5, 1.2))
            self.move_to(end_x, curr_y + random.randint(-3, 3),
                         duration_range=(0.8, 1.5))

            # 行末短暂停留（换行思考）
            time.sleep(random.uniform(0.1, 0.4))

    # ─────────────── 方向键微调滚动 ───────────────

    def arrow_key_scroll(self, presses=None):
        """
        【方向键滚动】用键盘上下方向键代替滚轮微调页面位置。
        有些人习惯用方向键精细滚动。
        """
        if presses is None:
            presses = random.randint(2, 6)

        direction = random.choice(["down", "up"])
        for _ in range(presses):
            self._key_press_with_duration(direction)
            time.sleep(random.uniform(0.1, 0.4))

        time.sleep(random.uniform(0.3, 0.8))

    # ─────────────── Tab 键在表单字段间跳转 ───────────────

    def tab_between_fields(self, tab_count=None):
        """
        【Tab 跳转】按 Tab 键在表单字段之间切换，
        不是每个字段都要用鼠标点击，真人常用 Tab 跳到下一个。
        """
        if tab_count is None:
            tab_count = random.randint(1, 3)

        for _ in range(tab_count):
            self._key_press_with_duration("tab")
            time.sleep(random.uniform(0.2, 0.8))

    # ─────────────── 重复点击（没反应再点一次） ───────────────

    def retry_click(self, x, y):
        """
        【重复点击】点了一次没反应（或觉得没点到），再点一次。
        两次点击间隔比双击长，落点有轻微偏移。
        """
        self.click(x, y)
        # 等了一会没反应
        time.sleep(random.uniform(0.8, 2.0))
        # 再点一次（落点略有偏移）
        self.click(x, y, offset_radius=8)

    # ─────────────── 鼠标突然快速甩动 ───────────────

    def mouse_flick(self):
        """
        【甩鼠标】真人有时会快速甩一下鼠标（无目的），
        比如在等待加载时随意晃动鼠标。
        """
        sw, sh = pyautogui.size()
        cx, cy = pyautogui.position()

        # 快速甩到随机方向
        flick_x = cx + random.randint(-300, 300)
        flick_y = cy + random.randint(-200, 200)
        flick_x = max(10, min(flick_x, sw - 10))
        flick_y = max(10, min(flick_y, sh - 10))

        self.move_to(flick_x, flick_y, duration_range=(0.1, 0.25))
        time.sleep(random.uniform(0.2, 0.8))

    # ─────────────── 更新 random_human_noise 权重表 ───────────────

    def random_human_noise_v2(self, screen_width, screen_height,
                              area=(0.12, 0.95, 0.22, 0.78)):
        """
        【综合随机噪声 v2】在 random_human_noise 基础上增加更多真人小动作。
        从加权动作池中随机挑选执行，让行为更不可预测。
        """
        actions = [
            ("idle_jitter", 0.12),
            ("wander", 0.12),
            ("scroll_back_up", 0.06),
            ("mouse_leave", 0.06),
            ("aborted_click", 0.06),
            ("switch_away", 0.06),
            ("unconscious_select", 0.08),
            ("hands_off", 0.08),
            ("scan_text", 0.06),
            ("mouse_flick", 0.06),
            ("arrow_scroll", 0.05),
            ("find_and_close", 0.04),
            ("accidental_zoom", 0.02),
            ("nothing", 0.08),
        ]
        names, weights = zip(*actions)
        choice = random.choices(names, weights=weights, k=1)[0]

        left = int(screen_width * area[0])
        right = int(screen_width * area[1])
        top = int(screen_height * area[2])
        bottom = int(screen_height * area[3])
        rx = random.randint(left, right)
        ry = random.randint(top, bottom)

        if choice == "idle_jitter":
            self.idle_jitter(duration=random.uniform(1.0, 3.0))
        elif choice == "wander":
            self.wander(screen_width, screen_height, count=random.randint(1, 3), area=area)
        elif choice == "scroll_back_up":
            self.scroll_back_up(clicks=random.randint(100, 250))
        elif choice == "mouse_leave":
            self.mouse_leave_window(duration_range=(1.0, 3.0))
        elif choice == "aborted_click":
            self.aborted_click(rx, ry)
        elif choice == "switch_away":
            self.switch_away_and_back(away_duration_range=(2.0, 8.0))
        elif choice == "hands_off":
            self.hands_off(duration_range=(2.0, 8.0))
        elif choice == "scan_text":
            self.scan_text(rx, ry, line_width=random.randint(200, 400), lines=random.randint(2, 4))
        elif choice == "mouse_flick":
            self.mouse_flick()
        elif choice == "arrow_scroll":
            self.arrow_key_scroll()
        elif choice == "find_and_close":
            self.find_and_close()
        elif choice == "accidental_zoom":
            self.accidental_zoom()
        else:
            time.sleep(random.uniform(0.5, 2.0))

    def impatient_waiting(self, duration_range=(3.0, 12.0)):
        """
        【不耐烦等待】模拟真人在等待页面加载/处理完成时的不耐烦行为。
        不会点击或刷新，避免干扰页面状态。可产生滚动和鼠标移动。

        真人等待时的典型表现：
        - 前期：安静等待，只有轻微鼠标抖动或完全静止
        - 中期：开始不耐烦，小幅移动鼠标、甩鼠标、无目的滚动
        - 后期：频繁甩鼠标、鼠标画圈、大幅漫游、来回滚动

        不耐烦程度随时间线性增长，动作频率和幅度也随之增大。

        参数：
            duration_range: 等待总时长范围（秒），也可传入固定秒数的元组如 (3.0, 3.0)
        """
        duration = random.uniform(*duration_range)
        start_time = time.perf_counter()
        end_time = start_time + duration
        sw, sh = pyautogui.size()

        while time.perf_counter() < end_time:
            elapsed = time.perf_counter() - start_time
            # 不耐烦程度：0.0（刚开始）→ 1.0（快结束）
            impatience = min(1.0, elapsed / duration)

            # 根据不耐烦程度选择行为和间隔
            action = random.random()

            if impatience < 0.3:
                # ── 前期：安静等待 ──
                if action < 0.6:
                    # 鼠标微抖动（手放在鼠标上无聊等待）
                    cx, cy = pyautogui.position()
                    jx = random.gauss(0, 0.5)
                    jy = random.gauss(0, 0.5)
                    pyautogui.moveTo(max(0, int(cx + jx)), max(0, int(cy + jy)), _pause=False)
                    time.sleep(random.uniform(0.5, 1.5))
                elif action < 0.85:
                    # 完全静止（盯着屏幕看）
                    time.sleep(random.uniform(0.8, 2.0))
                else:
                    # 偶尔小幅移动鼠标
                    cx, cy = pyautogui.position()
                    nx = cx + random.randint(-50, 50)
                    ny = cy + random.randint(-30, 30)
                    nx = max(10, min(nx, sw - 10))
                    ny = max(10, min(ny, sh - 10))
                    self.move_to(nx, ny, duration_range=(0.3, 0.6))
                    time.sleep(random.uniform(0.3, 0.8))

            elif impatience < 0.65:
                # ── 中期：开始不耐烦 ──
                if action < 0.25:
                    # 甩鼠标（无聊开始晃）
                    self.mouse_flick()
                elif action < 0.45:
                    # 鼠标在页面内容区漫游（无目的移动）
                    rx = random.randint(int(sw * 0.15), int(sw * 0.85))
                    ry = random.randint(int(sh * 0.2), int(sh * 0.7))
                    self.move_to(rx, ry, duration_range=(0.3, 0.7))
                    time.sleep(random.uniform(0.3, 1.0))
                elif action < 0.6:
                    # 无目的滚动几下
                    scroll_amount = random.choice([-80, -40, 40, 80])
                    pyautogui.scroll(scroll_amount)
                    time.sleep(random.uniform(0.3, 0.8))
                elif action < 0.75:
                    # 鼠标移到屏幕边缘再移回来（想去做别的又没动）
                    edge_x = random.choice([random.randint(5, 30), sw - random.randint(5, 30)])
                    edge_y = random.randint(int(sh * 0.2), int(sh * 0.8))
                    self.move_to(edge_x, edge_y, duration_range=(0.3, 0.6))
                    time.sleep(random.uniform(0.3, 0.8))
                    back_x = random.randint(int(sw * 0.2), int(sw * 0.8))
                    back_y = random.randint(int(sh * 0.3), int(sh * 0.7))
                    self.move_to(back_x, back_y, duration_range=(0.2, 0.5))
                else:
                    # 微抖动 + 叹气式停顿
                    self.idle_jitter(duration=random.uniform(0.5, 1.5))

            else:
                # ── 后期：明显不耐烦 ──
                if action < 0.25:
                    # 快速甩鼠标（烦躁）
                    self.mouse_flick()
                    if random.random() < 0.3:
                        self.mouse_flick()  # 连续甩两下
                elif action < 0.45:
                    # 鼠标画圈（无聊到极致）
                    cx, cy = pyautogui.position()
                    radius = random.randint(30, 80)
                    circle_steps = random.randint(8, 16)
                    for step in range(circle_steps):
                        angle = 2 * math.pi * step / circle_steps
                        px = int(cx + radius * math.cos(angle))
                        py = int(cy + radius * math.sin(angle))
                        px = max(10, min(px, sw - 10))
                        py = max(10, min(py, sh - 10))
                        pyautogui.moveTo(px, py, _pause=False)
                        time.sleep(random.uniform(0.02, 0.06))
                    time.sleep(random.uniform(0.2, 0.5))
                elif action < 0.6:
                    # 烦躁地来回滚动
                    pyautogui.scroll(random.randint(-150, -50))
                    time.sleep(random.uniform(0.2, 0.5))
                    pyautogui.scroll(random.randint(50, 150))
                    time.sleep(random.uniform(0.2, 0.5))
                elif action < 0.75:
                    # 大幅度漫游（烦躁地来回移动鼠标）
                    for _ in range(random.randint(2, 4)):
                        rx = random.randint(int(sw * 0.05), int(sw * 0.95))
                        ry = random.randint(int(sh * 0.05), int(sh * 0.95))
                        self.move_to(rx, ry, duration_range=(0.15, 0.35))
                    time.sleep(random.uniform(0.2, 0.5))
                else:
                    # 短暂静止（死鱼眼盯着屏幕）
                    time.sleep(random.uniform(0.3, 1.0))

            # 动作间隔随不耐烦程度缩短
            base_gap = max(0.1, 0.8 * (1 - impatience * 0.7))
            time.sleep(random.uniform(base_gap * 0.5, base_gap * 1.5))

    @staticmethod
    def page_stay(min_sec=2.0, max_sec=8.0):
        """
        【页面停留时长】模拟在页面上停留一段时间。
        """
        time.sleep(random.uniform(min_sec, max_sec))

    @staticmethod
    def pause(min_sec=0.3, max_sec=1.0):
        """随机停顿，模拟人在思考"""
        time.sleep(random.uniform(min_sec, max_sec))

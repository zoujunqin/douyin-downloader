"""
小红书创作者中心 - 首页操作模块

流程：
  1. 打开 Chrome 浏览器，最大化，导航到首页，等待加载完成
  2. 模拟真人随机浏览（鼠标漫游、切换窗口、滚动等）
  3. 截图匹配左侧菜单按钮，模拟真人在菜单间浏览
  4. 截图匹配「发布图文笔记」和「发布视频笔记」，模拟真人在两者间犹豫
  5. 最终点击「发布视频笔记」

公共入口: run() -> 返回点击后的状态，供后续模块衔接
"""

import subprocess
import sys
import time
import random
import os
import sqlite3
import ctypes
import ctypes.wintypes

# 确保 pyautogui 目录在 sys.path 中，以便兄弟模块（human 等）可被正确导入
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
if _THIS_DIR not in sys.path:
    sys.path.insert(0, _THIS_DIR)

import pyautogui
import pyperclip
import glob

from human import HumanInput

# ─────────────── 配置 ───────────────

HOME_URL = "https://creator.xiaohongshu.com/new/home"
_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
REF_IMG_DIR = os.path.join(_BASE_DIR, "ref_img", "home_page")
SCREENSHOT_DIR = os.path.join(_BASE_DIR, "tmp")
os.makedirs(SCREENSHOT_DIR, exist_ok=True)

pyautogui.FAILSAFE = True
pyautogui.PAUSE = 0.3

# ─────────────── 全局状态 ───────────────

user32 = ctypes.windll.user32
_chrome_hwnd = None
_screenshot_counter = 0
hi = HumanInput()


# ─────────────── 工具函数 ───────────────

def _take_screenshot(label):
    global _screenshot_counter
    _screenshot_counter += 1
    filename = f"{SCREENSHOT_DIR}/step_{_screenshot_counter:02d}_{label}.png"
    pyautogui.screenshot().save(filename)
    print(f"  [截图] {filename}")


def _safe_hotkey(*keys):
    _ensure_foreground()
    hi.hotkey(*keys)


def _safe_press(key):
    _ensure_foreground()
    hi.press(key)


# ─────────────── 窗口管理 ───────────────

def _find_chrome_hwnd():
    hwnds = []

    def enum_cb(hwnd, _):
        if user32.IsWindowVisible(hwnd):
            length = user32.GetWindowTextLengthW(hwnd)
            if length > 0:
                buf = ctypes.create_unicode_buffer(length + 1)
                user32.GetWindowTextW(hwnd, buf, length + 1)
                title = buf.value
                if "Google Chrome" in title or "小红书" in title or "chrome" in title.lower():
                    hwnds.append(hwnd)
        return True

    WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.wintypes.HWND, ctypes.wintypes.LPARAM)
    user32.EnumWindows(WNDENUMPROC(enum_cb), 0)
    return hwnds[0] if hwnds else None


def _get_window_title(hwnd):
    length = user32.GetWindowTextLengthW(hwnd)
    if length == 0:
        return ""
    buf = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buf, length + 1)
    return buf.value


def _is_target_foreground():
    fg = user32.GetForegroundWindow()
    if fg == 0:
        return False
    title = _get_window_title(fg)
    keywords = ["Google Chrome", "小红书", "chrome", "打开", "Open"]
    return any(kw.lower() in title.lower() for kw in keywords)


def _is_maximized(hwnd):
    class WINDOWPLACEMENT(ctypes.Structure):
        _fields_ = [
            ("length", ctypes.c_uint),
            ("flags", ctypes.c_uint),
            ("showCmd", ctypes.c_uint),
            ("ptMinPosition", ctypes.wintypes.POINT),
            ("ptMaxPosition", ctypes.wintypes.POINT),
            ("rcNormalPosition", ctypes.wintypes.RECT),
        ]
    wp = WINDOWPLACEMENT()
    wp.length = ctypes.sizeof(WINDOWPLACEMENT)
    user32.GetWindowPlacement(hwnd, ctypes.byref(wp))
    return wp.showCmd == 3


def _ensure_maximized():
    global _chrome_hwnd
    hwnd = _chrome_hwnd or _find_chrome_hwnd()
    if not hwnd or _is_maximized(hwnd):
        return
    print("[窗口] 正在最大化...")
    user32.ShowWindow(hwnd, 3)
    time.sleep(0.5)
    if not _is_maximized(hwnd):
        pyautogui.hotkey("win", "up")
        time.sleep(0.5)
    print("[窗口] 已最大化")


def _ensure_foreground():
    if _is_target_foreground():
        _ensure_maximized()
        return True

    print("[警告] 目标窗口不在前台，正在激活...")
    global _chrome_hwnd
    hwnd = _find_chrome_hwnd()
    if hwnd:
        _chrome_hwnd = hwnd
    elif _chrome_hwnd is None:
        print("[错误] 找不到 Chrome 窗口！")
        return False

    user32.SetForegroundWindow(_chrome_hwnd)
    time.sleep(0.3)
    if _is_target_foreground():
        _ensure_maximized()
        return True

    user32.keybd_event(0x12, 0, 0x0002, 0)
    time.sleep(0.1)
    user32.SetForegroundWindow(_chrome_hwnd)
    time.sleep(0.3)
    if _is_target_foreground():
        _ensure_maximized()
        return True

    pyautogui.hotkey("alt", "tab")
    time.sleep(0.5)
    if _is_target_foreground():
        _ensure_maximized()
    return _is_target_foreground()


def _update_chrome_hwnd():
    global _chrome_hwnd
    _chrome_hwnd = _find_chrome_hwnd() or _chrome_hwnd


# ─────────────── 图片匹配（带滚动重试） ───────────────

def _locate_ref_image(ref_image_name, confidence=0.7, max_scroll_rounds=5):
    """
    截图对比参考图片，找不到则模拟真人滚动后再对比。
    返回匹配区域 Box(left, top, width, height) 或 None。
    """
    ref_path = f"{REF_IMG_DIR}/{ref_image_name}"
    if not os.path.exists(ref_path):
        print(f"  [警告] 参考图片不存在: {ref_path}")
        return None

    screen_w, screen_h = pyautogui.size()

    for attempt in range(1, max_scroll_rounds + 2):
        label = "初始" if attempt == 1 else f"第{attempt - 1}次滚动后"
        print(f"  [{label}] 对比 {ref_image_name}...")
        try:
            location = pyautogui.locateOnScreen(ref_path, confidence=confidence)
        except pyautogui.ImageNotFoundException:
            location = None
        if location is not None:
            print(f"  匹配成功！区域: {location}")
            return location

        if attempt > max_scroll_rounds:
            break

        # 模拟真人向下滚动
        print(f"  未匹配到，真人向下滚动寻找...")
        _ensure_foreground()
        _human_scroll("down")

    print(f"  [警告] {ref_image_name} 匹配失败")
    return None


def _human_scroll(direction="down"):
    """
    模拟真人分段滚动半屏左右的距离。
    direction: "down" 向下滚，"up" 向上滚。
    分 2~3 段完成，每段直接用 pyautogui.scroll 大步滚（绕过 hi.scroll 内部拆分过细的问题），
    段间用 hi.pause 停顿。
    """
    screen_w, screen_h = pyautogui.size()
    sign = -1 if direction == "down" else 1
    # 总共滚 15~25 个大格（每格约 100px，合计 1500~2500px ≈ 半屏到一屏）
    total = random.randint(15, 25)
    segments = random.randint(2, 3)

    for seg in range(segments):
        # 最后一段用剩余量
        if seg < segments - 1:
            seg_amount = random.randint(total // (segments + 1) + 1, total // segments + 2)
            seg_amount = min(seg_amount, total)
        else:
            seg_amount = total
        if seg_amount <= 0:
            break
        total -= seg_amount

        # 先把鼠标移到页面中间随机位置
        mx = random.randint(int(screen_w * 0.3), int(screen_w * 0.7))
        my = random.randint(int(screen_h * 0.3), int(screen_h * 0.7))
        hi.move_to(mx, my, duration_range=(0.2, 0.4))
        # 直接用 pyautogui.scroll 大步滚，每步间隔模拟人手
        for _ in range(seg_amount):
            pyautogui.scroll(sign * random.randint(150, 250))
            time.sleep(random.uniform(0.02, 0.08))
        # 段间停顿
        hi.pause(0.5, 1.2)
    # 滚完后看一下页面
    hi.pause(0.8, 1.5)


def _box_center(box):
    """从 Box(left, top, width, height) 取中心坐标"""
    return box.left + box.width // 2, box.top + box.height // 2


def _random_point_in_box(box, margin=5):
    """在匹配区域内取一个随机点（不是正中心，模拟真人）"""
    x = random.randint(box.left + margin, box.left + box.width - margin)
    y = random.randint(box.top + margin, box.top + box.height - margin)
    return x, y


# ─────────────── 步骤1: 打开浏览器，导航到首页 ───────────────

def _step_open_browser():
    global _chrome_hwnd
    print(f"  导航到: {HOME_URL}")

    subprocess.Popen(["C:/Program Files/Google/Chrome/Application/chrome.exe", "--start-maximized"])
    time.sleep(3)

    _chrome_hwnd = _find_chrome_hwnd()
    if _chrome_hwnd:
        print(f"  Chrome 句柄: {_chrome_hwnd}")
        print(f"  窗口标题: {_get_window_title(_chrome_hwnd)}")

    _ensure_foreground()
    _ensure_maximized()

    # 地址栏输入 URL
    _safe_hotkey("ctrl", "l")
    hi.pause(0.3, 0.8)
    hi.paste_text(HOME_URL)
    hi.pause(0.2, 0.5)
    _safe_press("enter")

    # 等待页面加载
    print("  等待页面加载...")
    hi.impatient_waiting(duration_range=(4.0, 6.0))
    _update_chrome_hwnd()
    _take_screenshot("home_loaded")
    print("  首页加载完成")


# ─────────────── 步骤1.5: 登录检测 ───────────────

def _step_check_login():
    """
    截图匹配 login_button.png，如果匹配到说明未登录，退出程序并提示。
    """
    print("  检测登录状态...")
    _ensure_foreground()

    ref_path = f"{REF_IMG_DIR}/login_button.png"
    if not os.path.exists(ref_path):
        print(f"  [跳过] 登录检测参考图片不存在: {ref_path}")
        return

    try:
        location = pyautogui.locateOnScreen(ref_path, confidence=0.7)
    except pyautogui.ImageNotFoundException:
        location = None

    if location is not None:
        _take_screenshot("login_required")
        print("\n" + "!" * 50)
        print("  检测到登录按钮，当前未登录！")
        print("  请先在浏览器中手动登录小红书，登录完成后再重新执行本脚本。")
        print("!" * 50)
        sys.exit(1)

    print("  已登录，继续执行...")


# ─────────────── 步骤2: 模拟真人随机浏览 ───────────────

def _step_random_browse():
    _ensure_foreground()
    screen_w, screen_h = pyautogui.size()

    print("  真人随机浏览首页...")
    hi.browse_page(screen_w, screen_h)

    # 随机穿插一些真人噪声动作
    noise_count = random.randint(1, 3)
    for i in range(noise_count):
        action = random.choice([
            "switch_away",
            "wander",
            "noise",
            "idle",
        ])
        if action == "switch_away":
            print("  切换窗口再切回...")
            hi.switch_away_and_back(away_duration_range=(2.0, 6.0))
            _ensure_foreground()
        elif action == "wander":
            print("  随机漫游...")
            hi.wander(screen_w, screen_h, count=random.randint(2, 4))
        elif action == "noise":
            print("  随机小动作...")
            hi.random_human_noise_v2(screen_w, screen_h)
        else:
            print("  发呆...")
            hi.hands_off(duration_range=(1.0, 3.0))

    _ensure_foreground()
    print("  随机浏览完成")


# ─────────────── 步骤3: 菜单浏览 + 定位上传按钮 + 点击 ───────────────

def _step_browse_menu_and_click_upload():
    _ensure_foreground()
    screen_w, screen_h = pyautogui.size()

    # --- 3a: 匹配「首页」菜单按钮，记录位置 ---
    print("  定位「首页」菜单按钮...")
    home_btn_box = _locate_ref_image("menu_home_button.png", max_scroll_rounds=0)
    if home_btn_box:
        home_btn_pos = _box_center(home_btn_box)
        print(f"  「首页」按钮位置: {home_btn_pos}")
    else:
        home_btn_pos = None
        print("  「首页」按钮未匹配到，继续...")


    # --- 3c: 匹配「发布图文笔记」和「发布视频笔记」 ---
    print("  定位「发布图文笔记」和「发布视频笔记」...")
    _ensure_foreground()
    _take_screenshot("before_upload_match")

    note_box = _locate_ref_image("home_note_upload.png", max_scroll_rounds=3)
    video_box = _locate_ref_image("home_video_upload.png", max_scroll_rounds=0)

    if not video_box:
        # 可能页面滚动过头了，先定位「新的创作」标题，然后向上滚动回去
        print("  「发布视频笔记」未找到，尝试定位标题后向上滚动...")
        title_box = _locate_ref_image("title_create_topic.png", max_scroll_rounds=0)
        if title_box:
            print(f"  找到标题「新的创作」区域: {title_box}，向上滚动...")
        else:
            print("  标题也未匹配到，直接向上滚动·...")

        # 模拟真人向上滚动，最多尝试5轮
        for scroll_round in range(1, 6):
            _ensure_foreground()
            _human_scroll("up")

            print(f"  第{scroll_round}次向上滚动后，重新匹配 home_video_upload.png...")
            video_box = _locate_ref_image("home_video_upload.png", max_scroll_rounds=0)
            if video_box:
                break

    if not video_box:
        print("  [警告] 「发布视频笔记」仍未匹配到，使用比例坐标兜底")

    # --- 3d: 模拟真人在两个区域之间犹豫 ---
    targets = []
    if note_box:
        targets.append(("发布图文笔记", note_box))
    if video_box:
        targets.append(("发布视频笔记", video_box))

    if len(targets) >= 2:
        # 随机顺序在两个区域间移动鼠标，模拟犹豫
        random.shuffle(targets)
        for name, box in targets:
            rx, ry = _random_point_in_box(box)
            print(f"  鼠标移到「{name}」区域 ({rx}, {ry})...")
            _ensure_foreground()
            hi.hover(rx, ry, hover_duration_range=(0.8, 2.0))
            hi.pause(0.3, 1.0)

        # 再把鼠标移回另一个区域，模拟反复比较
        if random.random() < 0.5:
            name, box = targets[0]
            rx, ry = _random_point_in_box(box)
            print(f"  又看了看「{name}」({rx}, {ry})...")
            hi.move_to(rx, ry)
            hi.pause(0.5, 1.2)
    elif len(targets) == 1:
        name, box = targets[0]
        rx, ry = _random_point_in_box(box)
        hi.hover(rx, ry, hover_duration_range=(0.5, 1.5))

    # --- 3e: 点击「发布视频笔记」并验证上传对话框弹出 ---
    max_retries = random.randint(2, 4)
    upload_dialog_found = False

    for attempt in range(1, max_retries + 1):
        print(f"\n  --- 第 {attempt} 次尝试点击「发布视频笔记」---")
        _ensure_foreground()

        # 重新匹配视频上传按钮（重试时页面可能已变化）
        if attempt > 1:
            video_box = _locate_ref_image("home_video_upload.png", max_scroll_rounds=3)

        if video_box:
            click_x, click_y = _random_point_in_box(video_box)
            print(f"  点击「发布视频笔记」: ({click_x}, {click_y})")
            action = random.choice(["hesitate", "hover_click", "direct"])
            if action == "hesitate":
                hi.hesitate_click(click_x, click_y)
            elif action == "hover_click":
                hi.hover_link(click_x, click_y)
                hi.pause(0.2, 0.6)
                hi.click(click_x, click_y)
            else:
                hi.click(click_x, click_y)
        else:
            click_x = int(screen_w * 0.43)
            click_y = int(screen_h * 0.39)
            print(f"  兜底点击「发布视频笔记」: ({click_x}, {click_y})")
            hi.hesitate_click(click_x, click_y)

        # 等待页面加载
        print("  等待上传页加载...")
        hi.page_stay(3.0, 5.0)
        _update_chrome_hwnd()
        _take_screenshot("after_video_upload_click")

        # 检测上传文件对话框是否弹出
        print("  检测上传文件对话框...")
        upload_dialog_box = _locate_ref_image("window_upload_file.png", max_scroll_rounds=0)
        if upload_dialog_box:
            print("  上传文件对话框已弹出！")
            upload_dialog_found = True
            break

        print(f"  未检测到上传文件对话框（第 {attempt}/{max_retries} 次）")

        if attempt < max_retries:
            # 点击首页按钮回到首页，准备重试
            if home_btn_pos:
                print(f"  点击「首页」按钮回到首页: {home_btn_pos}")
                _ensure_foreground()
                hi.click(*home_btn_pos)
            else:
                # 兜底：用地址栏导航回首页
                print("  「首页」按钮位置未知，通过地址栏返回首页...")
                _ensure_foreground()
                _safe_hotkey("ctrl", "l")
                hi.pause(0.3, 0.6)
                hi.paste_text(HOME_URL)
                hi.pause(0.2, 0.4)
                _safe_press("enter")

            # 等待首页重新加载
            print("  等待首页重新加载...")
            hi.impatient_waiting(duration_range=(1.0, 3.0))
            _update_chrome_hwnd()
            _take_screenshot("retry_home_loaded")

            # 重新定位首页按钮
            home_btn_box = _locate_ref_image("menu_home_button.png", max_scroll_rounds=0)
            if home_btn_box:
                home_btn_pos = _box_center(home_btn_box)

    if not upload_dialog_found:
        print(f"  [警告] 重试 {max_retries} 次后仍未检测到上传文件对话框，继续执行...")

    print("  已完成上传页操作")


# ─────────────── 视频选择 ───────────────

DB_PATH = os.path.join(os.path.dirname(_BASE_DIR), "pyautogui", "publish_records.db")
VIDEO_EXTENSIONS = {".mp4", ".mov", ".avi", ".mkv", ".flv", ".wmv", ".webm"}


def _init_db():
    """确保数据库和表存在"""
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS publish_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            upload_dir TEXT,
            video_filename TEXT,
            video_path TEXT,
            publish_time TEXT
        )
    """)
    conn.commit()
    conn.close()


def _get_published_filenames(upload_dir):
    """从数据库获取指定 upload_dir 下所有已发布的视频文件名集合"""
    _init_db()
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.execute(
        "SELECT video_filename FROM publish_records WHERE upload_dir = ?",
        (upload_dir,),
    )
    published = {row[0] for row in cursor.fetchall()}
    conn.close()
    return published


def _pick_oldest_unpublished_video(upload_dir):
    """
    从 upload_dir 中选择创建时间最早的、尚未发布的视频文件。
    返回 (video_path, video_filename) 或 (None, None)。
    """
    if not os.path.isdir(upload_dir):
        print(f"  [错误] 视频上传目录不存在: {upload_dir}")
        return None, None

    published = _get_published_filenames(upload_dir)

    # 收集所有视频文件及其创建时间
    candidates = []
    for filename in os.listdir(upload_dir):
        ext = os.path.splitext(filename)[1].lower()
        if ext not in VIDEO_EXTENSIONS:
            continue
        if filename in published:
            continue
        filepath = os.path.join(upload_dir, filename)
        if not os.path.isfile(filepath):
            continue
        # 使用文件创建时间（Windows 上 st_ctime 是创建时间）
        ctime = os.stat(filepath).st_ctime
        candidates.append((ctime, filepath, filename))

    if not candidates:
        print(f"  [警告] 目录中没有可上传的视频（全部已发布或目录为空）: {upload_dir}")
        return None, None

    # 按创建时间升序排序，取最早的
    candidates.sort(key=lambda x: x[0])
    _, video_path, video_filename = candidates[0]
    print(f"  选中视频: {video_filename} (共 {len(candidates)} 个待发布)")
    return video_path, video_filename


# ─────────────── 步骤4: 在文件选择对话框中选择视频 ───────────────

def _step_select_video_file(video_path):
    """
    点击「发布视频笔记」后会弹出文件选择对话框，
    在地址栏输入视频文件的完整路径来选择文件。
    """
    print(f"  在文件对话框中选择: {video_path}")
    _take_screenshot("file_dialog_opened")

    # 等待文件对话框出现
    hi.pause(1.0, 2.0)

    # 在文件对话框的文件名输入框中输入路径
    # 文件对话框的文件名栏通常已获得焦点，直接输入路径
    hi.paste_text(video_path)
    _take_screenshot("file_dialog_path_entered")

    # 等待文件系统响应（路径解析、缩略图加载等需要时间）
    wait_sec = random.uniform(8.0, 15.0)
    print(f"  等待文件系统响应 {wait_sec:.1f} 秒...")
    time.sleep(wait_sec)

    # 按回车确认选择
    hi.press("enter")

# ─────────────── 公共入口 ───────────────

def run(config=None):
    """
    公共入口：打开首页 → 真人浏览 → 菜单浏览 → 点击「发布视频笔记」→ 选择视频文件。

    参数:
        config: 账号配置字典，包含 xiaohongshu_video_upload_dir_path 等字段。
                如果为 None，只执行到点击上传按钮为止。

    返回:
        dict: 包含 video_path, video_filename, account 等信息，供 publish_page 使用。
              如果无视频可上传，返回 None。
    """
    print("=" * 50)
    print("小红书首页 - 模拟真人浏览并进入上传页")
    print("=" * 50)

    # 如果有配置，先选好要上传的视频
    video_path = None
    video_filename = None
    upload_dir = ""
    if config:
        upload_dir = config.get("xiaohongshu_video_upload_dir_path", "").strip()
        if upload_dir:
            video_path, video_filename = _pick_oldest_unpublished_video(upload_dir)
            if video_path is None:
                print("\n没有可上传的视频，跳过本账号。")
                return None
        else:
            print("  [警告] 配置中未设置 xiaohongshu_video_upload_dir_path")

    print("\n脚本将在 3 秒后开始执行...")
    print("如需中止，请快速将鼠标移至屏幕左上角（FAILSAFE）")
    time.sleep(3)

    print("\n步骤1: 打开 Chrome 并导航到首页...")
    _step_open_browser()

    print("\n步骤1.5: 检测登录状态...")
    _step_check_login()

    print("\n步骤2: 模拟真人随机浏览...")
    _step_random_browse()

    print("\n步骤3: 浏览菜单并点击「发布视频笔记」...")
    _step_browse_menu_and_click_upload()

    # 步骤4: 如果有视频文件，在弹出的文件对话框中选择
    if video_path:
        print("\n步骤4: 在文件对话框中选择视频...")
        _step_select_video_file(video_path)

    print("\n首页流程执行完毕！")

    return {
        "video_path": video_path,
        "video_filename": video_filename,
        "upload_dir": upload_dir if config else "",
        "config": config,
    }


if __name__ == "__main__":
    run()

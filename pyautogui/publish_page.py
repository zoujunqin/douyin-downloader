"""
小红书创作者中心 - 发布页面操作模块

流程：
  1. 模拟真人随机浏览发布页面
  2. 截图比对视频上传完成标志（video_complete_high.png / video_complete_low.png）
     - 未匹配到则向上滚动页面，重试 3-5 次
  3. 稍微滚动页面，截图比对标题输入框（title_input.png）和描述标签区域（desc_tags.png），保存位置
  4. 点击标题位置，模拟犹豫，键盘输入标题
  5. 移动鼠标到描述和标签区域，模拟犹豫点击，输入描述和标签
  6. 设置内容备注
  7. 设置定时发布（首次+1天，第二次+2天），滚动查找 publish_schedule_button.png，
     点击开关，匹配 date.png 输入时间，随机滚动后点击 publish_button.png
  8. 等待发布完成
  9. 保存发布记录到数据库

公共入口: run(video_path, video_filename, account, config)
"""

import time
import random
import os
import sys
import sqlite3
import json
import re
from datetime import datetime, timedelta

# 确保 pyautogui 目录在 sys.path 中，以便兄弟模块（human 等）可被正确导入
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
if _THIS_DIR not in sys.path:
    sys.path.insert(0, _THIS_DIR)

import pyautogui

from human import HumanInput

# ─────────────── 配置 ───────────────

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
REF_IMG_DIR = os.path.join(_BASE_DIR, "ref_img", "publish_page")
SCREENSHOT_DIR = os.path.join(_BASE_DIR, "tmp")
DB_PATH = os.path.join(os.path.dirname(_BASE_DIR), "pyautogui", "publish_records.db")
DOWNLOAD_DB_PATH = os.path.join(os.path.dirname(_BASE_DIR), "dy_downloader.db")

os.makedirs(SCREENSHOT_DIR, exist_ok=True)
os.makedirs(REF_IMG_DIR, exist_ok=True)

pyautogui.FAILSAFE = True
pyautogui.PAUSE = 0.3

# ─────────────── 全局状态 ───────────────

_screenshot_counter = 100  # 从 100 开始，避免与 home_page 的截图编号冲突
hi = HumanInput()


# ─────────────── 工具函数 ───────────────

def _take_screenshot(label):
    global _screenshot_counter
    _screenshot_counter += 1
    filename = f"{SCREENSHOT_DIR}/step_{_screenshot_counter:02d}_{label}.png"
    pyautogui.screenshot().save(filename)
    print(f"  [截图] {filename}")


def _locate_ref_image(ref_image_name, confidence=0.7, max_attempts=1):
    """截图对比参考图片，返回匹配区域或 None"""
    ref_path = f"{REF_IMG_DIR}/{ref_image_name}"
    if not os.path.exists(ref_path):
        print(f"  [警告] 参考图片不存在: {ref_path}")
        return None

    for attempt in range(max_attempts):
        try:
            location = pyautogui.locateOnScreen(ref_path, confidence=confidence)
        except pyautogui.ImageNotFoundException:
            location = None
        if location is not None:
            print(f"  匹配成功: {ref_image_name} -> {location}")
            return location
        if attempt < max_attempts - 1:
            hi.pause(1.0, 2.0)

    print(f"  [警告] {ref_image_name} 匹配失败")
    return None


def _extract_title_from_filename(video_filename):
    """
    从视频文件名中提取标题。
    文件名格式: 2026-01-19_描述文字_标签ID_[标签列表].mp4
    提取日期后面的第一段描述文字作为标题。
    """
    name = os.path.splitext(video_filename)[0]
    name = re.sub(r"^\d{4}-\d{2}-\d{2}_", "", name)
    name = re.sub(r"_\[.*$", "", name)
    name = re.sub(r"_\d{15,}$", "", name)
    parts = name.split("_")
    title = parts[0] if parts else name
    if len(title) > 20:
        title = title[:20]
    return title


def _extract_tags_from_filename(video_filename):
    """
    从视频文件名中提取标签列表。
    文件名格式: 2026-01-19_描述文字_标签ID_[标签1,标签2,...].mp4
    返回标签列表，如 ["小猫", "可爱"]。
    """
    name = os.path.splitext(video_filename)[0]
    match = re.search(r"\[(.+)\]$", name)
    if match:
        tags_str = match.group(1)
        return [t.strip() for t in tags_str.split(",") if t.strip()]
    return []


def _query_download_record(video_filename):
    """
    从下载记录表 multi_account_download 中查询 title_2 和 tags。
    通过 file_name 字段匹配视频文件名。
    返回 (title_2, tags_list) 元组，查询失败时返回 (None, [])。
    """
    if not os.path.exists(DOWNLOAD_DB_PATH):
        print(f"  [警告] 下载记录数据库不存在: {DOWNLOAD_DB_PATH}")
        return None, []

    try:
        conn = sqlite3.connect(DOWNLOAD_DB_PATH)
        cursor = conn.execute(
            "SELECT title_2, tags FROM multi_account_download WHERE file_name = ?",
            (video_filename,),
        )
        row = cursor.fetchone()
        conn.close()

        if row:
            title_2 = row[0]
            tags_raw = row[1]
            tags_list = []
            if tags_raw:
                try:
                    tags_list = json.loads(tags_raw)
                except (json.JSONDecodeError, TypeError):
                    tags_list = []
            print(f"  [数据库] 查询到 title_2={title_2}, tags={tags_list}")
            return title_2, tags_list
        else:
            print(f"  [警告] 下载记录中未找到文件: {video_filename}")
            return None, []
    except Exception as e:
        print(f"  [错误] 查询下载记录失败: {e}")
        return None, []


# ─────────────── 数据库操作 ───────────────

def _save_publish_record(upload_dir, video_filename, video_path, xiaohongshu_account, days_to_add):
    """保存发布记录到数据库，以 upload_dir 关联"""
    scheduled_date = (datetime.now() + timedelta(days=days_to_add)).strftime("%Y-%m-%d")
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """INSERT INTO publish_records (upload_dir, video_filename, video_path, publish_time, xiaohongshu_account, scheduled_date)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (upload_dir, video_filename, video_path, datetime.now().isoformat(), xiaohongshu_account, scheduled_date),
    )
    conn.commit()
    conn.close()
    print(f"  [数据库] 发布记录已保存: {video_filename}, 账号: {xiaohongshu_account}, 定时日期: {scheduled_date}")


def _is_schedule_record_exists(xiaohongshu_account, scheduled_date):
    """检查是否存在相同账号和定时日期的发布记录"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.execute(
        """SELECT id FROM publish_records
           WHERE xiaohongshu_account = ? AND scheduled_date = ?
           LIMIT 1""",
        (xiaohongshu_account, scheduled_date),
    )
    exists = cursor.fetchone() is not None
    conn.close()
    return exists


# ─────────────── 发布步骤 ───────────────

def _step_browse_publish_page():
    """
    步骤1: 模拟真人随机浏览发布页面。
    进入页面后先像真人一样四处看看，移动鼠标，滚动页面。
    """
    print("  模拟真人浏览发布页面...")
    screen_w, screen_h = pyautogui.size()

    # 先随意浏览一下页面
    hi.browse_page(screen_w, screen_h)
    hi.pause(1.0, 2.0)

    # 穿插一些随机噪声动作
    hi.random_human_noise(screen_w, screen_h)
    hi.pause(0.5, 1.5)

    _take_screenshot("browse_publish_page")


def _step_wait_video_complete():
    """
    步骤2: 检测视频上传/处理完成。
    截图比对 video_complete_high.png 和 video_complete_low.png，
    任一匹配即视为完成。未匹配到则模拟真人向上滚动页面，重试 3-5 次。
    """
    print("  检测视频上传完成状态...")
    max_retries = random.randint(3, 5)

    for attempt in range(max_retries):
        print(f"  第 {attempt + 1}/{max_retries} 次检测...")
        _take_screenshot(f"video_complete_check_{attempt + 1}")

        # 尝试匹配两张参考图片，任一匹配即可
        high_box = _locate_ref_image("video_complete_high.png", confidence=0.7)
        if high_box:
            print("  视频处理完成（匹配 video_complete_high）")
            return True

        low_box = _locate_ref_image("video_complete_low.png", confidence=0.7)
        if low_box:
            print("  视频处理完成（匹配 video_complete_low）")
            return True

        # 未匹配到，模拟真人向上滚动页面
        print("  未检测到完成标志，模拟真人向上滚动页面...")
        screen_w, screen_h = pyautogui.size()

        # 先移动鼠标到页面中间区域再滚动
        cx = random.randint(int(screen_w * 0.3), int(screen_w * 0.7))
        cy = random.randint(int(screen_h * 0.3), int(screen_h * 0.7))
        hi.move_to(cx, cy, duration_range=(0.3, 0.6))
        hi.pause(0.3, 0.8)

        # 向上滚动
        hi.scroll(random.randint(150, 400))
        hi.pause(1.5, 3.0)

        # 偶尔穿插一些空闲微抖动
        if random.random() < 0.4:
            hi.idle_jitter(duration=random.uniform(0.5, 1.5))

    print("  [警告] 视频完成状态检测超时，继续尝试填写信息...")
    _take_screenshot("video_complete_timeout")
    return False


def _step_locate_title_and_desc():
    """
    步骤3: 稍微滚动页面，截图比对标题输入框和描述标签区域，保存位置。
    返回 (title_box, desc_tags_box) 元组。
    """
    print("  定位标题输入框和描述标签区域...")
    screen_w, screen_h = pyautogui.size()

    # 稍微滚动一下页面，让标题和描述区域可见
    cx = random.randint(int(screen_w * 0.3), int(screen_w * 0.7))
    cy = random.randint(int(screen_h * 0.3), int(screen_h * 0.7))
    hi.move_to(cx, cy, duration_range=(0.3, 0.6))
    hi.pause(0.3, 0.6)

    # 稍微向下滚动，让标题和描述区域可见
    scroll_amount = random.randint(50, 150)
    hi.scroll(-scroll_amount)
    hi.pause(1.0, 2.0)

    _take_screenshot("locate_title_desc")

    # 截图比对标题输入框
    title_box = _locate_ref_image("title_input.png", confidence=0.6)
    if title_box:
        print(f"  标题输入框位置: left={title_box.left}, top={title_box.top}, "
              f"width={title_box.width}, height={title_box.height}")

    # 截图比对描述和标签区域
    desc_tags_box = _locate_ref_image("desc_tags.png", confidence=0.6)
    if desc_tags_box:
        print(f"  描述标签区域位置: left={desc_tags_box.left}, top={desc_tags_box.top}, "
              f"width={desc_tags_box.width}, height={desc_tags_box.height}")

    return title_box, desc_tags_box


def _step_fill_title(title_box, video_filename, title_2=None):
    """
    步骤4: 点击标题位置，模拟真人犹豫，然后键盘输入标题。
    优先使用下载记录中的 title_2，否则从文件名提取。
    """
    if title_2:
        title = title_2
    else:
        title = _extract_title_from_filename(video_filename)
    print(f"  准备填写标题: {title}")

    if title_box:
        cx = title_box.left + title_box.width // 2
        cy = title_box.top + title_box.height // 2
    else:
        # 兜底位置
        screen_w, screen_h = pyautogui.size()
        cx = int(screen_w * 0.5)
        cy = int(screen_h * 0.25)
        print(f"  兜底标题位置: ({cx}, {cy})")

    # 模拟真人犹豫后点击标题输入框
    print("  模拟犹豫后点击标题输入框...")
    hi.hesitate_click(cx, cy)
    hi.pause(0.5, 1.0)

    # 全选清除默认内容
    pyautogui.hotkey("ctrl", "a")
    hi.pause(0.2, 0.5)

    # 模拟真人键盘逐字输入标题
    print(f"  键盘输入标题: {title}")
    hi.type_text(title)
    hi.pause(0.8, 1.5)

    _take_screenshot("after_title_input")


def _step_fill_desc_and_tags(desc_tags_box, video_filename, title_2=None, db_tags=None):
    """
    步骤5: 移动鼠标到描述和标签区域，模拟真人犹豫点击，输入描述和标签。
    描述内容 = title_2 + 下载记录中的 tags。
    标签从下载记录中获取，每输入一个 #tag 后：
      - 等待随机 1-5s
      - 模拟真人按键盘向下键随机 1-8 次
      - 模拟真人按键盘向上键随机 1-4 次
    """
    print("  准备填写描述和标签...")

    if desc_tags_box:
        cx = desc_tags_box.left + desc_tags_box.width // 2
        cy = desc_tags_box.top + desc_tags_box.height // 2
    else:
        # 兜底位置（描述区域通常在标题下方）
        screen_w, screen_h = pyautogui.size()
        cx = int(screen_w * 0.5)
        cy = int(screen_h * 0.45)
        print(f"  兜底描述标签位置: ({cx}, {cy})")

    # 模拟真人移动鼠标到描述区域
    print("  模拟真人移动鼠标到描述标签区域...")
    hi.move_to(cx, cy, duration_range=(0.5, 1.0))
    hi.pause(0.5, 1.2)

    # 模拟犹豫后点击
    print("  模拟犹豫后点击描述输入框...")
    hi.hesitate_click(cx, cy)
    hi.pause(0.5, 1.0)

    # 描述文字使用 title_2
    tags = db_tags if db_tags else _extract_tags_from_filename(video_filename)
    desc_text = title_2 if title_2 else _extract_title_from_filename(video_filename)

    print(f"  键盘输入描述: {desc_text}")
    hi.type_text(desc_text)
    hi.pause(0.8, 1.5)

    # 输入标签（# 开头）
    if not tags:
        tags = []

    print(f"  输入标签: {tags}")
    for tag in tags:
        hi.pause(0.3, 0.8)
        tag_text = f" #{tag}"
        hi.type_text(tag_text)

        # 等待随机 1-3s
        wait_sec = random.uniform(1.0, 3.0)
        print(f"  标签 #{tag} 输入完毕，等待 {wait_sec:.1f}s...")
        time.sleep(wait_sec)

        # 模拟真人按键盘向下键随机 1-8 次
        down_count = random.randint(1, 8)
        print(f"  按下向下键 {down_count} 次...")
        for _ in range(down_count):
            hi.press("down")
            time.sleep(random.uniform(0.08, 0.25))

        hi.pause(0.2, 0.5)

        # 模拟真人按键盘向上键随机 1-4 次
        up_count = random.randint(1, 4)
        print(f"  按上向上键 {up_count} 次...")
        for _ in range(up_count):
            hi.press("up")
            time.sleep(random.uniform(0.08, 0.25))

        hi.pause(0.2, 0.5)

        # 按回车确认选中的标签
        pyautogui.press("enter")
        hi.pause(0.3, 0.6)

    hi.pause(0.5, 1.0)
    _take_screenshot("after_desc_tags_input")


def _step_set_content_remark():
    """
    步骤6: 设置内容备注。
    截图匹配 content_remark.png，如果没匹配到但匹配到了 title_set_face.png 或 hdht.png，
    则向下滚动后重试，最多重试 5 次。
    匹配到后点击该区域，再截图匹配 content_remark_select.png 并点击。
    """
    print("  定位内容备注区域...")
    max_retries = 5
    screen_w, screen_h = pyautogui.size()

    for attempt in range(max_retries):
        print(f"  第 {attempt + 1}/{max_retries} 次匹配 content_remark...")
        _take_screenshot(f"content_remark_check_{attempt + 1}")

        remark_box = _locate_ref_image("content_remark.png", confidence=0.6)
        if remark_box:
            print("  匹配到 content_remark，点击该区域...")
            cx = remark_box.left + remark_box.width // 2
            cy = remark_box.top + remark_box.height // 2
            hi.click(cx, cy)
            hi.pause(0.8, 1.5)

            # 截图匹配 content_remark_select.png
            _take_screenshot("content_remark_select_check")
            select_box = _locate_ref_image("content_remark_select.png", confidence=0.6)
            if select_box:
                print("  匹配到 content_remark_select，点击...")
                sx = select_box.left + select_box.width // 2
                sy = select_box.top + select_box.height // 2
                hi.click(sx, sy)
                hi.pause(0.5, 1.0)
            else:
                print("  [警告] 未匹配到 content_remark_select")

            _take_screenshot("after_content_remark")
            return True

        # 没匹配到 content_remark，检查是否在页面中（通过 title_set_face 或 hdht 判断）
        face_box = _locate_ref_image("title_set_face.png", confidence=0.6)
        hdht_box = _locate_ref_image("hdht.png", confidence=0.6)
        if face_box or hdht_box:
            print("  匹配到 title_set_face 或 hdht，说明在页面中但需要向下滚动...")
            cx = random.randint(int(screen_w * 0.3), int(screen_w * 0.7))
            cy = random.randint(int(screen_h * 0.3), int(screen_h * 0.7))
            hi.move_to(cx, cy, duration_range=(0.3, 0.6))
            hi.pause(0.3, 0.6)
            hi.scroll(-random.randint(200, 400))
            hi.pause(1.0, 2.0)
        else:
            print("  [警告] 未匹配到任何参考图片，停止重试")
            break

    print("  [警告] content_remark 匹配超时，跳过此步骤")
    _take_screenshot("content_remark_timeout")
    return False


def _step_set_schedule(days_to_add):
    """
    步骤7: 设置定时发布。
    1. 等待 1-3s
    2. 向下滚动直到匹配 publish_schedule_button.png
    3. 点击开关位置
    4. 截图匹配 date.png，点击左边一点点
    5. 根据 days_to_add 计算日期：今天 + days_to_add 天
    6. 键盘输入 YYYY-MM-DD hh:mm 格式时间
    7. 模拟真人随机上下滚动
    8. 截图匹配 publish_button.png，模拟真人点击发布
    """
    print("  设置定时发布...")
    screen_w, screen_h = pyautogui.size()

    print(f"  days_to_add: {days_to_add}")

    # 等待 1-3s
    wait_time = random.uniform(1.0, 3.0)
    print(f"  等待 {wait_time:.1f}s...")
    time.sleep(wait_time)

    # 向下滚动直到匹配 publish_schedule_button.png
    print("  向下滚动查找定时发布按钮...")
    max_scroll_attempts = 10
    for scroll_attempt in range(max_scroll_attempts):
        _take_screenshot(f"schedule_scroll_{scroll_attempt + 1}")

        schedule_box = _locate_ref_image("publish_schedule_button.png", confidence=0.7)
        if schedule_box:
            print("  匹配到 publish_schedule_button，点击开关位置...")
            # 点击开关位置（按钮中心偏左）
            cx = schedule_box.left + int(schedule_box.width * 0.3)
            cy = schedule_box.top + schedule_box.height // 2
            hi.click(cx, cy)
            hi.pause(0.8, 1.5)
            break

        # 未匹配到，向下滚动
        cx = random.randint(int(screen_w * 0.3), int(screen_w * 0.7))
        cy = random.randint(int(screen_h * 0.5), int(screen_h * 0.7))
        hi.move_to(cx, cy, duration_range=(0.3, 0.6))
        hi.pause(0.3, 0.5)
        hi.scroll(-random.randint(200, 400))
        hi.pause(1.0, 2.0)
    else:
        print("  [警告] 未找到定时发布按钮，跳过定时设置")
        _take_screenshot("schedule_button_not_found")
        return False

    # 截图匹配 date.png，点击左边一点点
    print("  查找日期选择器...")
    _take_screenshot("date_selector_check")
    date_box = _locate_ref_image("date.png", confidence=0.7)
    if date_box:
        print("  匹配到 date.png，点击左边...")
        # 点击 date.png 左边一点的位置
        cx = date_box.left - random.randint(1, 60)
        cy = date_box.top + date_box.height // 2
        hi.click(cx, cy)
        hi.pause(0.5, 1.0)

        # 全选清除原有日期
        print("  全选清除原有日期...")
        pyautogui.hotkey("ctrl", "a")
        hi.pause(0.3, 0.6)
    else:
        print("  [警告] 未匹配到 date.png")

    # 计算目标日期
    target_date = datetime.now() + timedelta(days=days_to_add)
    date_str = target_date.strftime("%Y-%m-%d")

    # 生成随机时间（上午 9:00 - 下午 10:00 之间）
    hour = random.randint(9, 22)
    minute = random.randint(0, 59)
    time_str = f"{hour:02d}:{minute:02d}"

    full_datetime = f"{date_str} {time_str}"
    print(f"  输入定时时间: {full_datetime}")

    # 键盘输入时间
    hi.type_text(full_datetime)
    hi.pause(0.5, 1.0)

    # 截图匹配 title_more.png 位置，点击
    title_more_box = _locate_ref_image("title_more.png", confidence=0.8)
    if title_more_box:
        print("  匹配到 title_more.png，点击...")
        hi.click(title_more_box.left + title_more_box.width // 2,
                 title_more_box.top + title_more_box.height // 2)
        hi.pause(0.5, 1.0)

    # 模拟真人向上滚动直到匹配 title_set_face.png
    print("  向上滚动查找 title_set_face...")
    for scroll_attempt in range(10):
        face_box = _locate_ref_image("title_set_face.png", confidence=0.6)
        if face_box:
            print("  匹配到 title_set_face")
            break
        hi.scroll(300)
        hi.pause(0.5, 1.0)

    # 模拟真人向下滚动直到匹配 publish_schedule_button.png
    print("  向下滚动查找 publish_schedule_button...")
    for scroll_attempt in range(10):
        _take_screenshot(f"schedule_final_scroll_{scroll_attempt + 1}")
        schedule_box = _locate_ref_image("publish_schedule_button.png", confidence=0.7)
        if schedule_box:
            print("  匹配到 publish_schedule_button")
            break
        hi.scroll(-300)
        hi.pause(0.5, 1.0)

    _take_screenshot("after_schedule_set")

    return True


def _step_click_publish():
    """
    步骤7: 点击「发布」按钮。
    """
    print("  点击「发布」按钮...")

    publish_box = _locate_ref_image("publish_button.png", confidence=0.7)
    if publish_box:
        cx = publish_box.left + publish_box.width // 2
        cy = publish_box.top + publish_box.height // 2
        hi.click(cx, cy)
    else:
        screen_w, screen_h = pyautogui.size()
        cx = int(screen_w * 0.85)
        cy = int(screen_h * 0.9)
        print(f"  兜底点击「发布」按钮: ({cx}, {cy})")
        hi.click(cx, cy)

    hi.pause(1.0, 2.0)
    _take_screenshot("after_publish_click")


def _step_wait_publish_complete():
    """
    步骤7: 等待发布完成。
    """
    print("  等待发布完成...")
    hi.impatient_waiting(duration_range=(3.0, 6.0))
    _take_screenshot("publish_result")

    success_box = _locate_ref_image("publish_success.png", confidence=0.7)
    if success_box:
        print("  发布成功!")
        return True

    print("  发布流程已执行，请通过截图确认结果")
    return True


# ─────────────── 公共入口 ───────────────

def run(video_path, video_filename, upload_dir, config, days_to_add=1):
    """
    公共入口：浏览页面 → 等待上传完成 → 定位输入区域 → 填写标题 → 填写描述标签 → 发布 → 保存记录。

    参数:
        video_path: 视频文件完整路径
        video_filename: 视频文件名
        upload_dir: 视频上传目录路径（用于关联发布记录）
        config: 配置字典
        days_to_add: 定时发布时，距离今天的天数（默认1）
    """
    print("=" * 50)
    print("小红书发布页 - 填写信息并发布")
    print("=" * 50)
    print(f"  视频: {video_filename}")
    print(f"  上传目录: {upload_dir}")

    print("\n步骤1: 模拟真人浏览发布页面...")
    _step_browse_publish_page()

    print("\n步骤2: 检测视频上传完成...")
    _step_wait_video_complete()

    print("\n查询下载记录中的 title_2 和 tags...")
    title_2, db_tags = _query_download_record(video_filename)

    print("\n步骤3: 定位标题和描述标签区域...")
    title_box, desc_tags_box = _step_locate_title_and_desc()

    print("\n步骤4: 填写标题...")
    _step_fill_title(title_box, video_filename, title_2=title_2)

    print("\n步骤5: 填写描述和标签...")
    _step_fill_desc_and_tags(desc_tags_box, video_filename, title_2=title_2, db_tags=db_tags)

    print("\n步骤6: 设置内容备注...")
    _step_set_content_remark()

    print("\n步骤7: 设置定时发布...")
    _step_set_schedule(days_to_add)

    print("\n步骤8: 点击发布...")
    _step_click_publish()

    print("\n步骤9: 等待发布完成...")
    _step_wait_publish_complete()

    print("\n步骤10: 保存发布记录...")
    xiaohongshu_account = config.get("xiaohongshu_account", "未知账号")
    _save_publish_record(upload_dir, video_filename, video_path, xiaohongshu_account, days_to_add)

    print("\n发布流程执行完毕!")

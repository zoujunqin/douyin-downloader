#!/usr/bin/env python3
import asyncio
import json
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

# 确保 pyautogui 目录在 sys.path 中，以便正确导入其中的模块
_PYAUTOGUI_DIR = str(Path(__file__).parent / 'pyautogui')
if _PYAUTOGUI_DIR not in sys.path:
    sys.path.insert(0, _PYAUTOGUI_DIR)


def switch_wifi(wifi_name: str) -> bool:
    """切换到指定WiFi。成功返回True，失败返回False。"""
    if not wifi_name or not wifi_name.strip():
        return False
    wifi_name = wifi_name.strip()
    try:
        result = subprocess.run(
            ['netsh', 'wlan', 'connect', f'name={wifi_name}'],
            capture_output=True,
            text=True,
            timeout=30,
        )
        return result.returncode == 0
    except Exception as e:
        print(f"切换WiFi失败: {e}")
        return False


def is_wifi_connected(wifi_name: str) -> bool:
    """检查当前是否已连接到指定WiFi。"""
    if not wifi_name or not wifi_name.strip():
        return False
    wifi_name = wifi_name.strip()
    try:
        result = subprocess.run(
            ['netsh', 'wlan', 'show', 'interfaces'],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0:
            return wifi_name in result.stdout
        return False
    except Exception:
        return False


async def main():
    config_path = Path(__file__).parent / 'multiple_account_config.json'
    if not config_path.exists():
        print(f"配置文件不存在: {config_path}")
        return

    with open(config_path, 'r', encoding='utf-8') as f:
        accounts = json.load(f)

    if not isinstance(accounts, list):
        print("配置文件必须是JSON数组")
        return

    for idx, entry in enumerate(accounts, 1):
        account = entry.get('xiaohongshu_account', '未知账号')
        print(f"\n{'=' * 60}")
        print(f"[{idx}/{len(accounts)}] 处理账号: {account}")
        print(f"{'=' * 60}")

        # wifi = entry.get('wifi', '')
        # if wifi and wifi.strip():
        #     wifi = wifi.strip()
        #     print(f"[{idx}/{len(accounts)}] 切换WiFi: {wifi}")
        #     if not is_wifi_connected(wifi):
        #         if not switch_wifi(wifi):
        #             print(f"[{idx}/{len(accounts)}] WiFi切换失败: {wifi}，跳过账号")
        #             continue
        #         await asyncio.sleep(3)
        #     else:
        #         print(f"[{idx}/{len(accounts)}] WiFi已连接: {wifi}")

        # 按 days_per_time 循环定时发布
        days_per_time = entry.get("days_per_time", 1)
        for schedule_idx in range(days_per_time):
            days_to_add = schedule_idx + 1  # 第1次+1天，第2次+2天，第3次+3天
            scheduled_date = (datetime.now() + timedelta(days=days_to_add)).strftime("%Y-%m-%d")
            account = entry.get('xiaohongshu_account', '未知账号')
            print(f"\n  [{idx}/{len(accounts)}] 第 {schedule_idx + 1}/{days_per_time} 次定时发布，days_to_add={days_to_add}, 定时日期={scheduled_date}")

            # 检查是否已存在相同账号和定时日期的发布记录
            import publish_page
            if publish_page._is_schedule_record_exists(account, scheduled_date):
                print(f"  [{idx}/{len(accounts)}] 账号 {account} 在 {scheduled_date} 已存在发布记录，跳过本次发布")
                continue

            loop_start_time = time.time()

            import home_page
            home_page_start = time.time()
            result = home_page.run(config=entry)
            home_page_duration = time.time() - home_page_start
            print(f"  [计时] home_page 耗时: {home_page_duration:.1f}s")

            if result is None:
                print(f"  [{idx}/{len(accounts)}] 无可上传视频，跳过本次发布")
                continue

            video_path = result.get("video_path")
            video_filename = result.get("video_filename")
            if video_path and video_filename:
                import publish_page
                publish_start = time.time()
                publish_page.run(
                    video_path=video_path,
                    video_filename=video_filename,
                    upload_dir=result.get("upload_dir", ""),
                    config=entry,
                    days_to_add=days_to_add,
                )
                publish_duration = time.time() - publish_start
                total_duration = time.time() - loop_start_time
                print(f"  [计时] publish_page 耗时: {publish_duration:.1f}s")
                print(f"  [计时] 本次发布总耗时: {total_duration:.1f}s")
            else:
                print(f"  [{idx}/{len(accounts)}] 未选择视频，跳过发布步骤")


if __name__ == '__main__':
    asyncio.run(main())

r"""清理 multi_account_download 表中指定 video_dir_path 的下载记录。

用法:
    # 交互式：从 multiple_account_config.json 中选择要清理的目录
    python clean_multi_account.py

    # 直接指定目录路径
    python clean_multi_account.py -d "C:\Users\11714\Desktop\小红书二创\小猫\单次机器素材"

    # 指定数据库路径
    python clean_multi_account.py --db my_database.db
"""

import argparse
import json
import re
import shutil
import sqlite3
import sys
from pathlib import Path


DEFAULT_DB = 'dy_downloader.db'
DEFAULT_CONFIG = 'multiple_account_config.json'
DEFAULT_HISTORY = 'douyin_users_history.json'


def get_connection(db_path: str) -> sqlite3.Connection:
    if not Path(db_path).exists():
        print(f"错误: 数据库文件不存在: {db_path}")
        sys.exit(1)
    return sqlite3.connect(db_path)


def _load_config(config_path: str) -> list:
    """加载多账号配置文件，返回条目列表。"""
    if not Path(config_path).exists():
        return []
    try:
        with open(config_path, 'r', encoding='utf-8') as f:
            accounts = json.load(f)
        return accounts if isinstance(accounts, list) else []
    except Exception:
        return []


def _find_config_entry(config_path: str, video_dir_path: str) -> dict | None:
    """根据 video_dir_path 查找配置条目。"""
    for entry in _load_config(config_path):
        if entry.get('video_dir_path', '').strip() == video_dir_path:
            return entry
    return None


def _load_history(history_path: str) -> list:
    """加载历史用户记录。"""
    if not Path(history_path).exists():
        return []
    try:
        with open(history_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _strip_url_query(url: str) -> str:
    """去掉 URL 查询参数，只保留 scheme + host + path。"""
    return re.sub(r'\?.*$', '', url.strip())


def _extract_sec_uid_from_url(url: str) -> str:
    """从用户主页 URL 中提取 sec_uid。"""
    match = re.search(r'/user/([A-Za-z0-9_-]+)', url)
    return match.group(1) if match else ""


def _get_sec_uids_from_entry(entry: dict) -> list:
    """从配置条目中提取所有 douyin_users 的 sec_uid 列表。"""
    sec_uids = []
    for user in entry.get('douyin_users', []):
        raw = user.strip() if isinstance(user, str) else user.get('user_page', '').strip()
        url = _strip_url_query(raw) if raw else ''
        uid = _extract_sec_uid_from_url(url)
        if uid:
            sec_uids.append(uid)
    return sec_uids


def _save_users_to_history(douyin_users: list, history_path: str):
    """将抖音用户列表追加到历史文件中（去重，去掉查询参数）。"""
    history = _load_history(history_path)
    existing = set(history)
    added = 0
    for user in douyin_users:
        raw = user.strip() if isinstance(user, str) else user.get('user_page', '').strip()
        url = _strip_url_query(raw) if raw else ''
        if url and url not in existing:
            history.append(url)
            existing.add(url)
            added += 1
    with open(history_path, 'w', encoding='utf-8') as f:
        json.dump(history, f, ensure_ascii=False, indent=2)
    return added


def _clear_directory(dir_path: str) -> int:
    """清空目录中的所有文件和子目录，返回删除的项目数。"""
    p = Path(dir_path)
    if not p.exists() or not p.is_dir():
        return 0
    count = 0
    for item in p.iterdir():
        if item.is_file():
            item.unlink()
            count += 1
        elif item.is_dir():
            shutil.rmtree(item)
            count += 1
    return count


def show_config_dirs(config_path: str, conn: sqlite3.Connection):
    """显示配置文件中所有条目及其数据库记录数（按 sec_uid 统计）。"""
    accounts = _load_config(config_path)
    if not accounts:
        print(f"配置文件 {config_path} 为空或不存在。")
        return []

    entries = []
    print(f"\n{'序号':<6}{'记录数':<8}{'用户数':<8}{'目录路径'}")
    print("-" * 80)
    for i, entry in enumerate(accounts, 1):
        vdp = entry.get('video_dir_path', '').strip()
        if not vdp:
            continue
        sec_uids = _get_sec_uids_from_entry(entry)
        if sec_uids:
            placeholders = ','.join('?' for _ in sec_uids)
            cursor = conn.execute(
                f'SELECT COUNT(*) FROM multi_account_download WHERE sec_uid IN ({placeholders})',
                sec_uids,
            )
            count = cursor.fetchone()[0]
        else:
            count = 0
        print(f"{i:<6}{count:<8}{len(sec_uids):<8}{vdp}")
        entries.append((vdp, entry))
    print()
    return entries


def clean_by_dir(conn: sqlite3.Connection, video_dir_path: str, config_path: str,
                 history_path: str = DEFAULT_HISTORY):
    """删除指定条目中所有 douyin_users 的下载记录（按 sec_uid），并清空关联目录。"""
    # 查找配置条目以获取 douyin_users 和关联目录
    entry = _find_config_entry(config_path, video_dir_path)
    if not entry:
        print(f"在配置文件中没有找到目录 [{video_dir_path}] 对应的条目。")
        return

    sec_uids = _get_sec_uids_from_entry(entry)
    if not sec_uids:
        print(f"条目 [{video_dir_path}] 中没有有效的 douyin_users。")
        return

    # 统计这些用户的下载记录数
    placeholders = ','.join('?' for _ in sec_uids)
    cursor = conn.execute(
        f'SELECT COUNT(*) FROM multi_account_download WHERE sec_uid IN ({placeholders})',
        sec_uids,
    )
    count = cursor.fetchone()[0]

    dirs_to_clear = [video_dir_path]
    for key in ('increased_video_dir_path', 'xiaohongshu_video_upload_dir_path'):
        d = entry.get(key, '').strip()
        if d:
            dirs_to_clear.append(d)

    print(f"\n即将执行以下操作:")
    print(f"  1. 删除 {len(sec_uids)} 个抖音用户的 {count} 条数据库下载记录")
    print(f"  2. 清空以下目录中的所有文件:")
    for d in dirs_to_clear:
        exists = "存在" if Path(d).exists() else "不存在"
        print(f"     - {d}  ({exists})")

    if count == 0:
        print("\n数据库中没有下载记录，仅清理目录。")

    # 第一次确认
    confirm1 = input("\n确认执行以上操作? (y/N): ").strip().lower()
    if confirm1 != 'y':
        print("已取消。")
        return

    # 第二次确认
    confirm2 = input("此操作不可撤销! 再次确认? (yes/N): ").strip().lower()
    if confirm2 != 'yes':
        print("已取消。")
        return

    # 删除数据库记录（按 sec_uid）
    if count > 0:
        conn.execute(
            f'DELETE FROM multi_account_download WHERE sec_uid IN ({placeholders})',
            sec_uids,
        )
        conn.commit()
        print(f"\n已删除 {count} 条数据库记录。")

    # 清空关联目录
    for d in dirs_to_clear:
        cleared = _clear_directory(d)
        if cleared > 0:
            print(f"已清空目录: {d} ({cleared} 项)")
        elif Path(d).exists():
            print(f"目录已为空: {d}")
        else:
            print(f"目录不存在，跳过: {d}")

    # 将当前 douyin_users 写入历史文件
    douyin_users = entry.get('douyin_users', [])
    if douyin_users:
        added = _save_users_to_history(douyin_users, history_path)
        print(f"\n已将 {added} 个抖音用户写入历史记录: {history_path}")

    print("\n" + "=" * 60)
    print("提示: 请更换新一批的抖音博主账号用于视频批量下载!")
    print("请编辑 multiple_account_config.json 中对应条目的 douyin_users 列表。")
    print("=" * 60)


def interactive_mode(conn: sqlite3.Connection, config_path: str,
                     history_path: str = DEFAULT_HISTORY):
    """交互式选择要清理的目录（仅从配置文件列出）。"""
    entries = show_config_dirs(config_path, conn)
    if not entries:
        return

    choice = input("请输入要清理的序号 (多个用逗号分隔): ").strip()

    try:
        indices = [int(x.strip()) for x in choice.split(',')]
    except ValueError:
        print("输入无效。")
        return

    for idx in indices:
        if 1 <= idx <= len(entries):
            clean_by_dir(conn, entries[idx - 1][0], config_path, history_path)
        else:
            print(f"序号 {idx} 超出范围，跳过。")


def main():
    parser = argparse.ArgumentParser(
        description='清理 multi_account_download 表中指定 video_dir_path 的下载记录'
    )
    parser.add_argument(
        '-d', '--dir',
        dest='video_dir_path',
        help='要清理的 video_dir_path 目录路径',
    )
    parser.add_argument(
        '--db',
        default=DEFAULT_DB,
        help=f'数据库路径 (默认: {DEFAULT_DB})',
    )
    parser.add_argument(
        '--config',
        default=DEFAULT_CONFIG,
        help=f'多账号配置文件路径 (默认: {DEFAULT_CONFIG})',
    )
    parser.add_argument(
        '--history',
        default=DEFAULT_HISTORY,
        help=f'抖音用户历史记录文件路径 (默认: {DEFAULT_HISTORY})',
    )

    args = parser.parse_args()
    conn = get_connection(args.db)

    try:
        if args.video_dir_path:
            clean_by_dir(conn, args.video_dir_path, args.config, args.history)
        else:
            interactive_mode(conn, args.config, args.history)
    finally:
        conn.close()


if __name__ == '__main__':
    main()

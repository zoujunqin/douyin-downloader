r"""清理 multi_account_download 表中指定 video_dir_path 的下载记录。

用法:
    # 交互式：从 multiple_account_config.json 中选择要清理的目录
    python clean_multi_account.py

    # 直接指定目录路径
    python clean_multi_account.py -d "C:\Users\11714\Desktop\小红书二创\小猫\单次机器素材"

    # 清理所有目录的记录
    python clean_multi_account.py --all

    # 指定数据库路径
    python clean_multi_account.py --db my_database.db
"""

import argparse
import json
import sqlite3
import sys
from pathlib import Path


DEFAULT_DB = 'dy_downloader.db'
DEFAULT_CONFIG = 'multiple_account_config.json'


def get_connection(db_path: str) -> sqlite3.Connection:
    if not Path(db_path).exists():
        print(f"错误: 数据库文件不存在: {db_path}")
        sys.exit(1)
    return sqlite3.connect(db_path)


def show_all_dirs(conn: sqlite3.Connection):
    """显示数据库中所有 video_dir_path 及其记录数。"""
    cursor = conn.execute(
        'SELECT video_dir_path, COUNT(*) FROM multi_account_download GROUP BY video_dir_path ORDER BY video_dir_path'
    )
    rows = cursor.fetchall()
    if not rows:
        print("数据库中没有多账号下载记录。")
        return rows
    print(f"\n{'序号':<6}{'记录数':<8}{'目录路径'}")
    print("-" * 80)
    for i, (path, count) in enumerate(rows, 1):
        print(f"{i:<6}{count:<8}{path}")
    print()
    return rows


def clean_by_dir(conn: sqlite3.Connection, video_dir_path: str):
    """删除指定 video_dir_path 的所有记录。"""
    cursor = conn.execute(
        'SELECT COUNT(*) FROM multi_account_download WHERE video_dir_path = ?',
        (video_dir_path,),
    )
    count = cursor.fetchone()[0]
    if count == 0:
        print(f"没有找到目录 [{video_dir_path}] 的下载记录。")
        return

    print(f"即将删除目录 [{video_dir_path}] 的 {count} 条下载记录。")
    confirm = input("确认删除? (y/N): ").strip().lower()
    if confirm != 'y':
        print("已取消。")
        return

    conn.execute(
        'DELETE FROM multi_account_download WHERE video_dir_path = ?',
        (video_dir_path,),
    )
    conn.commit()
    print(f"已删除 {count} 条记录。")


def clean_all(conn: sqlite3.Connection):
    """删除所有多账号下载记录。"""
    cursor = conn.execute('SELECT COUNT(*) FROM multi_account_download')
    count = cursor.fetchone()[0]
    if count == 0:
        print("数据库中没有多账号下载记录。")
        return

    print(f"即将删除全部 {count} 条多账号下载记录。")
    confirm = input("确认删除? (y/N): ").strip().lower()
    if confirm != 'y':
        print("已取消。")
        return

    conn.execute('DELETE FROM multi_account_download')
    conn.commit()
    print(f"已删除全部 {count} 条记录。")


def interactive_mode(conn: sqlite3.Connection, config_path: str):
    """交互式选择要清理的目录。"""
    # 优先从配置文件加载目录列表
    dirs_from_config = []
    if Path(config_path).exists():
        try:
            with open(config_path, 'r', encoding='utf-8') as f:
                accounts = json.load(f)
            dirs_from_config = [
                entry.get('video_dir_path', '').strip()
                for entry in accounts
                if entry.get('video_dir_path', '').strip()
            ]
        except Exception:
            pass

    # 展示数据库中所有目录
    rows = show_all_dirs(conn)
    if not rows:
        return

    db_dirs = [r[0] for r in rows]

    # 合并配置文件中有但数据库中没记录的目录（提示用户）
    config_only = [d for d in dirs_from_config if d not in db_dirs]
    if config_only:
        print("以下目录在配置文件中但数据库中无记录:")
        for d in config_only:
            print(f"  - {d}")
        print()

    choice = input("请输入要清理的序号 (多个用逗号分隔，输入 all 清理全部): ").strip()
    if choice.lower() == 'all':
        clean_all(conn)
        return

    try:
        indices = [int(x.strip()) for x in choice.split(',')]
    except ValueError:
        print("输入无效。")
        return

    for idx in indices:
        if 1 <= idx <= len(rows):
            clean_by_dir(conn, rows[idx - 1][0])
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
        '--all',
        action='store_true',
        help='清理所有多账号下载记录',
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
        '--yes', '-y',
        action='store_true',
        help='跳过确认提示，直接执行删除',
    )

    args = parser.parse_args()
    conn = get_connection(args.db)

    try:
        if args.all:
            if args.yes:
                cursor = conn.execute('SELECT COUNT(*) FROM multi_account_download')
                count = cursor.fetchone()[0]
                conn.execute('DELETE FROM multi_account_download')
                conn.commit()
                print(f"已删除全部 {count} 条记录。")
            else:
                clean_all(conn)
        elif args.video_dir_path:
            if args.yes:
                cursor = conn.execute(
                    'SELECT COUNT(*) FROM multi_account_download WHERE video_dir_path = ?',
                    (args.video_dir_path,),
                )
                count = cursor.fetchone()[0]
                conn.execute(
                    'DELETE FROM multi_account_download WHERE video_dir_path = ?',
                    (args.video_dir_path,),
                )
                conn.commit()
                print(f"已删除目录 [{args.video_dir_path}] 的 {count} 条记录。")
            else:
                clean_by_dir(conn, args.video_dir_path)
        else:
            interactive_mode(conn, args.config)
    finally:
        conn.close()


if __name__ == '__main__':
    main()

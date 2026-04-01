import asyncio
import argparse
import json
import logging
import re
import shutil
import sys
from pathlib import Path
from typing import Any

from config import ConfigLoader
from auth import CookieManager
from storage import Database, FileManager
from control import QueueManager, RateLimiter, RetryHandler
from core import DouyinAPIClient, URLParser, DownloaderFactory
from cli.progress_display import ProgressDisplay
from utils.logger import setup_logger, set_console_log_level

logger = setup_logger('CLI')
display = ProgressDisplay()


def _as_bool(value: Any, default: bool = True) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


async def download_url(
    url: str,
    config: ConfigLoader,
    cookie_manager: CookieManager,
    database: Database = None,
    progress_reporter: ProgressDisplay = None,
):
    if progress_reporter:
        progress_reporter.advance_step("初始化", "创建下载组件")
    file_manager = FileManager(config.get('path'))
    rate_limiter = RateLimiter(max_per_second=float(config.get('rate_limit', 2) or 2))
    retry_handler = RetryHandler(max_retries=config.get('retry_times', 3))
    queue_manager = QueueManager(max_workers=int(config.get('thread', 5) or 5))

    original_url = url

    async with DouyinAPIClient(
        cookie_manager.get_cookies(),
        proxy=config.get("proxy"),
    ) as api_client:
        if progress_reporter:
            progress_reporter.advance_step("解析链接", "检查短链并解析 URL")
        if url.startswith('https://v.douyin.com'):
            resolved_url = await api_client.resolve_short_url(url)
            if resolved_url:
                url = resolved_url
            else:
                if progress_reporter:
                    progress_reporter.update_step("解析链接", "短链解析失败")
                display.print_error(f"Failed to resolve short URL: {url}")
                return None

        parsed = URLParser.parse(url)
        if not parsed:
            if progress_reporter:
                progress_reporter.update_step("解析链接", "URL 解析失败")
            display.print_error(f"Failed to parse URL: {url}")
            return None

        if not progress_reporter:
            display.print_info(f"URL type: {parsed['type']}")
        if progress_reporter:
            progress_reporter.advance_step("创建下载器", f"URL 类型: {parsed['type']}")

        downloader = DownloaderFactory.create(
            parsed['type'],
            config,
            api_client,
            file_manager,
            cookie_manager,
            database,
            rate_limiter,
            retry_handler,
            queue_manager,
            progress_reporter=progress_reporter,
        )

        if not downloader:
            if progress_reporter:
                progress_reporter.update_step("创建下载器", "未找到匹配下载器")
            display.print_error(f"No downloader found for type: {parsed['type']}")
            return None

        if progress_reporter:
            progress_reporter.advance_step("执行下载", "开始拉取与下载资源")
        result = await downloader.download(parsed)

        if progress_reporter:
            progress_reporter.advance_step(
                "记录历史",
                "写入数据库历史" if (result and database) else "数据库未启用，跳过",
            )
        if result and database:
            safe_config = {
                k: v for k, v in config.config.items()
                if k not in ("cookies", "cookie", "transcript")
            }
            await database.add_history({
                'url': original_url,
                'url_type': parsed['type'],
                'total_count': result.total,
                'success_count': result.success,
                'config': json.dumps(safe_config, ensure_ascii=False),
            })

        if progress_reporter:
            if result:
                progress_reporter.advance_step(
                    "收尾",
                    f"成功 {result.success} / 失败 {result.failed} / 跳过 {result.skipped}",
                )
            else:
                progress_reporter.advance_step("收尾", "无可统计结果")

        return result


def _strip_url_query(url: str) -> str:
    """Remove query parameters from a URL, keeping only scheme + host + path."""
    return re.sub(r'\?.*$', '', url.strip())


def _extract_sec_uid_from_url(url: str) -> str:
    """从用户主页 URL 中提取 sec_uid。"""
    match = re.search(r'/user/([A-Za-z0-9_-]+)', url)
    return match.group(1) if match else ""


async def download_multiple_account_config(
    config_path: str,
    base_config: ConfigLoader,
    cookie_manager: CookieManager,
    database: Database = None,
):
    """按 multiple_account_config.json 循环下载多账号素材。

    每个条目包含 video_dir_path、post_count 和 douyin_users 列表。
    对每个 video_dir_path，依次遍历所有 douyin_users，调用用户主页下载。
    每次运行目标都是新下载 post_count 条作品；已下载过的视频通过数据库
    自动跳过不重复下载，但不占用 post_count 名额。
    """
    try:
        with open(config_path, 'r', encoding='utf-8') as f:
            accounts = json.load(f)
    except Exception as e:
        display.print_error(f"Failed to load multiple_account_config: {e}")
        return

    if not isinstance(accounts, list):
        display.print_error("multiple_account_config.json must be a JSON array")
        return

    # 确保数据库可用（用于记录多账号下载历史，防止重复下载）
    ma_db = database
    own_db = False
    if ma_db is None:
        db_path = base_config.get('database_path', 'dy_downloader.db') or 'dy_downloader.db'
        ma_db = Database(db_path=str(db_path))
        await ma_db.initialize()
        own_db = True
        display.print_success("Multi-account database initialized")

    try:
        for entry_idx, entry in enumerate(accounts, 1):
            video_dir_path = entry.get('video_dir_path', '').strip()
            increased_video_dir_path = entry.get('increased_video_dir_path', '').strip()
            post_count = int(entry.get('post_count', 0) or 0)
            douyin_users = entry.get('douyin_users', [])

            if not video_dir_path:
                display.print_warning(f"Entry #{entry_idx}: missing video_dir_path, skipping")
                continue
            if not douyin_users:
                display.print_warning(f"Entry #{entry_idx}: no douyin_users, skipping")
                continue

            # 自动创建目录
            video_dir = Path(video_dir_path)
            video_dir.mkdir(parents=True, exist_ok=True)
            if increased_video_dir_path:
                Path(increased_video_dir_path).mkdir(parents=True, exist_ok=True)

            # 清空 video_dir_path 目录内容
            for item in video_dir.iterdir():
                if item.is_file():
                    item.unlink()
                elif item.is_dir():
                    shutil.rmtree(item)
            display.print_info(f"已清空目录: {video_dir_path}")

            # 查询该目录历史已下载 ID（仅用于防止重复下载，不影响本次计数）
            already_ids = await ma_db.get_multi_account_downloaded_ids(video_dir_path)
            history_count = len(already_ids)

            display.print_info(
                f"\n[{entry_idx}/{len(accounts)}] 目标目录: {video_dir_path}  "
                f"本次目标: {post_count} 条  历史已下载: {history_count} 条"
            )

            # 为这个 video_dir_path 克隆配置并覆盖保存路径
            dir_config = ConfigLoader(base_config.config_path)
            dir_config.config.update(base_config.config)
            dir_config.update(path=video_dir_path)
            # 遇到已下载的视频时停止当前用户，跳到下一个用户
            dir_config.config['stop_on_skip'] = True
            # 不创建 post/like 等模式子目录，直接放在作者目录下
            dir_config.config['skip_mode_folder'] = True
            # 视频文件直接放在作者目录下，不为每个视频创建子文件夹
            dir_config.config['folderstyle'] = False
            # 视频文件直接放在 video_dir_path 下，不创建作者名子目录
            dir_config.config['skip_author_folder'] = True
            # 目录已清空，数据库中已有记录的视频不需要重新下载
            dir_config.config['skip_redownload'] = True
            # 不生成 download_manifest.jsonl 文件
            dir_config.config['skip_manifest'] = True
            # 视频时长过滤（秒），短于此值的视频跳过不下载
            video_length = int(entry.get('video_length', 0) or 0)
            if video_length > 0:
                dir_config.config['min_video_length'] = video_length

            # 本次运行新下载的计数（每次运行从 0 开始）
            new_downloaded = 0

            for user_idx, user_entry in enumerate(douyin_users, 1):
                if post_count > 0 and new_downloaded >= post_count:
                    display.print_info(f"  本次已新下载 {post_count} 条，停止遍历后续用户")
                    break

                raw_page = user_entry.get('user_page', '').strip()
                if not raw_page:
                    display.print_warning(f"  用户 #{user_idx}: user_page 为空，跳过")
                    continue

                # 去掉查询参数，只保留路径
                user_url = _strip_url_query(raw_page)
                sec_uid = _extract_sec_uid_from_url(user_url)
                display.print_info(f"  [{user_idx}/{len(douyin_users)}] 下载用户: {user_url}")

                # 动态调整本次最多拉取数量（需要多拉一些以覆盖被跳过的已下载作品）
                if post_count > 0:
                    remaining = post_count - new_downloaded
                    number_cfg = dict(dir_config.get('number') or {})
                    number_cfg['post'] = remaining
                    dir_config.update(number=number_cfg)

                result = await download_url(
                    user_url,
                    dir_config,
                    cookie_manager,
                    ma_db,
                    progress_reporter=None,
                )

                if result:
                    new_count = 0
                    # 记录本次成功下载的 aweme_id 到 multi_account_download 表
                    for aweme_id in result.downloaded_aweme_ids:
                        if aweme_id not in already_ids:
                            await ma_db.add_multi_account_download(
                                video_dir_path, sec_uid, aweme_id
                            )
                            already_ids.add(aweme_id)
                            new_count += 1
                    new_downloaded += new_count
                    display.print_success(
                        f"  用户 #{user_idx} 完成: 新增 {new_count} / 失败 {result.failed} / 跳过 {result.skipped}  本次累计新增: {new_downloaded}/{post_count}"
                    )
                else:
                    display.print_warning(f"  用户 #{user_idx} 下载失败或无结果")

            # 将 video_dir_path 中的文件复制到 increased_video_dir_path
            if increased_video_dir_path:
                inc_dir = Path(increased_video_dir_path)
                copied_count = 0
                for item in video_dir.iterdir():
                    if item.is_file():
                        shutil.copy2(str(item), str(inc_dir / item.name))
                        copied_count += 1
                display.print_success(
                    f"已复制 {copied_count} 个文件到: {increased_video_dir_path}"
                )

            display.print_success(
                f"目录 [{video_dir_path}] 汇总: 本次新下载 {new_downloaded} 条, 历史总计 {len(already_ids)} 条"
            )
    finally:
        if own_db:
            await ma_db.close()


async def main_async(args):
    display.show_banner()

    if args.config:
        config_path = args.config
    else:
        config_path = 'config.yml'

    if not Path(config_path).exists():
        display.print_error(f"Config file not found: {config_path}")
        return

    config = ConfigLoader(config_path)

    if args.url:
        urls = args.url if isinstance(args.url, list) else [args.url]
        for url in urls:
            if url not in config.get('link', []):
                config.update(link=config.get('link', []) + [url])

    if args.path:
        config.update(path=args.path)

    if args.thread:
        config.update(thread=args.thread)

    # ── 多账号批量下载模式：提前检测，跳过 link 校验 ─────────────────────
    multiple_config_path = args.multiple_config or 'multiple_account_config.json'
    is_multiple_mode = Path(multiple_config_path).exists()

    if not is_multiple_mode and not config.validate():
        display.print_error("Invalid configuration: missing required fields")
        return

    cookies = config.get_cookies()
    cookie_manager = CookieManager()
    cookie_manager.set_cookies(cookies)

    if not cookie_manager.validate_cookies():
        display.print_warning("Cookies may be invalid or incomplete")

    database = None
    if config.get('database'):
        db_path = config.get('database_path', 'dy_downloader.db') or 'dy_downloader.db'
        database = Database(db_path=str(db_path))
        await database.initialize()
        display.print_success("Database initialized")

    if is_multiple_mode:
        display.print_info(f"Multiple account config detected: {multiple_config_path}")
        await download_multiple_account_config(
            multiple_config_path,
            config,
            cookie_manager,
            database,
        )
        if database is not None:
            await database.close()
        return

    urls = config.get_links()
    display.print_info(f"Found {len(urls)} URL(s) to process")

    all_results = []
    progress_config = config.get("progress", {}) or {}
    quiet_by_config = _as_bool(progress_config.get("quiet_logs", True), default=True)
    quiet_progress_logs = quiet_by_config and not (args.verbose or args.show_warnings)
    if quiet_progress_logs:
        # Progress 运行期间若有大量错误日志会触发 rich 反复重绘，导致屏幕出现重复块。
        # 默认静默控制台日志，下载完成后再恢复。
        set_console_log_level(logging.CRITICAL)

    display.start_download_session(len(urls))
    try:
        for i, url in enumerate(urls, 1):
            display.start_url(i, len(urls), url)

            result = await download_url(
                url,
                config,
                cookie_manager,
                database,
                progress_reporter=display,
            )
            if result:
                all_results.append(result)
                display.complete_url(result)
            else:
                display.fail_url("下载失败或链接无效")
    finally:
        display.stop_download_session()
        if database is not None:
            await database.close()
        if quiet_progress_logs:
            set_console_log_level(logging.ERROR)

    if all_results:
        from core.downloader_base import DownloadResult
        total_result = DownloadResult()
        for r in all_results:
            total_result.total += r.total
            total_result.success += r.success
            total_result.failed += r.failed
            total_result.skipped += r.skipped

        display.print_success("\n=== Overall Summary ===")
        display.show_result(total_result)


def main():
    parser = argparse.ArgumentParser(description='Douyin Downloader - 抖音批量下载工具')
    parser.add_argument('-u', '--url', action='append', help='Download URL(s)')
    parser.add_argument('-c', '--config', help='Config file path (default: config.yml)')
    parser.add_argument('-p', '--path', help='Save path')
    parser.add_argument('-t', '--thread', type=int, help='Thread count')
    parser.add_argument(
        '-m', '--multiple-config',
        dest='multiple_config',
        help='Multiple account config JSON path (default: multiple_account_config.json)',
    )
    parser.add_argument('--show-warnings', action='store_true', help='Show warning logs in console')
    parser.add_argument('-v', '--verbose', action='store_true', help='Enable verbose console logs')
    try:
        from __init__ import __version__
    except ImportError:
        __version__ = "2.0.0"
    parser.add_argument('--version', action='version', version=__version__)

    args = parser.parse_args()

    if args.verbose:
        set_console_log_level(logging.INFO)
    elif args.show_warnings:
        set_console_log_level(logging.WARNING)
    else:
        set_console_log_level(logging.ERROR)

    try:
        asyncio.run(main_async(args))
    except KeyboardInterrupt:
        display.print_warning("\nDownload interrupted by user")
        sys.exit(0)
    except Exception as e:
        display.print_error(f"Fatal error: {e}")
        logger.exception("Fatal error occurred")
        sys.exit(1)


if __name__ == '__main__':
    main()

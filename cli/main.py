import asyncio
import argparse
import json
import logging
import re
import shutil
import sys
from pathlib import Path
from typing import Any

import aiohttp

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


def _extract_hashtags(text: str) -> list:
    """从文本中提取 #标签，返回标签列表（不含#号）。"""
    if not text:
        return []
    return re.findall(r'#([^\s#]+)', text)


async def _rewrite_title(title: str) -> str:
    """通过在线 API 将标题改写为约 50% 相似度的新语句，添加不同语气。

    使用搜狗翻译做中→英→中回译来实现语义保留但表述不同的效果。
    如果 API 失败或结果与原文完全相同，则返回原文。
    """
    if not title or not title.strip():
        return ""

    # 去掉标题中的 #标签 部分，只改写正文
    clean_title = re.sub(r'#[^\s#]+', '', title).strip()
    if not clean_title:
        return ""

    try:
        async with aiohttp.ClientSession() as session:
            # 第一步：中文 → 英文
            async with session.post(
                "https://fanyi.sogou.com/api/transpc/text/result",
                json={"from": "zh-CHS", "to": "en", "text": clean_title},
                headers={"Content-Type": "application/json"},
                timeout=aiohttp.ClientTimeout(total=8),
            ) as resp:
                data = await resp.json()
                en_text = data.get("data", {}).get("translate", {}).get("dit", "")
                if not en_text:
                    return clean_title

            # 第二步：英文 → 中文
            async with session.post(
                "https://fanyi.sogou.com/api/transpc/text/result",
                json={"from": "en", "to": "zh-CHS", "text": en_text},
                headers={"Content-Type": "application/json"},
                timeout=aiohttp.ClientTimeout(total=8),
            ) as resp:
                data = await resp.json()
                zh_text = data.get("data", {}).get("translate", {}).get("dit", "")
                return zh_text if zh_text and zh_text != clean_title else clean_title
    except Exception as e:
        logger.warning("标题改写失败: %s", e)
        return clean_title


async def _fetch_related_keywords(keyword: str, count: int = 20) -> list:
    """通过 Bing 搜索建议 API 获取关键词的相关词列表。

    对关键词本身以及逐字追加 a~z 进行多次查询，以获取足够多的建议。
    返回最多 count 个去重的相关词（结果中自然会包含形容词、名词等）。
    如果网络请求失败，返回空列表。
    """
    url = "https://api.bing.com/qsonhs.aspx"
    # 先查原词，再用 "关键词+字母" 扩展获取更多结果
    queries = [keyword]
    for ch in 'abcdefghijklmnopqrstuvwxyz':
        queries.append(f"{keyword} {ch}")

    seen = set()
    suggestions = []
    try:
        async with aiohttp.ClientSession() as session:
            for query in queries:
                if len(suggestions) >= count:
                    break
                try:
                    params = {"type": "cb", "q": query}
                    async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                        text = await resp.text()
                        match = re.search(r'\{.*\}', text, re.DOTALL)
                        if not match:
                            continue
                        data = json.loads(match.group())
                        for item in data.get('AS', {}).get('Results', []):
                            for suggest in item.get('Suggests', []):
                                txt = suggest.get('Txt', '').strip()
                                if txt and txt != keyword and txt not in seen:
                                    seen.add(txt)
                                    suggestions.append(txt)
                                if len(suggestions) >= count:
                                    break
                            if len(suggestions) >= count:
                                break
                except Exception:
                    continue
    except Exception as e:
        logger.warning("获取搜索建议失败: %s", e)
    return suggestions[:count]


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

    # 加载抖音用户历史记录（用于检测重复用户）
    history_path = Path(config_path).parent / 'douyin_users_history.json'
    history_users = set()
    if history_path.exists():
        try:
            with open(history_path, 'r', encoding='utf-8') as f:
                history_list = json.load(f)
            if isinstance(history_list, list):
                for url in history_list:
                    cleaned = _strip_url_query(str(url))
                    if cleaned:
                        history_users.add(cleaned)
        except Exception:
            pass

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

            display.print_info(
                f"\n[{entry_idx}/{len(accounts)}] 目标目录: {video_dir_path}  "
                f"本次目标: {post_count} 条"
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
            # 多账号模式命名：搜索关键词组合 + 随机字符
            dir_config.config['multi_account_naming'] = True
            # 以 video_dir_path 上级目录名为关键词，联网搜索相关词用于文件命名
            parent_name = Path(video_dir_path).parent.name
            if parent_name:
                keyword_count = max(20, post_count * 5)
                related_keywords = await _fetch_related_keywords(parent_name, count=keyword_count)
                if related_keywords:
                    display.print_info(f"  命名关键词池 ({parent_name}): {related_keywords}")
                else:
                    display.print_warning(f"  未获取到搜索建议，将使用 [{parent_name}] 作为命名关键词")
                    related_keywords = [parent_name]
                dir_config.config['naming_keywords'] = related_keywords
            # 视频时长过滤（秒），只下载时长小于此值的视频
            video_length = int(entry.get('video_length', 0) or 0)
            if video_length > 0:
                dir_config.config['min_video_length'] = video_length
                display.print_info(f"  视频时长过滤: 只下载时长 < {video_length}秒 的视频")

            # 本次运行新下载的计数（每次运行从 0 开始）
            new_downloaded = 0

            for user_idx, user_entry in enumerate(douyin_users, 1):
                if post_count > 0 and new_downloaded >= post_count:
                    display.print_info(f"  本次已新下载 {post_count} 条，停止遍历后续用户")
                    break

                # 兼容字符串数组和对象数组两种格式
                if isinstance(user_entry, str):
                    raw_page = user_entry.strip()
                else:
                    raw_page = user_entry.get('user_page', '').strip()
                if not raw_page:
                    display.print_warning(f"  用户 #{user_idx}: user_page 为空，跳过")
                    continue

                # 去掉查询参数，只保留路径
                user_url = _strip_url_query(raw_page)
                sec_uid = _extract_sec_uid_from_url(user_url)

                # 检查该用户是否在历史记录中
                if user_url in history_users:
                    display.print_warning(
                        f"  用户 #{user_idx}: {user_url} 已在历史记录中，跳过（请更换新博主）"
                    )
                    continue

                # 查询该用户历史已下载 ID（按 sec_uid 去重，与文件夹无关）
                already_ids = await ma_db.get_multi_account_downloaded_ids(sec_uid)
                display.print_info(
                    f"  [{user_idx}/{len(douyin_users)}] 下载用户: {user_url}  历史已下载: {len(already_ids)} 条"
                )

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
                            # 从 aweme 表查询视频元数据
                            aweme_info = await ma_db.get_aweme(aweme_id)
                            meta_kwargs = {}
                            if aweme_info:
                                meta_kwargs['title'] = aweme_info.get('title')
                                meta_kwargs['desc'] = aweme_info.get('title')
                                meta_kwargs['author_name'] = aweme_info.get('author_name')
                                # 从完整 metadata 解析 tags 和 duration
                                raw_meta = aweme_info.get('metadata')
                                if raw_meta:
                                    try:
                                        full = json.loads(raw_meta)
                                        # 提取标签
                                        tags = []
                                        for te in full.get('text_extra', []):
                                            if te.get('type') == 1 and te.get('hashtag_name'):
                                                tags.append(te['hashtag_name'])
                                        for cha in full.get('cha_list', []):
                                            name = cha.get('cha_name', '')
                                            if name and name not in tags:
                                                tags.append(name)
                                        meta_kwargs['tags'] = json.dumps(tags, ensure_ascii=False) if tags else None
                                        # 提取时长（毫秒）
                                        duration = full.get('video', {}).get('duration', 0) or 0
                                        meta_kwargs['duration'] = duration
                                        # 提取完整描述
                                        meta_kwargs['desc'] = full.get('desc')
                                    except (json.JSONDecodeError, TypeError):
                                        pass
                                # 从下载结果中获取视频文件名
                                dl_filename = result.downloaded_filenames.get(aweme_id)
                                if dl_filename:
                                    meta_kwargs['file_name'] = dl_filename
                                else:
                                    # 降级：从目录中查找
                                    file_path = aweme_info.get('file_path')
                                    if file_path:
                                        try:
                                            fp = Path(file_path)
                                            if fp.is_dir():
                                                video_files = [f.name for f in fp.iterdir() if f.suffix in ('.mp4', '.mp3')]
                                                if video_files:
                                                    meta_kwargs['file_name'] = video_files[0]
                                        except Exception:
                                            pass

                            # 从 title/desc 中提取 #标签
                            raw_title = meta_kwargs.get('title') or meta_kwargs.get('desc') or ''
                            hashtags = _extract_hashtags(raw_title)
                            if hashtags:
                                meta_kwargs['hashtags'] = json.dumps(hashtags, ensure_ascii=False)

                            # 改写标题（回译生成不同表述）
                            if raw_title:
                                title_2 = await _rewrite_title(raw_title)
                                if title_2:
                                    meta_kwargs['title_2'] = title_2
                                    display.print_info(f"    标题改写: {raw_title[:30]}... → {title_2[:30]}...")

                            await ma_db.add_multi_account_download(
                                sec_uid, aweme_id,
                                video_dir_path=video_dir_path, **meta_kwargs
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

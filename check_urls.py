#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
TVBox URL Checker Pro v4
Part 1 - 強化版 (含代理解包) - 修正版
"""

from __future__ import annotations
import json
import re
import shutil
import socket
import time
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from typing import List, Tuple, Optional, Set, Dict
from dataclasses import dataclass, field
from urllib.parse import urlparse, parse_qs, unquote
import requests
import yaml
import urllib3

# 關閉 SSL 未驗證的警告提示
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ============================================================================
# 設定載入
# ============================================================================

try:
    with open("config.yaml", "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
except Exception:
    cfg = {}

INPUT_FILE = cfg.get("input", "data/source.txt")
OUTPUT_FILE = cfg.get("output", "data/source_clean.txt")
INVALID_FILE = cfg.get("invalid", "data/invalid_urls.txt")
DUPLICATE_FILE = cfg.get("duplicate", "data/duplicate_urls.txt")
PROXY_FILE = cfg.get("proxy", "data/proxy_urls.txt")
UNPROXY_FILE = cfg.get("unproxy", "data/unproxy_urls.txt")
REPORT_FILE = cfg.get("report", "data/report.md")
MAX_WORKERS = cfg.get("workers", 50)
TIMEOUT = cfg.get("timeout", 8)
RETRY = cfg.get("retry", 3)
BACKUP_ENABLED = cfg.get("backup", True)
HISTORY_DIR = cfg.get("history", "data/history")

# 代理解包設定
PROXY_UNPACK_ENABLED = cfg.get("proxy_unpack", True)
PROXY_UNPACK_DEPTH = cfg.get("proxy_unpack_depth", 3)

USER_AGENT = (
    "Mozilla/5.0 "
    "(Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 "
    "(KHTML, like Gecko) "
    "Chrome/119.0.0.0 "
    "Safari/537.36"
)

# ============================================================================
# 常數定義
# ============================================================================

URL_PATTERN = re.compile(r"https?://[^\s<>\"']+")

INVALID_KEYWORDS = [
    "404", "not found", "access denied", "forbidden", "error",
    "502 bad gateway", "503 service", "nginx", "<html"
]

PROXY_SERVICES = {
    "scrapeops": (r"scrapeops\.io", "url"),
    "oxylabs": (r"oxylabs\.io", "url"),
    "brightdata": (r"brightdata\.com|luminati\.io", "url"),
    "smartproxy": (r"smartproxy\.com", "url"),
    "soax": (r"soax\.com", "url"),
    "netnut": (r"netnut\.io", "url"),
    "iproyal": (r"iproyal\.com", "url"),
    "webshare": (r"webshare\.io", "url"),
    "proxyrack": (r"proxyrack\.com", "url"),
    "zenrows": (r"zenrows\.com", "url"),
    "scrapingbee": (r"scrapingbee\.com", "url"),
    "scrapingfish": (r"scrapingfish\.com", "url"),
    "scraperapi": (r"scraperapi\.com", "url"),
}

PROXY_PARAMS = [
    "url", "target", "destination", "dest", "to", "redirect",
    "redirect_uri", "continue", "next", "return", "return_url",
    "callback", "callback_url", "forward", "forward_url", "proxy_url", "proxy_dest"
]

# ============================================================================
# 資料結構
# ============================================================================

@dataclass
class CheckResult:
    """單一 URL 檢查結果"""
    url: str
    is_valid: bool
    original_url: Optional[str] = None
    unpacked_url: Optional[str] = None
    unpack_depth: int = 0
    is_proxy: bool = False
    proxy_type: Optional[str] = None
    status_code: Optional[int] = None
    error_message: Optional[str] = None

@dataclass
class LineResult:
    """單行處理結果"""
    original_line: str
    cleaned_line: str
    urls: List[str] = field(default_factory=list)
    valid_urls: List[str] = field(default_factory=list)
    invalid_urls: List[str] = field(default_factory=list)
    duplicate_urls: List[str] = field(default_factory=list)
    proxy_urls: List[str] = field(default_factory=list)
    unpacked_urls: List[str] = field(default_factory=list)

# ============================================================================
# URL 解包器
# ============================================================================

class URLUnpacker:
    """代理網址解包器"""
    def __init__(self):
        self.proxy_patterns = self._compile_proxy_patterns()
        self.unpacked_cache: Dict[str, Tuple[str, int]] = {}
    
    def _compile_proxy_patterns(self) -> Dict[str, Dict]:
        patterns = {}
        for service, (pattern, param) in PROXY_SERVICES.items():
            patterns[service] = {
                'pattern': re.compile(pattern, re.IGNORECASE),
                'param': param
            }
        return patterns
    
    def unpack_url(self, url: str, depth: int = 0, max_depth: int = 3) -> Tuple[str, int, bool]:
        if not PROXY_UNPACK_ENABLED or depth >= max_depth:
            return url, depth, False
        
        cache_key = f"{url}_{depth}"
        if cache_key in self.unpacked_cache:
            cached_url, cached_depth = self.unpacked_cache[cache_key]
            return cached_url, cached_depth, cached_url != url
        
        unpacked = self._extract_real_url(url)
        if unpacked and unpacked != url:
            real_url, new_depth, _ = self.unpack_url(unpacked, depth + 1, max_depth)
            self.unpacked_cache[cache_key] = (real_url, new_depth)
            return real_url, new_depth, True
        
        self.unpacked_cache[cache_key] = (url, depth)
        return url, depth, False
    
    def _extract_real_url(self, url: str) -> Optional[str]:
        try:
            parsed = urlparse(url)
            params = parse_qs(parsed.query)
            
            for param in PROXY_PARAMS:
                if param in params:
                    real_url = unquote(params[param][0])
                    if self._is_valid_url(real_url):
                        return real_url
            
            path_patterns = [
                r'/prox(ies|y)/(https?://[^\s/]+.*)',
                r'/api/proxy/(https?://[^\s/]+.*)',
                r'/(fetch|get|redirect|go|link|out)/(https?://[^\s/]+.*)',
            ]
            for pattern in path_patterns:
                match = re.search(pattern, parsed.path)
                if match:
                    real_url = unquote(match.group(2))
                    if self._is_valid_url(real_url):
                        return real_url
            
            domain = parsed.netloc.lower()
            for service, info in self.proxy_patterns.items():
                if info['pattern'].search(domain):
                    param = info['param']
                    if param in params:
                        real_url = unquote(params[param][0])
                        if self._is_valid_url(real_url):
                            return real_url
            
            if parsed.path and ('%3A' in parsed.path or '%2F' in parsed.path):
                decoded_path = unquote(parsed.path)
                url_match = re.search(r'https?://[^\s<>"\']+', decoded_path)
                if url_match and self._is_valid_url(url_match.group(0)):
                    return url_match.group(0)
            
            return None
        except Exception:
            return None
    
    def _is_valid_url(self, url: str) -> bool:
        if not url: return False
        try:
            parsed = urlparse(url)
            return parsed.scheme in ('http', 'https') and bool(parsed.netloc)
        except Exception:
            return False
    
    def get_proxy_type(self, url: str) -> Optional[str]:
        try:
            parsed = urlparse(url)
            domain = parsed.netloc.lower()
            for service, info in self.proxy_patterns.items():
                if info['pattern'].search(domain):
                    return service
            
            params = parse_qs(parsed.query)
            if any(p in params for p in PROXY_PARAMS):
                return "通用代理"
            return None
        except Exception:
            return None

# ============================================================================
# URL 檢查器
# ============================================================================

class URLChecker:
    """URL 檢查器主類別 - 含代理解包"""
    def __init__(self):
        self.total = 0
        self.valid = 0
        self.invalid = 0
        self.duplicate = 0
        self.proxy_count = 0
        self.unpacked_count = 0
        self.empty_lines = 0
        self.no_url_lines = 0
        
        self.seen_urls: Set[str] = set()
        self.invalid_urls: List[str] = []
        self.duplicate_urls: List[str] = []
        self.proxy_urls: List[str] = []
        self.unpacked_urls: List[str] = []
        self.url_status: Dict[str, bool] = {}
        
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT})
        
        adapter = requests.adapters.HTTPAdapter(
            pool_connections=MAX_WORKERS,
            pool_maxsize=MAX_WORKERS,
            max_retries=RETRY
        )
        self.session.mount('http://', adapter)
        self.session.mount('https://', adapter)
        
        self.unpacker = URLUnpacker()
        # 初始化共用的全域執行緒池
        self.executor = ThreadPoolExecutor(max_workers=MAX_WORKERS)

    def load(self) -> List[str]:
        p = Path(INPUT_FILE)
        if not p.exists():
            raise FileNotFoundError(f"輸入檔案不存在: {INPUT_FILE}")
        lines = p.read_text(encoding="utf-8", errors="ignore").splitlines()
        print(f"📂 載入 {len(lines)} 行資料")
        return lines

    def save(self, lines: List[str]) -> None:
        output_path = Path(OUTPUT_FILE)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        
        if BACKUP_ENABLED and output_path.exists():
            self._backup_file(output_path)
        
        filtered_lines = []
        for line in lines:
            stripped = line.strip()
            if not stripped:
                self.empty_lines += 1
                continue
            if not URL_PATTERN.search(line):
                self.no_url_lines += 1
                continue
            filtered_lines.append(line)
        
        output_path.write_text("\n".join(filtered_lines), encoding="utf-8")
        print(f"\n📊 過濾統計：")
        print(f"  - 移除空白行：{self.empty_lines} 行")
        print(f"  - 移除無網址行：{self.no_url_lines} 行")
        print(f"  - 保留有效行：{len(filtered_lines)} 行")
        print(f"  - 輸出檔案：{OUTPUT_FILE}")

    def _backup_file(self, file_path: Path) -> None:
        history_path = Path(HISTORY_DIR)
        history_path.mkdir(parents=True, exist_ok=True)
        ts = time.strftime("%Y%m%d-%H%M%S")
        shutil.copy2(file_path, history_path / f"backup_{ts}.txt")

    def save_invalid(self) -> None:
        if self.invalid_urls: Path(INVALID_FILE).write_text("\n".join(self.invalid_urls), encoding="utf-8")

    def save_duplicate(self) -> None:
        if self.duplicate_urls: Path(DUPLICATE_FILE).write_text("\n".join(self.duplicate_urls), encoding="utf-8")

    def save_proxy(self) -> None:
        if self.proxy_urls: Path(PROXY_FILE).write_text("\n".join(self.proxy_urls), encoding="utf-8")

    def save_unpacked(self) -> None:
        if self.unpacked_urls: Path(UNPROXY_FILE).write_text("\n".join(self.unpacked_urls), encoding="utf-8")

    def extract_urls(self, line: str) -> List[str]:
        return URL_PATTERN.findall(line)

    def process_url(self, url: str) -> Optional[CheckResult]:
        """處理單個 URL 核心邏輯（多執行緒進入點）"""
        self.total += 1
        proxy_type = self.unpacker.get_proxy_type(url)
        is_proxy = proxy_type is not None
        
        if is_proxy and PROXY_UNPACK_ENABLED:
            real_url, depth, unpacked = self.unpacker.unpack_url(url, max_depth=PROXY_UNPACK_DEPTH)
            if unpacked and real_url:
                self.unpacked_count += 1
                self.unpacked_urls.append(real_url)
                
                if real_url in self.seen_urls:
                    self.duplicate += 1
                    self.duplicate_urls.append(real_url)
                    return CheckResult(url=real_url, original_url=url, unpacked_url=real_url, unpack_depth=depth, is_valid=False, is_proxy=True, proxy_type=proxy_type, error_message="解包後URL重複")
                
                self.seen_urls.add(real_url)
                is_valid = self.check_url(real_url)
                if is_valid:
                    self.valid += 1
                    return CheckResult(url=real_url, original_url=url, unpacked_url=real_url, unpack_depth=depth, is_valid=True, is_proxy=True, proxy_type=proxy_type)
                else:
                    self.invalid += 1
                    self.invalid_urls.append(real_url)
                    return CheckResult(url=real_url, original_url=url, unpacked_url=real_url, unpack_depth=depth, is_valid=False, is_proxy=True, proxy_type=proxy_type, error_message="解包後URL無效")
            else:
                self.proxy_count += 1
                self.proxy_urls.append(url)
                return CheckResult(url=url, is_valid=False, is_proxy=True, proxy_type=proxy_type, error_message="無法解包代理URL")
        
        if is_proxy:
            self.proxy_count += 1
            self.proxy_urls.append(url)
        
        if url in self.seen_urls:
            self.duplicate += 1
            self.duplicate_urls.append(url)
            return CheckResult(url=url, is_valid=False, is_proxy=is_proxy, proxy_type=proxy_type, error_message="重複 URL")
        
        self.seen_urls.add(url)
        is_valid = self.url_status.get(url) if url in self.url_status else self.check_url(url)
        self.url_status[url] = is_valid
        
        if is_valid:
            self.valid += 1
            return CheckResult(url=url, is_valid=True, is_proxy=is_proxy, proxy_type=proxy_type)
        else:
            self.invalid += 1
            self.invalid_urls.append(url)
            return CheckResult(url=url, is_valid=False, is_proxy=is_proxy, proxy_type=proxy_type, error_message="連線失敗或內容無效")

    def check_all(self) -> None:
        lines = self.load()
        line_results: List[LineResult] = []
        all_tasks = []
        
        print(f"🔍 開始檢查網址有效性...")
        if PROXY_UNPACK_ENABLED:
            print(f"🔄 代理解包已啟用 (最大深度: {PROXY_UNPACK_DEPTH})")
        
        for line_num, line in enumerate(lines, 1):
            urls = self.extract_urls(line)
            if not urls:
                line_results.append(LineResult(original_line=line, cleaned_line=line, urls=[]))
                continue
            
            line_result = LineResult(original_line=line, cleaned_line=line, urls=urls)
            for url in urls:
                # 提交給全域共用的 executor，且目標修正為 process_url
                future = self.executor.submit(self.process_url, url)
                all_tasks.append((future, url, line_result))
            
            line_results.append(line_result)
            if line_num % 50 == 0:
                print(f"  進度: {line_num}/{len(lines)} 行")
        
        print(f"  ⏳ 等待所有檢查完成...")
        processed_urls: Set[str] = set()
        
        for future, url, line_result in all_tasks:
            try:
                result = future.result(timeout=TIMEOUT + 5)
                if result and url not in processed_urls:
                    processed_urls.add(url)
                    
                    if result.is_proxy and result.unpacked_url:
                        if result.is_valid:
                            line_result.unpacked_urls.append(result.unpacked_url)
                            line_result.valid_urls.append(result.unpacked_url)
                            line_result.cleaned_line = line_result.cleaned_line.replace(url, result.unpacked_url)
                        else:
                            line_result.invalid_urls.append(result.unpacked_url)
                            line_result.cleaned_line = line_result.cleaned_line.replace(url, "")
                    elif result.is_proxy:
                        line_result.proxy_urls.append(url)
                        line_result.cleaned_line = line_result.cleaned_line.replace(url, "")
                    elif result.is_valid:
                        line_result.valid_urls.append(url)
                    else:
                        line_result.invalid_urls.append(url)
                        line_result.cleaned_line = line_result.cleaned_line.replace(url, "")
            except Exception as e:
                self.invalid += 1
                self.invalid_urls.append(url)
                line_result.invalid_urls.append(url)
                line_result.cleaned_line = line_result.cleaned_line.replace(url, "")
                print(f"  ⚠️ 檢查 URL 失敗: {url[:50]}... - {str(e)}")
        
        print(f"  ✅ 完成所有檢查")
        
        # 關閉執行緒池
        self.executor.shutdown(wait=True)
        
        cleaned_lines = []
        for result in line_results:
            cleaned = re.sub(r'\s+', ' ', result.cleaned_line).strip()
            if cleaned:
                cleaned_lines.append(cleaned)
        
        self.save(cleaned_lines)
        self.save_invalid()
        self.save_duplicate()
        self.save_proxy()
        self.save_unpacked()
        self.generate_report()

    def check_url(self, url: str) -> bool:
        for attempt in range(RETRY):
            try:
                try:
                    head_response = self.session.head(url, timeout=TIMEOUT, allow_redirects=True, verify=False)
                    if head_response.status_code >= 400: pass
                except Exception:
                    pass
                
                response = self.session.get(
                    url, timeout=TIMEOUT, allow_redirects=True, stream=True, verify=False,
                    headers={'Accept': '*/*', 'Accept-Encoding': 'gzip, deflate', 'Connection': 'keep-alive'}
                )
                
                if response.status_code >= 400:
                    if attempt < RETRY - 1:
                        time.sleep(0.5 * (attempt + 1))
                        continue
                    return False
                
                content_length = response.headers.get('content-length')
                if content_length and int(content_length) < 100:
                    if attempt < RETRY - 1:
                        time.sleep(0.5 * (attempt + 1))
                        continue
                    return False
                
                content = self._read_content(response)
                if self.validate_content(url, content):
                    return True
                
                if attempt < RETRY - 1:
                    time.sleep(0.5 * (attempt + 1))
                    continue
                return False
            except (requests.exceptions.RequestException, socket.error):
                if attempt < RETRY - 1:
                    time.sleep(0.5 * (attempt + 1))
                    continue
                return False
        return False

    def _read_content(self, response: requests.Response, max_size: int = 2048) -> str:
        content = ""
        try:
            for chunk in response.iter_content(chunk_size=512):
                if chunk:
                    try:
                        content += chunk.decode('utf-8', errors='ignore')
                        if len(content) >= max_size: break
                    except Exception:
                        pass
        except Exception:
            pass
        return content

    def validate_content(self, url: str, content: str) -> bool:
        if not content or len(content.strip()) < 10: return False
        url_lower = url.lower()
        if url_lower.endswith('.json'): return self._validate_json(content)
        elif url_lower.endswith('.xml'): return self._validate_xml(content)
        elif url_lower.endswith(('.m3u', '.m3u8')): return self._validate_m3u(content)
        elif url_lower.endswith('.txt'): return self._validate_txt(content)
        return self._validate_common(content)

    def _validate_common(self, content: str) -> bool:
        content_lower = content.lower()
        for keyword in INVALID_KEYWORDS:
            if keyword in content_lower: return False
        if len(content.strip()) < 20: return False
        tvbox_indicators = ['url', 'name', 'title', 'channel', 'group', 'http', 'https', '://', 'm3u8', 'flv']
        return sum(1 for ind in tvbox_indicators if ind in content_lower) >= 2

    def _validate_json(self, content: str) -> bool:
        content = content.strip()
        if not content or len(content) < 20 or '<html' in content.lower(): return False
        try:
            data = json.loads(content)
            if isinstance(data, dict):
                return any(k in data for k in ['urls', 'channels', 'sites', 'apps']) or len(data) >= 2
            return isinstance(data, list) and len(data) > 0
        except json.JSONDecodeError:
            return False

    def _validate_xml(self, content: str) -> bool:
        content_lower = content.lower()
        return any(i in content_lower for i in ['<?xml', '<tv', '<rss', '<channel']) and ('http' in content_lower or 'channel' in content_lower)

    def _validate_m3u(self, content: str) -> bool:
        return '#EXTM3U' in content.upper() and ('#EXTINF:' in content.upper() or 'HTTP' in content.upper())

    def _validate_txt(self, content: str) -> bool:
        content_lower = content.lower()
        if any(k in content_lower for k in ['404', 'forbidden', 'access denied', 'nginx', '<html', 'error']): return False
        return any(URL_PATTERN.search(line) for line in content.splitlines() if line.strip())

    def generate_report(self) -> None:
        lines = [
            "# 📊 TVBox URL 檢查報告", "", "## 📈 統計摘要", "",
            "| 項目 | 數量 | 比例 |", "|------|------|------|",
            f"| 總網址數 | {self.total} | 100% |",
            f"| ✅ 有效 | {self.valid} | {(self.valid/self.total*100):.1f}% |" if self.total > 0 else "| ✅ 有效 | 0 | 0% |",
            f"| ❌ 失效 | {self.invalid} | {(self.invalid/self.total*100):.1f}% |" if self.total > 0 else "| ❌ 失效 | 0 | 0% |",
            f"| 🔄 重複 | {self.duplicate} | {(self.duplicate/self.total*100):.1f}% |" if self.total > 0 else "| 🔄 重複 | 0 | 0% |",
            f"| 🛡️ 代理 | {self.proxy_count} | {(self.proxy_count/self.total*100):.1f}% |" if self.total > 0 else "| 🛡️ 代理 | 0 | 0% |",
            f"| 🔓 解包 | {self.unpacked_count} | {(self.unpacked_count/self.total*100):.1f}% |" if self.total > 0 else "| 🔓 解包 | 0 | 0% |",
            "", "## 🧹 清理統計", "",
            f"- **移除空白行**：{self.empty_lines} 行", f"- **移除無網址行**：{self.no_url_lines} 行", "",
            f"## ✅ 有效網址 ({self.valid})", "", f"有效網址已儲存至：`{OUTPUT_FILE}`", ""
        ]
        
        if self.unpacked_urls:
            lines.extend(["## 🔓 成功解包的URL", "", f"共成功解包 **{len(self.unpacked_urls)}** 個代理網址。", ""])
            for url in self.unpacked_urls[:30]: lines.append(f"- `{url}`")
            if len(self.unpacked_urls) > 30: lines.append(f"- ... 還有 {len(self.unpacked_urls) - 30} 個")
            lines.extend([f"完整清單請查看：`{UNPROXY_FILE}`", ""])
            
        lines.extend(["## ❌ 無效網址列表", ""])
        if self.invalid_urls:
            for url in self.invalid_urls[:30]: lines.append(f"- `{url}`")
            lines.append(f"完整清單請查看：`{INVALID_FILE}`")
        else:
            lines.append("✅ 沒有無效網址")
            
        lines.extend(["", "## 🔄 重複網址列表", ""])
        if self.duplicate_urls:
            for url in self.duplicate_urls[:30]: lines.append(f"- `{url}`")
            lines.append(f"完整清單請查看：`{DUPLICATE_FILE}`")
        else:
            lines.append("✅ 沒有重複網址")
            
        lines.extend(["", "---", f"🕐 更新時間：{time.strftime('%Y-%m-%d %H:%M:%S')}", "", "✅ 報告由 TVBox URL Checker Pro v4 自動生成"])
        Path(REPORT_FILE).write_text("\n".join(lines), encoding="utf-8")
        print(f"📄 報告已生成：{REPORT_FILE}")

# ============================================================================
# 主程式
# ============================================================================

if __name__ == "__main__":
    print("=" * 70)
    print("🚀 TVBox URL Checker Pro v4 (含代理解包)")
    print("=" * 70)
    
    start_time = time.time()
    try:
        checker = URLChecker()
        checker.check_all()
        
        print("\n" + "=" * 70)
        print("✅ 檢查完成！")
        print("=" * 70)
        print(f"📊 總網址 : {checker.total}")
        print(f"✅ 有效   : {checker.valid}")
        print(f"❌ 失效   : {checker.invalid}")
        print(f"🔄 重複   : {checker.duplicate}")
        print(f"🛡️ 代理   : {checker.proxy_count}")
        print(f"🔓 解包   : {checker.unpacked_count}")
        print(f"⏱️ 耗時   : {time.time() - start_time:.2f} 秒")
        print("=" * 70)
    except Exception as e:
        print(f"💥 程式執行失敗: {e}")

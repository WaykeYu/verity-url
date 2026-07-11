#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
TVBox URL Checker Pro v4
Part 1 - 強化版 (含代理解包)

功能
-------------------------
✓ 讀取 TXT
✓ 保留原格式
✓ 去除重覆網址
✓ 建立網址清單
✓ 多執行緒準備
✓ 去除空白行
✓ 去除無網址行
✓ 代理網址解包（提取真實網址）
✓ 自動還原被代理的URL
"""

from __future__ import annotations
import json
import re
import shutil
import socket
import time
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Tuple, Optional, Set, Dict
from dataclasses import dataclass, field
from collections import defaultdict
from urllib.parse import urlparse, parse_qs, unquote
import requests
import yaml

# ============================================================================
# 設定載入
# ============================================================================

with open("config.yaml", "r", encoding="utf-8") as f:
    cfg = yaml.safe_load(f)

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

# 無效關鍵詞
INVALID_KEYWORDS = [
    "404",
    "not found",
    "access denied",
    "forbidden",
    "error",
    "502 bad gateway",
    "503 service",
    "nginx",
    "<html",
]

# 代理服務特徵（用於識別和提取真實URL）
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

# 通用代理參數名稱
PROXY_PARAMS = [
    "url",
    "target",
    "destination",
    "dest",
    "to",
    "redirect",
    "redirect_uri",
    "continue",
    "next",
    "return",
    "return_url",
    "callback",
    "callback_url",
    "forward",
    "forward_url",
    "proxy_url",
    "proxy_dest",
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
        """編譯代理服務模式"""
        patterns = {}
        for service, (pattern, param) in PROXY_SERVICES.items():
            patterns[service] = {
                'pattern': re.compile(pattern, re.IGNORECASE),
                'param': param
            }
        return patterns
    
    def unpack_url(self, url: str, depth: int = 0, max_depth: int = 3) -> Tuple[str, int, bool]:
        """解包代理網址，提取真實目標URL"""
        if not PROXY_UNPACK_ENABLED:
            return url, 0, False
        
        if depth >= max_depth:
            return url, depth, False
        
        # 檢查快取
        cache_key = f"{url}_{depth}"
        if cache_key in self.unpacked_cache:
            cached_url, cached_depth = self.unpacked_cache[cache_key]
            return cached_url, cached_depth, cached_url != url
        
        original_url = url
        unpacked = self._extract_real_url(url)
        
        if unpacked and unpacked != url:
            # 遞歸解包
            real_url, new_depth, _ = self.unpack_url(unpacked, depth + 1, max_depth)
            self.unpacked_cache[cache_key] = (real_url, new_depth)
            return real_url, new_depth, True
        
        self.unpacked_cache[cache_key] = (url, depth)
        return url, depth, False
    
    def _extract_real_url(self, url: str) -> Optional[str]:
        """從代理網址中提取真實URL"""
        try:
            parsed = urlparse(url)
            
            # 方法1: 通過查詢參數提取
            params = parse_qs(parsed.query)
            
            # 檢查常見的代理參數
            for param in PROXY_PARAMS:
                if param in params:
                    real_url = params[param][0]
                    real_url = unquote(real_url)
                    if self._is_valid_url(real_url):
                        return real_url
            
            # 方法2: 通過路徑提取
            path_patterns = [
                r'/proxy/(https?://[^\s/]+.*)',
                r'/proxies/(https?://[^\s/]+.*)',
                r'/api/proxy/(https?://[^\s/]+.*)',
                r'/get/(https?://[^\s/]+.*)',
                r'/fetch/(https?://[^\s/]+.*)',
                r'/redirect/(https?://[^\s/]+.*)',
                r'/go/(https?://[^\s/]+.*)',
                r'/link/(https?://[^\s/]+.*)',
                r'/out/(https?://[^\s/]+.*)',
            ]
            
            for pattern in path_patterns:
                match = re.search(pattern, parsed.path)
                if match:
                    real_url = unquote(match.group(1))
                    if self._is_valid_url(real_url):
                        return real_url
            
            # 方法3: 通過域名模式識別
            domain = parsed.netloc.lower()
            for service, info in self.proxy_patterns.items():
                if info['pattern'].search(domain):
                    param = info['param']
                    if param in params:
                        real_url = unquote(params[param][0])
                        if self._is_valid_url(real_url):
                            return real_url
            
            # 方法4: 提取URL編碼的完整URL
            if parsed.path and '%3A' in parsed.path or '%2F' in parsed.path:
                try:
                    decoded_path = unquote(parsed.path)
                    url_match = re.search(r'https?://[^\s<>"\']+', decoded_path)
                    if url_match:
                        real_url = url_match.group(0)
                        if self._is_valid_url(real_url):
                            return real_url
                except Exception:
                    pass
            
            return None
            
        except Exception:
            return None
    
    def _is_valid_url(self, url: str) -> bool:
        """驗證URL是否有效"""
        if not url:
            return False
        try:
            parsed = urlparse(url)
            return parsed.scheme in ('http', 'https') and bool(parsed.netloc)
        except Exception:
            return False
    
    def get_proxy_type(self, url: str) -> Optional[str]:
        """檢測代理類型"""
        try:
            parsed = urlparse(url)
            domain = parsed.netloc.lower()
            
            for service, info in self.proxy_patterns.items():
                if info['pattern'].search(domain):
                    return service
            
            params = parse_qs(parsed.query)
            for param in PROXY_PARAMS:
                if param in params:
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
        
        # 建立 Session
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT})
        
        # 設定連線池
        adapter = requests.adapters.HTTPAdapter(
            pool_connections=MAX_WORKERS,
            pool_maxsize=MAX_WORKERS,
            max_retries=RETRY
        )
        self.session.mount('http://', adapter)
        self.session.mount('https://', adapter)
        
        # 初始化解包器
        self.unpacker = URLUnpacker()

    # ========================================================================
    # 檔案 I/O
    # ========================================================================

    def load(self) -> List[str]:
        """載入輸入檔案"""
        p = Path(INPUT_FILE)
        if not p.exists():
            raise FileNotFoundError(f"輸入檔案不存在: {INPUT_FILE}")
        
        content = p.read_text(encoding="utf-8", errors="ignore")
        lines = content.splitlines()
        
        print(f"📂 載入 {len(lines)} 行資料")
        return lines

    def save(self, lines: List[str]) -> None:
        """儲存清理後的檔案"""
        output_path = Path(OUTPUT_FILE)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        
        if BACKUP_ENABLED and output_path.exists():
            self._backup_file(output_path)
        
        filtered_lines = []
        url_pattern = re.compile(r'https?://[^\s<>"\']+')
        
        for line in lines:
            stripped = line.strip()
            
            if not stripped:
                self.empty_lines += 1
                continue
            
            if not url_pattern.search(line):
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
        """備份檔案"""
        history_path = Path(HISTORY_DIR)
        history_path.mkdir(parents=True, exist_ok=True)
        ts = time.strftime("%Y%m%d-%H%M%S")
        shutil.copy2(file_path, history_path / f"backup_{ts}.txt")

    def save_invalid(self) -> None:
        """儲存失效網址"""
        if self.invalid_urls:
            Path(INVALID_FILE).write_text(
                "\n".join(self.invalid_urls),
                encoding="utf-8"
            )

    def save_duplicate(self) -> None:
        """儲存重複網址"""
        if self.duplicate_urls:
            Path(DUPLICATE_FILE).write_text(
                "\n".join(self.duplicate_urls),
                encoding="utf-8"
            )

    def save_proxy(self) -> None:
        """儲存代理網址"""
        if self.proxy_urls:
            Path(PROXY_FILE).write_text(
                "\n".join(self.proxy_urls),
                encoding="utf-8"
            )

    def save_unpacked(self) -> None:
        """儲存解包後的URL"""
        if self.unpacked_urls:
            Path(UNPROXY_FILE).write_text(
                "\n".join(self.unpacked_urls),
                encoding="utf-8"
            )

    # ========================================================================
    # URL 處理
    # ========================================================================

    def extract_urls(self, line: str) -> List[str]:
        """從一行文字中提取所有 URL"""
        return URL_PATTERN.findall(line)

    def process_url(self, url: str) -> Optional[CheckResult]:
        """處理單個 URL"""
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
                    return CheckResult(
                        url=real_url,
                        original_url=url,
                        unpacked_url=real_url,
                        unpack_depth=depth,
                        is_valid=False,
                        is_proxy=True,
                        proxy_type=proxy_type,
                        error_message="解包後URL重複"
                    )
                
                self.seen_urls.add(real_url)
                
                is_valid = self.check_url(real_url)
                if is_valid:
                    self.valid += 1
                    return CheckResult(
                        url=real_url,
                        original_url=url,
                        unpacked_url=real_url,
                        unpack_depth=depth,
                        is_valid=True,
                        is_proxy=True,
                        proxy_type=proxy_type
                    )
                else:
                    self.invalid += 1
                    self.invalid_urls.append(real_url)
                    return CheckResult(
                        url=real_url,
                        original_url=url,
                        unpacked_url=real_url,
                        unpack_depth=depth,
                        is_valid=False,
                        is_proxy=True,
                        proxy_type=proxy_type,
                        error_message="解包後URL無效"
                    )
            else:
                self.proxy_count += 1
                self.proxy_urls.append(url)
                return CheckResult(
                    url=url,
                    is_valid=False,
                    is_proxy=True,
                    proxy_type=proxy_type,
                    error_message="無法解包代理URL"
                )
        
        if is_proxy:
            self.proxy_count += 1
            self.proxy_urls.append(url)
        
        if url in self.seen_urls:
            self.duplicate += 1
            self.duplicate_urls.append(url)
            return CheckResult(
                url=url,
                is_valid=False,
                is_proxy=is_proxy,
                proxy_type=proxy_type,
                error_message="重複 URL"
            )
        
        self.seen_urls.add(url)
        
        if url in self.url_status:
            is_valid = self.url_status[url]
        else:
            is_valid = self.check_url(url)
            self.url_status[url] = is_valid
        
        if is_valid:
            self.valid += 1
            return CheckResult(url=url, is_valid=True, is_proxy=is_proxy, proxy_type=proxy_type)
        else:
            self.invalid += 1
            self.invalid_urls.append(url)
            return CheckResult(
                url=url,
                is_valid=False,
                is_proxy=is_proxy,
                proxy_type=proxy_type,
                error_message="連線失敗或內容無效"
            )

    # ========================================================================
    # 主要檢查流程
    # ========================================================================

    def check_all(self) -> None:
        """執行所有檢查流程"""
        lines = self.load()
        line_results: List[LineResult] = []
        all_tasks = []
        
        print(f"🔍 開始檢查網址有效性...")
        if PROXY_UNPACK_ENABLED:
            print(f"🔄 代理解包已啟用 (最大深度: {PROXY_UNPACK_DEPTH})")
        
        for line_num, line in enumerate(lines, 1):
            urls = self.extract_urls(line)
            
            if not urls:
                line_results.append(LineResult(
                    original_line=line,
                    cleaned_line=line,
                    urls=[]
                ))
                continue
            
            line_result = LineResult(
                original_line=line,
                cleaned_line=line,
                urls=urls
            )
            
            for url in urls:
                future = self._submit_check_task(url)
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
                            line_result.cleaned_line = line_result.cleaned_line.replace(
                                url, result.unpacked_url
                            )
                        else:
                            line_result.invalid_urls.append(result.unpacked_url)
                            line_result.cleaned_line = line_result.cleaned_line.replace(
                                url, ""
                            )
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

    def _submit_check_task(self, url: str):
        """提交檢查任務到執行緒池"""
        executor = ThreadPoolExecutor(max_workers=MAX_WORKERS)
        return executor.submit(self.check_url, url)

    # ========================================================================
    # URL 有效性檢查
    # ========================================================================

    def check_url(self, url: str) -> bool:
        """檢查網址是否有效"""
        for attempt in range(RETRY):
            try:
                try:
                    head_response = self.session.head(
                        url,
                        timeout=TIMEOUT,
                        allow_redirects=True,
                        verify=False
                    )
                    if head_response.status_code < 400:
                        pass
                except Exception:
                    pass
                
                response = self.session.get(
                    url,
                    timeout=TIMEOUT,
                    allow_redirects=True,
                    stream=True,
                    verify=False,
                    headers={
                        'Accept': '*/*',
                        'Accept-Encoding': 'gzip, deflate',
                        'Connection': 'keep-alive'
                    }
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
                
            except (requests.exceptions.Timeout,
                    requests.exceptions.ConnectionError,
                    requests.exceptions.SSLError,
                    requests.exceptions.ProxyError,
                    requests.exceptions.RequestException,
                    socket.gaierror,
                    socket.timeout):
                if attempt < RETRY - 1:
                    time.sleep(0.5 * (attempt + 1))
                    continue
                return False
        
        return False

    def _read_content(self, response: requests.Response, max_size: int = 2048) -> str:
        """安全地讀取回應內容"""
        content = ""
        try:
            for chunk in response.iter_content(chunk_size=512):
                if chunk:
                    try:
                        content += chunk.decode('utf-8', errors='ignore')
                        if len(content) >= max_size:
                            break
                    except Exception:
                        pass
        except Exception:
            pass
        return content

    # ========================================================================
    # 內容驗證
    # ========================================================================

    def validate_content(self, url: str, content: str) -> bool:
        """根據 URL 副檔名驗證內容"""
        if not content or len(content.strip()) < 10:
            return False
        
        url_lower = url.lower()
        
        if url_lower.endswith('.json'):
            return self._validate_json(content)
        elif url_lower.endswith('.xml'):
            return self._validate_xml(content)
        elif url_lower.endswith(('.m3u', '.m3u8')):
            return self._validate_m3u(content)
        elif url_lower.endswith('.txt'):
            return self._validate_txt(content)
        else:
            return self._validate_common(content)

    def _validate_common(self, content: str) -> bool:
        """通用內容驗證"""
        content_lower = content.lower()
        
        for keyword in INVALID_KEYWORDS:
            if keyword in content_lower:
                return False
        
        content_stripped = content.strip()
        if len(content_stripped) < 20:
            return False
        
        tvbox_indicators = [
            'url', 'name', 'title', 'channel', 'group',
            'http', 'https', '://', 'm3u8', 'flv'
        ]
        
        indicator_count = sum(1 for ind in tvbox_indicators if ind in content_lower)
        
        return indicator_count >= 2

    def _validate_json(self, content: str) -> bool:
        """JSON 內容驗證"""
        content = content.strip()
        if not content or len(content) < 20:
            return False
        
        if '<html' in content.lower():
            return False
        
        try:
            data = json.loads(content)
            if isinstance(data, dict):
                if any(key in data for key in ['urls', 'channels', 'sites', 'apps']):
                    return True
                if len(data) >= 2:
                    return True
            elif isinstance(data, list):
                return len(data) > 0
            return False
        except json.JSONDecodeError:
            return False

    def _validate_xml(self, content: str) -> bool:
        """XML 內容驗證"""
        content_lower = content.lower()
        
        xml_indicators = ['<?xml', '<tv', '<rss', '<channel', '<item', '<title']
        
        has_xml_tag = any(indicator in content_lower for indicator in xml_indicators)
        has_url_or_channel = 'http' in content_lower or 'channel' in content_lower
        
        return has_xml_tag and has_url_or_channel

    def _validate_m3u(self, content: str) -> bool:
        """M3U 內容驗證"""
        content_upper = content.upper()
        
        if '#EXTM3U' not in content_upper:
            return False
        
        has_entries = '#EXTINF:' in content_upper
        has_url = 'HTTP' in content_upper
        
        return has_entries and has_url

    def _validate_txt(self, content: str) -> bool:
        """TXT 內容驗證"""
        content_lower = content.lower()
        
        for keyword in ['404', 'forbidden', 'access denied', 'nginx', '<html', 'error']:
            if keyword in content_lower:
                return False
        
        lines = [line.strip() for line in content.splitlines() if line.strip()]
        
        if not lines:
            return False
        
        has_url = any(URL_PATTERN.search(line) for line in lines)
        
        return has_url

    # ========================================================================
    # 報告生成
    # ========================================================================

    def generate_report(self) -> None:
        """生成檢查報告（含代理解包統計）"""
        lines = [
            "# 📊 TVBox URL 檢查報告",
            "",
            "## 📈 統計摘要",
            "",
            f"| 項目 | 數量 | 比例 |",
            f"|------|------|------|",
            f"| 總網址數 | {self.total} | 100% |",
            f"| ✅ 有效 | {self.valid} | {(self.valid/self.total*100):.1f}%" if self.total > 0 else "| ✅ 有效 | 0 | 0% |",
            f"| ❌ 失效 | {self.invalid} | {(self.invalid/self.total*100):.1f}%" if self.total > 0 else "| ❌ 失效 | 0 | 0% |",
            f"| 🔄 重複 | {self.duplicate} | {(self.duplicate/self.total*100):.1f}%" if self.total > 0 else "| 🔄 重複 | 0 | 0% |",
            f"| 🛡️ 代理 | {self.proxy_count} | {(self.proxy_count/self.total*100):.1f}%" if self.total > 0 else "| 🛡️ 代理 | 0 | 0% |",
            f"| 🔓 解包 | {self.unpacked_count} | {(self.unpacked_count/self.total*100):.1f}%" if self.total > 0 else "| 🔓 解包 | 0 | 0% |",
            "",
            "## 🧹 清理統計",
            "",
            f"- **移除空白行**：{self.empty_lines} 行",
            f"- **移除無網址行**：{self.no_url_lines} 行",
            "",
            f"## ✅ 有效網址 ({self.valid})",
            "",
            f"有效網址已儲存至：`{OUTPUT_FILE}`",
            "",
        ]
        
        if self.unpacked_urls:
            lines.extend([
                "## 🔓 成功解包的URL",
                "",
                f"共成功解包 **{len(self.unpacked_urls)}** 個代理網址，已還原為真實URL。",
                "",
            ])
            for url in self.unpacked_urls[:30]:
                lines.append(f"- `{url}`")
            if len(self.unpacked_urls) > 30:
                lines.append(f"- ... 還有 {len(self.unpacked_urls) - 30} 個")
            lines.append(f"完整清單請查看：`{UNPROXY_FILE}`")
            lines.append("")
        
        if self.proxy_urls:
            lines.extend([
                "## 🛡️ 無法解包的代理網址",
                "",
                f"共發現 **{len(self.proxy_urls)}** 個無法解包的代理網址（已移除）。",
                "",
            ])
            for url in self.proxy_urls[:30]:
                lines.append(f"- `{url}`")
            if len(self.proxy_urls) > 30:
                lines.append(f"- ... 還有 {len(self.proxy_urls) - 30} 個")
            lines.append(f"完整清單請查看：`{PROXY_FILE}`")
            lines.append("")
        
        lines.extend([
            "## ❌ 無效網址列表",
            "",
        ])
        
        if self.invalid_urls:
            for url in self.invalid_urls[:30]:
                lines.append(f"- `{url}`")
            if len(self.invalid_urls) > 30:
                lines.append(f"- ... 還有 {len(self.invalid_urls) - 30} 個")
            lines.append(f"完整清單請查看：`{INVALID_FILE}`")
        else:
            lines.append("✅ 沒有無效網址")
        
        lines.extend([
            "",
            "## 🔄 重複網址列表",
            ""
        ])
        
        if self.duplicate_urls:
            for url in self.duplicate_urls[:30]:
                lines.append(f"- `{url}`")
            if len(self.duplicate_urls) > 30:
                lines.append(f"- ... 還有 {len(self.duplicate_urls) - 30} 個")
            lines.append(f"完整清單請查看：`{DUPLICATE_FILE}`")
        else:
            lines.append("✅ 沒有重複網址")
        
        lines.extend([
            "",
            "---",
            f"🕐 更新時間：{time.strftime('%Y-%m-%d %H:%M:%S')}",
            "",
            "✅ 報告由 TVBox URL Checker Pro v4 自動生成"
        ])
        
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
        
        # 輸出結果
        print("\n" + "=" * 70)
        print("✅ 檢查完成！")
        print("=" * 70)
        print(f"📊 總網址 : {checker.total}")
        print(f"✅ 有效   : {checker.valid}")
        print(f"❌ 失效   : {checker.invalid}")
        print(f"🔄 重複   : {checker.duplicate}")
        print(f"🛡️ 代理   : {checker.proxy_count}")
        print(f"🔓 解包   : {checker.unpacked_count}")
        print(f"🧹 移除空白行 : {checker.empty_lines}")
        print(f"🧹 移除無網址行 : {checker.no_url_lines}")
        print(f"⏱️ 耗時   : {time.time() - start_time:.2f} 秒")
        print("=" * 70)
        print(f"\n📁 輸出檔案：")
        print(f"  - 有效清單: {OUTPUT_FILE}")
        print(f"  - 無效清單: {INVALID_FILE}")
        print(f"  - 重複清單: {DUPLICATE_FILE}")
        print(f"  - 代理清單: {PROXY_FILE}")
        print(f"  - 解包清單: {UNPROXY_FILE}")
        print(f"  - 檢查報告: {REPORT_FILE}")
        
    except KeyboardInterrupt:
        print("\n\n⚠️ 使用者中斷執行")
    except Exception as e:
        print(f"\n❌ 錯誤：{e}")
        import traceback
        traceback.print_exc()

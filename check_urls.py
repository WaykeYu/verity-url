#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
TVBox URL Checker Pro v3
源地址優先版 - 優先使用 GitHub 原始地址，代理作為備用

改進重點：
✓ 優先使用 GitHub 原始地址 (raw.githubusercontent.com)
✓ 代理服務作為備用方案
✓ 智慧型 URL 重寫
✓ 詳細的診斷日誌
✓ 內容快取機制
"""

from __future__ import annotations
import json
import re
import shutil
import socket
import time
import hashlib
import random
from typing import List, Tuple, Dict, Any, Optional
from urllib.parse import urlparse
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
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
REPORT_FILE = cfg.get("report", "data/report.md")
MAX_WORKERS = cfg.get("workers", 20)
TIMEOUT = cfg.get("timeout", 15)
RETRY = cfg.get("retry", 3)

# ============================================================================
# GitHub 代理服務配置（備用）
# ============================================================================

GITHUB_PROXIES = [
    "https://gh-proxy.com",
    "https://ghproxy.net",
    "https://mirror.ghproxy.com",
    "https://gh.api.99988866.xyz",
]

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
]

# ============================================================================
# URL 工具函數
# ============================================================================

def extract_github_raw_url(url: str) -> Optional[str]:
    """
    從各種 GitHub URL 格式中提取原始 raw 地址
    
    支援格式：
    1. https://raw.githubusercontent.com/... (已是原始地址)
    2. https://github.com/.../raw/... (GitHub raw 地址)
    3. https://gh-proxy.org/https://raw.githubusercontent.com/... (代理地址)
    4. https://gh-proxy.org/https://github.com/.../raw/... (代理地址)
    """
    # 如果已經是 raw.githubusercontent.com，直接返回
    if 'raw.githubusercontent.com' in url:
        return url
    
    # 檢查是否為代理 URL，嘗試提取原始地址
    for proxy in GITHUB_PROXIES:
        if url.startswith(proxy):
            # 移除代理前綴
            remaining = url[len(proxy):]
            # 移除開頭的 /
            if remaining.startswith('/'):
                remaining = remaining[1:]
            
            # 檢查是否包含 raw.githubusercontent.com
            if 'raw.githubusercontent.com' in remaining:
                # 確保有 https:// 前綴
                if remaining.startswith('https://'):
                    return remaining
                else:
                    return f"https://{remaining}"
            
            # 檢查是否包含 github.com/.../raw/...
            if 'github.com' in remaining and '/raw/' in remaining:
                # 轉換為 raw.githubusercontent.com 格式
                # 例如: github.com/niuber/niuber/raw/refs/heads/main/... 
                # 轉換為: raw.githubusercontent.com/niuber/niuber/refs/heads/main/...
                github_match = re.search(r'github\.com/([^/]+/[^/]+)/raw/(.+)', remaining)
                if github_match:
                    user_repo = github_match.group(1)
                    path = github_match.group(2)
                    return f"https://raw.githubusercontent.com/{user_repo}/{path}"
    
    # 檢查是否為 github.com/.../raw/... 格式
    github_raw_match = re.search(r'github\.com/([^/]+/[^/]+)/raw/(.+)', url)
    if github_raw_match:
        user_repo = github_raw_match.group(1)
        path = github_raw_match.group(2)
        return f"https://raw.githubusercontent.com/{user_repo}/{path}"
    
    # 如果不是 GitHub URL，返回 None
    if 'github.com' not in url and 'raw.githubusercontent.com' not in url:
        return None
    
    return url

def is_github_url(url: str) -> bool:
    """判斷是否為 GitHub 相關 URL"""
    url_lower = url.lower()
    return 'github.com' in url_lower or 'raw.githubusercontent.com' in url_lower

def get_github_proxy_alternatives(original_url: str) -> List[str]:
    """生成 GitHub URL 的代理備用地址"""
    alternatives = []
    
    # 如果是 raw URL，生成代理版本
    if 'raw.githubusercontent.com' in original_url:
        path = original_url.replace('https://raw.githubusercontent.com', '')
        for proxy in GITHUB_PROXIES:
            alternatives.append(f"{proxy}/https://raw.githubusercontent.com{path}")
    
    # 如果是普通 GitHub URL，生成 raw 版本和代理版本
    elif 'github.com' in original_url and '/raw/' in original_url:
        # 先轉換為 raw 地址
        raw_url = extract_github_raw_url(original_url)
        if raw_url and raw_url != original_url:
            alternatives.append(raw_url)
            # 再生成 raw 地址的代理版本
            path = raw_url.replace('https://raw.githubusercontent.com', '')
            for proxy in GITHUB_PROXIES:
                alternatives.append(f"{proxy}/https://raw.githubusercontent.com{path}")
    
    return alternatives

# ============================================================================
# 智慧型 URL 檢查器
# ============================================================================

class SmartURLChecker:
    """智慧型 URL 檢查器 - 源地址優先"""
    
    def __init__(self):
        self.session = self._create_session()
        
        # 快取機制
        self.cache = {}
        self.cache_ttl = 1800  # 30分鐘
        
        # 統計資料
        self.stats = {
            'raw_success': 0,
            'raw_failed': 0,
            'proxy_success': 0,
            'proxy_failed': 0,
            'cache_hit': 0,
            'total_attempts': 0,
            'urls_processed': 0,
        }
        
        # 請求頭模板
        self.base_headers = {
            "Accept": "*/*",
            "Accept-Encoding": "gzip, deflate",
            "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8",
            "Connection": "keep-alive",
            "Cache-Control": "no-cache",
            "User-Agent": random.choice(USER_AGENTS),
        }
    
    def _create_session(self) -> requests.Session:
        """創建配置好的 Session"""
        session = requests.Session()
        
        retry_strategy = requests.adapters.Retry(
            total=2,
            backoff_factor=0.5,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["HEAD", "GET"]
        )
        
        adapter = requests.adapters.HTTPAdapter(
            pool_connections=MAX_WORKERS * 2,
            pool_maxsize=MAX_WORKERS * 2,
            max_retries=retry_strategy
        )
        
        session.mount('http://', adapter)
        session.mount('https://', adapter)
        
        return session

    def check_url(self, url: str) -> Tuple[bool, Dict[str, Any]]:
        """
        智慧型檢查 URL
        策略：
        1. 檢查快取
        2. 如果是 GitHub URL，優先使用原始 raw 地址
        3. 如果 raw 地址失敗，嘗試代理備用
        4. 一般 URL 使用標準檢查
        """
        result = {
            'url': url,
            'valid': False,
            'method': '',
            'status_code': None,
            'response_time': None,
            'error': None,
            'url_used': url,
            'attempts': [],
            'content_preview': None,
            'content_type': None,
        }
        
        self.stats['urls_processed'] += 1
        
        # 檢查快取
        cache_key = hashlib.md5(url.encode()).hexdigest()
        if cache_key in self.cache:
            cached_time, cached_result = self.cache[cache_key]
            if time.time() - cached_time < self.cache_ttl:
                self.stats['cache_hit'] += 1
                return cached_result[0], cached_result[1]
        
        # 判斷 URL 類型
        is_github = is_github_url(url)
        
        if is_github:
            # GitHub URL：優先使用原始 raw 地址
            is_valid, result = self._check_github_url(url, result)
        else:
            # 一般 URL：標準檢查
            is_valid, result = self._check_normal_url(url, result)
        
        # 更新快取
        self.cache[cache_key] = (time.time(), (is_valid, result))
        
        return is_valid, result
    
    def _check_github_url(self, url: str, result: Dict) -> Tuple[bool, Dict]:
        """檢查 GitHub URL（源地址優先）"""
        
        # 1. 嘗試提取原始 raw 地址
        raw_url = extract_github_raw_url(url)
        
        if raw_url and raw_url != url:
            result['method'] = 'raw'
            result['url_used'] = raw_url
            
            is_valid, attempt_result = self._attempt_check(raw_url)
            result['attempts'].append(attempt_result)
            
            if is_valid:
                self.stats['raw_success'] += 1
                result['valid'] = True
                return True, result
            else:
                self.stats['raw_failed'] += 1
                result['error'] = f"Raw 地址訪問失敗: {attempt_result.get('error', '未知錯誤')}"
        
        # 2. 如果 raw 地址失敗，嘗試原始 URL（如果不同的話）
        if url != raw_url:
            result['method'] = 'original'
            result['url_used'] = url
            
            is_valid, attempt_result = self._attempt_check(url)
            result['attempts'].append(attempt_result)
            
            if is_valid:
                self.stats['raw_success'] += 1
                result['valid'] = True
                return True, result
        
        # 3. 嘗試代理備用地址
        proxy_alternatives = get_github_proxy_alternatives(url)
        for proxy_url in proxy_alternatives:
            if proxy_url in [a.get('url_used') for a in result['attempts']]:
                continue
            
            result['method'] = 'proxy'
            result['url_used'] = proxy_url
            
            is_valid, attempt_result = self._attempt_check(proxy_url)
            result['attempts'].append(attempt_result)
            
            if is_valid:
                self.stats['proxy_success'] += 1
                result['valid'] = True
                return True, result
            else:
                self.stats['proxy_failed'] += 1
        
        # 所有策略都失敗
        result['valid'] = False
        if not result.get('error'):
            result['error'] = "所有訪問方式均失敗（raw、原始、代理）"
        return False, result
    
    def _check_normal_url(self, url: str, result: Dict) -> Tuple[bool, Dict]:
        """檢查一般 URL"""
        result['method'] = 'direct'
        result['url_used'] = url
        
        is_valid, attempt_result = self._attempt_check(url)
        result['attempts'].append(attempt_result)
        
        if is_valid:
            result['valid'] = True
            return True, result
        else:
            result['valid'] = False
            result['error'] = attempt_result.get('error', '訪問失敗')
            return False, result
    
    def _attempt_check(self, url: str) -> Tuple[bool, Dict]:
        """
        執行單次檢查嘗試
        """
        attempt_result = {
            'url_used': url,
            'success': False,
            'status_code': None,
            'response_time': None,
            'error': None,
            'content_preview': None,
            'content_type': None,
            'content_length': 0,
            'attempt_number': 0,
        }
        
        headers = {
            **self.base_headers,
            "User-Agent": random.choice(USER_AGENTS),
        }
        
        for attempt in range(RETRY):
            attempt_result['attempt_number'] = attempt + 1
            self.stats['total_attempts'] += 1
            
            try:
                start_time = time.time()
                
                # 先嘗試 HEAD
                try:
                    response = self.session.head(
                        url,
                        timeout=TIMEOUT,
                        allow_redirects=True,
                        headers=headers
                    )
                    
                    if response.status_code < 400:
                        # HEAD 成功，記錄資訊
                        attempt_result['status_code'] = response.status_code
                        attempt_result['content_type'] = response.headers.get('content-type', '')
                        content_length = response.headers.get('content-length')
                        if content_length:
                            attempt_result['content_length'] = int(content_length)
                    else:
                        # HEAD 失敗，嘗試 GET
                        response = self.session.get(
                            url,
                            timeout=TIMEOUT,
                            allow_redirects=True,
                            stream=True,
                            headers=headers
                        )
                except Exception:
                    # HEAD 出錯，使用 GET
                    if attempt > 0:
                        time.sleep(0.5 * attempt)
                    
                    response = self.session.get(
                        url,
                        timeout=TIMEOUT,
                        allow_redirects=True,
                        stream=True,
                        headers=headers
                    )
                
                response_time = time.time() - start_time
                attempt_result['response_time'] = round(response_time, 3)
                attempt_result['status_code'] = response.status_code
                attempt_result['content_type'] = response.headers.get('content-type', '')
                
                # 檢查狀態碼
                if response.status_code >= 400:
                    attempt_result['error'] = f"HTTP {response.status_code}"
                    if response.status_code == 429:
                        time.sleep(2 * (attempt + 1))
                    continue
                
                # 檢查 Content-Length
                content_length = response.headers.get('content-length')
                if content_length:
                    attempt_result['content_length'] = int(content_length)
                    if attempt_result['content_length'] == 0:
                        attempt_result['error'] = "內容長度為 0"
                        continue
                
                # 讀取部分內容
                content = ""
                try:
                    content_limit = 8192 if url.endswith('.json') else 4096
                    for chunk in response.iter_content(chunk_size=1024):
                        if chunk:
                            content += chunk.decode('utf-8', errors='ignore')
                            if len(content) >= content_limit:
                                break
                except Exception as e:
                    attempt_result['error'] = f"內容讀取錯誤: {str(e)[:30]}"
                    continue
                
                attempt_result['content_preview'] = content[:200]
                
                # 驗證內容
                is_valid = self._validate_content(url, content)
                if is_valid:
                    attempt_result['success'] = True
                    return True, attempt_result
                else:
                    attempt_result['error'] = "內容驗證失敗"
                    continue
                
            except requests.exceptions.Timeout:
                attempt_result['error'] = "超時"
                if attempt < RETRY - 1:
                    time.sleep(1 * (attempt + 1))
                continue
                
            except requests.exceptions.ConnectionError:
                attempt_result['error'] = "連線錯誤"
                if attempt < RETRY - 1:
                    time.sleep(0.5 * (attempt + 1))
                continue
                
            except requests.exceptions.SSLError:
                attempt_result['error'] = "SSL 錯誤"
                if attempt == 0:
                    try:
                        response = self.session.get(
                            url,
                            timeout=TIMEOUT,
                            verify=False,
                            allow_redirects=True,
                            headers=headers
                        )
                        if response.status_code < 400:
                            attempt_result['success'] = True
                            attempt_result['error'] = "SSL 驗證失敗但內容可存取"
                            return True, attempt_result
                    except:
                        pass
                continue
                
            except Exception as e:
                attempt_result['error'] = str(e)[:50]
                if attempt < RETRY - 1:
                    time.sleep(0.5 * (attempt + 1))
                continue
        
        return False, attempt_result
    
    def _validate_content(self, url: str, content: str) -> bool:
        """驗證內容"""
        if not content or len(content.strip()) < 10:
            return False
        
        content_lower = content.lower()
        
        # 檢查錯誤頁面
        error_patterns = [
            '404: not found', '404 not found',
            'access denied', 'forbidden',
            'rate limit', 'too many requests',
            '<html', '<!doctype html',
            'nginx', 'apache',
            'error occurred', 'internal server error',
            'service unavailable', 'bad gateway'
        ]
        
        for pattern in error_patterns:
            if pattern in content_lower:
                return False
        
        # 根據檔案類型驗證
        url_lower = url.lower()
        
        # JSON 驗證
        if url_lower.endswith('.json'):
            try:
                data = json.loads(content)
                if isinstance(data, dict):
                    tvbox_fields = ['spider', 'sites', 'lives', 'parses']
                    has_fields = any(f in data for f in tvbox_fields)
                    return has_fields or len(data) > 0
                elif isinstance(data, list):
                    return len(data) > 0
                return False
            except json.JSONDecodeError:
                return False
        
        # M3U 驗證
        if url_lower.endswith(('.m3u', '.m3u8')):
            return '#EXTM3U' in content.upper()
        
        # XML 驗證
        if url_lower.endswith('.xml'):
            return ('<?xml' in content_lower or 
                    '<tv' in content_lower or 
                    '<rss' in content_lower)
        
        # TXT 驗證
        if url_lower.endswith('.txt'):
            lines = [l.strip() for l in content.splitlines() if l.strip()]
            if len(lines) < 2:
                return False
            url_pattern = re.compile(r'https?://[^\s<>"\']+')
            urls = url_pattern.findall(content)
            return len(urls) > 0 or len(lines) > 3
        
        # 通用驗證
        url_pattern = re.compile(r'https?://[^\s<>"\']+')
        urls = url_pattern.findall(content)
        
        tvbox_keywords = ['tvbox', 'catvod', '影视', '直播', '接口', 'spider', 'parse']
        has_tvbox_keyword = any(keyword in content_lower for keyword in tvbox_keywords)
        
        if has_tvbox_keyword or len(urls) > 3:
            return True
        
        lines = [l.strip() for l in content.splitlines() if l.strip()]
        return len(lines) >= 3
    
    def clear_cache(self):
        """清除快取"""
        self.cache.clear()

# ============================================================================
# 主程式
# ============================================================================

class TVBoxChecker:
    """TVBox URL 檢查器主程式"""
    
    def __init__(self):
        self.checker = SmartURLChecker()
        self.total = 0
        self.valid = 0
        self.invalid = 0
        self.duplicate = 0
        self.empty_lines = 0
        self.no_url_lines = 0
        self.seen = set()
        self.invalid_urls = []
        self.duplicate_urls = []
        self.url_details = {}
        self.method_stats = {}
        
        self.url_pattern = re.compile(r'https?://[^\s<>"\']+')

    def load_lines(self) -> List[str]:
        p = Path(INPUT_FILE)
        if not p.exists():
            raise FileNotFoundError(f"找不到檔案: {INPUT_FILE}")
        return p.read_text(encoding='utf-8', errors='ignore').splitlines()

    def save_lines(self, lines: List[str]) -> List[str]:
        output_path = Path(OUTPUT_FILE)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        
        if output_path.exists():
            history_path = Path("data/history")
            history_path.mkdir(parents=True, exist_ok=True)
            ts = time.strftime("%Y%m%d-%H%M%S")
            shutil.copy2(output_path, history_path / f"backup_{ts}.txt")
        
        filtered = []
        for line in lines:
            stripped = line.strip()
            if not stripped:
                self.empty_lines += 1
                continue
            if not self.url_pattern.search(stripped):
                self.no_url_lines += 1
                continue
            filtered.append(line)
        
        output_path.write_text('\n'.join(filtered), encoding='utf-8')
        return filtered

    def save_invalid(self):
        if self.invalid_urls:
            Path(INVALID_FILE).write_text(
                '\n'.join(self.invalid_urls), encoding='utf-8'
            )

    def save_duplicate(self):
        if self.duplicate_urls:
            Path(DUPLICATE_FILE).write_text(
                '\n'.join(self.duplicate_urls), encoding='utf-8'
            )

    def extract_urls(self, line: str) -> List[str]:
        return self.url_pattern.findall(line)

    def is_duplicate(self, url: str) -> bool:
        if url in self.seen:
            self.duplicate += 1
            self.duplicate_urls.append(url)
            return True
        self.seen.add(url)
        return False

    def check_all(self):
        lines = self.load_lines()
        cleaned_lines = []
        tasks = []

        print(f"📂 載入 {len(lines)} 行資料")
        print(f"🔍 開始智慧型檢查（源地址優先）...")
        print(f"📡 GitHub 代理服務: {len(GITHUB_PROXIES)} 個（備用）")
        print("-" * 60)

        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            for line in lines:
                urls = self.extract_urls(line)
                if not urls:
                    cleaned_lines.append(line)
                    continue

                newline = line
                futures = []

                for url in urls:
                    self.total += 1
                    if self.is_duplicate(url):
                        newline = newline.replace(url, "")
                        continue

                    future = executor.submit(self.checker.check_url, url)
                    futures.append((future, url))

                tasks.append((newline, futures))

            for idx, (newline, futures) in enumerate(tasks):
                for future, url in futures:
                    try:
                        is_valid, details = future.result(timeout=TIMEOUT + 10)
                        
                        self.url_details[url] = details
                        
                        if is_valid:
                            self.valid += 1
                            method = details.get('method', 'unknown')
                            self.method_stats[method] = self.method_stats.get(method, 0) + 1
                        else:
                            self.invalid += 1
                            self.invalid_urls.append(url)
                            newline = newline.replace(url, "")
                            
                            if self.invalid <= 10:
                                error = details.get('error', '未知錯誤')
                                url_used = details.get('url_used', url)
                                print(f"  ⚠️ {url[:60]}... - {error}")
                    except Exception as e:
                        self.invalid += 1
                        self.invalid_urls.append(url)
                        newline = newline.replace(url, "")
                        print(f"  ❌ {url[:60]}... - 檢查異常: {str(e)[:30]}")
                
                cleaned_lines.append(newline)
                
                if (idx + 1) % 10 == 0:
                    progress = (idx + 1) / len(tasks) * 100
                    print(f"  進度: {progress:.1f}% ({idx + 1}/{len(tasks)})")

        print(f"\n💾 儲存結果...")
        final_lines = self.save_lines(cleaned_lines)
        self.save_invalid()
        self.save_duplicate()
        self.generate_report(final_lines)

    def generate_report(self, final_lines: List[str]):
        checker_stats = self.checker.stats
        
        lines = [
            "# 📊 TVBox URL 檢查報告（源地址優先版）",
            "",
            "## 📈 統計摘要",
            "",
            f"| 項目 | 數量 | 比例 |",
            f"|------|------|------|",
            f"| 總網址數 | {self.total} | 100% |",
            f"| ✅ 有效 | {self.valid} | {(self.valid/self.total*100):.1f}%" if self.total > 0 else "| ✅ 有效 | 0 | 0% |",
            f"| ❌ 失效 | {self.invalid} | {(self.invalid/self.total*100):.1f}%" if self.total > 0 else "| ❌ 失效 | 0 | 0% |",
            f"| 🔄 重複 | {self.duplicate} | {(self.duplicate/self.total*100):.1f}%" if self.total > 0 else "| 🔄 重複 | 0 | 0% |",
            "",
            "## 🌐 訪問方式統計",
            "",
        ]
        
        method_names = {
            'raw': 'GitHub Raw (優先)',
            'original': '原始 URL',
            'proxy': '代理服務 (備用)',
            'direct': '直接連接'
        }
        
        for method, count in self.method_stats.items():
            name = method_names.get(method, method)
            lines.append(f"- **{name}**：{count} 個")
        
        lines.extend([
            "",
            "## 📡 檢查器統計",
            "",
            f"- **快取命中**：{checker_stats.get('cache_hit', 0)} 次",
            f"- **Raw 成功**：{checker_stats.get('raw_success', 0)} 次",
            f"- **Raw 失敗**：{checker_stats.get('raw_failed', 0)} 次",
            f"- **代理成功**：{checker_stats.get('proxy_success', 0)} 次",
            f"- **代理失敗**：{checker_stats.get('proxy_failed', 0)} 次",
            "",
            "## 🧹 清理統計",
            "",
            f"- **移除空白行**：{self.empty_lines} 行",
            f"- **移除無網址行**：{self.no_url_lines} 行",
            f"- **保留行數**：{len(final_lines)} 行",
            "",
            "## 📋 無效網址列表",
            "",
        ])
        
        if self.invalid_urls:
            for url in self.invalid_urls[:20]:
                details = self.url_details.get(url, {})
                error = details.get('error', '未知原因')
                method = details.get('method', '未知')
                lines.append(f"- `{url}` - {error} (方式: {method})")
            if len(self.invalid_urls) > 20:
                lines.append(f"- ... 還有 {len(self.invalid_urls) - 20} 個")
        else:
            lines.append("✅ 沒有無效網址")
        
        lines.extend([
            "",
            "---",
            f"🕐 更新時間：{time.strftime('%Y-%m-%d %H:%M:%S')}",
            "",
            "✅ 報告由 TVBox URL Checker Pro v3 (源地址優先版) 自動生成"
        ])
        
        Path(REPORT_FILE).write_text('\n'.join(lines), encoding='utf-8')
        print(f"📄 報告已生成: {REPORT_FILE}")

# ============================================================================
# 主程式
# ============================================================================

if __name__ == "__main__":
    print("=" * 70)
    print("🚀 TVBox URL Checker Pro v3 - 源地址優先版")
    print("=" * 70)
    
    start_time = time.time()
    
    try:
        checker = TVBoxChecker()
        checker.check_all()
        
        print("\n" + "=" * 70)
        print("✅ 檢查完成！")
        print("=" * 70)
        print(f"📊 總網址 : {checker.total}")
        print(f"✅ 有效   : {checker.valid}")
        print(f"❌ 失效   : {checker.invalid}")
        print(f"🔄 重複   : {checker.duplicate}")
        print(f"⏱️ 耗時   : {time.time() - start_time:.2f} 秒")
        print("=" * 70)
        
        print(f"\n📦 快取命中: {checker.checker.stats['cache_hit']} 次")
        print(f"🌐 Raw 成功: {checker.checker.stats['raw_success']} 次")
        print(f"🔄 代理成功: {checker.checker.stats['proxy_success']} 次")
        
    except KeyboardInterrupt:
        print("\n\n⚠️ 使用者中斷執行")
    except Exception as e:
        print(f"\n❌ 錯誤：{e}")
        import traceback
        traceback.print_exc()

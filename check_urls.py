#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
TVBox URL Checker Pro v3
網路優化版 - 針對 GitHub 代理服務優化

改進重點：
✓ 支援 GitHub 代理服務檢測
✓ 智能重試策略（區分不同錯誤類型）
✓ 代理服務健康檢查
✓ 多重驗證機制
✓ URL 重寫和備用方案
✓ 詳細的診斷日誌
"""

from __future__ import annotations
import json
import re
import shutil
import socket
import time
import hashlib
from typing import List, Tuple, Dict, Any, Optional, Set
from urllib.parse import urlparse, urlunparse
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
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
MAX_WORKERS = cfg.get("workers", 20)  # 降低並發數避免被限制
TIMEOUT = cfg.get("timeout", 15)      # 增加超時時間
RETRY = cfg.get("retry", 3)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/119.0.0.0 Safari/537.36"
)

# ============================================================================
# GitHub 代理服務配置
# ============================================================================

GITHUB_PROXIES = [
    "https://gh-proxy.com",
    "https://ghproxy.net",
    "https://mirror.ghproxy.com",
    "https://gh.api.99988866.xyz",
    "https://git.yumenaka.net",
]

class GitHubProxyManager:
    """GitHub 代理服務管理器"""
    
    def __init__(self):
        self.proxies = GITHUB_PROXIES
        self.healthy_proxies = []
        self.proxy_cache = {}
        self.last_check = 0
        self.check_interval = 300  # 5分鐘檢查一次
        
    def get_proxy(self, url: str) -> str:
        """獲取可用的代理 URL"""
        # 如果不是 GitHub URL，直接返回原 URL
        if 'github.com' not in url and 'raw.githubusercontent.com' not in url:
            return url
        
        # 檢查是否已經是代理 URL
        for proxy in self.proxies:
            if url.startswith(proxy):
                return url
        
        # 嘗試獲取可用的代理
        available_proxies = self._get_healthy_proxies()
        if available_proxies:
            # 將原始 GitHub URL 轉換為代理 URL
            return self._convert_to_proxy_url(url, available_proxies[0])
        
        # 如果沒有可用代理，返回原始 URL
        return url
    
    def _get_healthy_proxies(self) -> List[str]:
        """獲取健康的代理列表"""
        current_time = time.time()
        
        # 如果快取未過期，直接返回
        if current_time - self.last_check < self.check_interval:
            return self.healthy_proxies
        
        # 檢查所有代理
        self.healthy_proxies = []
        for proxy in self.proxies:
            if self._check_proxy_health(proxy):
                self.healthy_proxies.append(proxy)
        
        self.last_check = current_time
        return self.healthy_proxies
    
    def _check_proxy_health(self, proxy: str) -> bool:
        """檢查代理是否健康"""
        test_url = f"{proxy}/https://raw.githubusercontent.com/niuber/niuber/refs/heads/main/tvbox/TVBoxOSC/tvbox/api.json"
        
        try:
            response = requests.head(
                test_url,
                timeout=5,
                allow_redirects=True,
                headers={"User-Agent": USER_AGENT}
            )
            return response.status_code < 400
        except:
            return False
    
    def _convert_to_proxy_url(self, original_url: str, proxy: str) -> str:
        """將 GitHub URL 轉換為代理 URL"""
        # 如果是 raw.githubusercontent.com，轉換為代理格式
        if 'raw.githubusercontent.com' in original_url:
            path = original_url.replace('https://raw.githubusercontent.com', '')
            return f"{proxy}/https://raw.githubusercontent.com{path}"
        
        # 如果是 github.com，轉換為代理格式
        if 'github.com' in original_url:
            path = original_url.replace('https://github.com', '')
            return f"{proxy}/https://github.com{path}"
        
        return original_url

# ============================================================================
# 智慧型 URL 檢查器
# ============================================================================

class SmartURLChecker:
    """智慧型 URL 檢查器 - 針對代理服務優化"""
    
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT})
        
        # 設定重試策略
        retry_strategy = requests.adapters.Retry(
            total=2,
            backoff_factor=0.5,
            status_forcelist=[429, 500, 502, 503, 504]
        )
        adapter = requests.adapters.HTTPAdapter(
            pool_connections=MAX_WORKERS,
            pool_maxsize=MAX_WORKERS,
            max_retries=retry_strategy
        )
        self.session.mount('http://', adapter)
        self.session.mount('https://', adapter)
        
        # 初始化代理管理器
        self.proxy_manager = GitHubProxyManager()
        
        # 統計資料
        self.stats = {
            'proxy_used': 0,
            'proxy_failed': 0,
            'direct_success': 0,
            'direct_failed': 0,
            'retry_success': 0,
        }
        
        # 快取
        self.cache = {}
        self.cache_ttl = 1800  # 30分鐘

    def check_url(self, url: str) -> Tuple[bool, Dict[str, Any]]:
        """
        智慧型檢查 URL
        策略：
        1. 檢測 URL 類型
        2. 如果是 GitHub URL，嘗試多種代理
        3. 如果代理失敗，嘗試直接連接
        4. 記錄詳細的診斷資訊
        """
        result = {
            'url': url,
            'status': False,
            'method': '',
            'status_code': None,
            'response_time': None,
            'error': None,
            'proxy_used': None,
            'attempts': []
        }
        
        # 檢查快取
        cache_key = hashlib.md5(url.encode()).hexdigest()
        if cache_key in self.cache:
            cached_time, cached_result = self.cache[cache_key]
            if time.time() - cached_time < self.cache_ttl:
                return cached_result[0], cached_result[1]
        
        # 判斷 URL 類型
        url_type = self._detect_url_type(url)
        
        if url_type == 'github':
            # GitHub URL 使用多策略檢查
            is_valid, result = self._check_github_url(url, result)
        else:
            # 一般 URL 使用標準檢查
            is_valid, result = self._check_normal_url(url, result)
        
        # 更新快取
        self.cache[cache_key] = (time.time(), (is_valid, result))
        
        return is_valid, result
    
    def _detect_url_type(self, url: str) -> str:
        """檢測 URL 類型"""
        url_lower = url.lower()
        if 'github.com' in url_lower or 'raw.githubusercontent.com' in url_lower:
            return 'github'
        elif 'gh-proxy' in url_lower or 'ghproxy' in url_lower:
            return 'github_proxy'
        else:
            return 'normal'
    
    def _check_github_url(self, url: str, result: Dict) -> Tuple[bool, Dict]:
        """檢查 GitHub URL（多策略）"""
        
        # 策略 1: 嘗試使用代理
        proxy_url = self.proxy_manager.get_proxy(url)
        if proxy_url != url:
            result['method'] = 'proxy'
            result['proxy_used'] = proxy_url
            
            is_valid, attempt_result = self._attempt_check(proxy_url)
            result['attempts'].append(attempt_result)
            
            if is_valid:
                self.stats['proxy_used'] += 1
                return True, result
        
        # 策略 2: 嘗試直接連接原始 URL
        # 提取原始 GitHub URL
        original_url = self._extract_original_github_url(url)
        if original_url:
            result['method'] = 'direct'
            is_valid, attempt_result = self._attempt_check(original_url)
            result['attempts'].append(attempt_result)
            
            if is_valid:
                self.stats['direct_success'] += 1
                return True, result
            else:
                self.stats['direct_failed'] += 1
        
        # 策略 3: 嘗試其他代理
        for proxy in GITHUB_PROXIES:
            if proxy == result.get('proxy_used'):
                continue
            
            test_url = self.proxy_manager._convert_to_proxy_url(url, proxy)
            if test_url:
                result['method'] = f'proxy_alt'
                result['proxy_used'] = test_url
                
                is_valid, attempt_result = self._attempt_check(test_url)
                result['attempts'].append(attempt_result)
                
                if is_valid:
                    self.stats['proxy_used'] += 1
                    return True, result
        
        # 所有策略都失敗
        result['status'] = False
        return False, result
    
    def _check_normal_url(self, url: str, result: Dict) -> Tuple[bool, Dict]:
        """檢查一般 URL"""
        is_valid, attempt_result = self._attempt_check(url)
        result['attempts'].append(attempt_result)
        result['method'] = 'direct'
        
        if is_valid:
            return True, result
        else:
            result['status'] = False
            return False, result
    
    def _attempt_check(self, url: str) -> Tuple[bool, Dict]:
        """執行單次檢查嘗試"""
        attempt_result = {
            'url': url,
            'success': False,
            'status_code': None,
            'response_time': None,
            'error': None,
            'content_preview': None
        }
        
        for attempt in range(RETRY):
            try:
                start_time = time.time()
                
                # 先嘗試 HEAD
                try:
                    response = self.session.head(
                        url,
                        timeout=TIMEOUT,
                        allow_redirects=True,
                        headers={
                            "Accept": "*/*",
                            "Accept-Encoding": "gzip, deflate",
                            "Connection": "keep-alive",
                            "Cache-Control": "no-cache"
                        }
                    )
                    
                    # HEAD 成功且狀態碼正常
                    if response.status_code < 400:
                        # 對於 GitHub raw 內容，HEAD 可能成功但內容可能不完整
                        # 所以我們還是要驗證內容
                        pass
                    else:
                        # HEAD 失敗，嘗試 GET
                        response = self.session.get(
                            url,
                            timeout=TIMEOUT,
                            allow_redirects=True,
                            stream=True,
                            headers={
                                "Accept": "*/*",
                                "Accept-Encoding": "gzip, deflate",
                                "Connection": "keep-alive"
                            }
                        )
                except:
                    # HEAD 出錯，直接使用 GET
                    response = self.session.get(
                        url,
                        timeout=TIMEOUT,
                        allow_redirects=True,
                        stream=True,
                        headers={
                            "Accept": "*/*",
                            "Accept-Encoding": "gzip, deflate",
                            "Connection": "keep-alive"
                        }
                    )
                
                response_time = time.time() - start_time
                attempt_result['response_time'] = round(response_time, 3)
                attempt_result['status_code'] = response.status_code
                
                # 檢查狀態碼
                if response.status_code >= 400:
                    attempt_result['error'] = f"HTTP {response.status_code}"
                    continue
                
                # 讀取部分內容進行驗證
                content = ""
                try:
                    # 對於 JSON 文件，讀取更多內容
                    content_limit = 8192 if url.endswith('.json') else 2048
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
                is_valid = self._validate_github_content(url, content)
                if is_valid:
                    attempt_result['success'] = True
                    return True, attempt_result
                else:
                    attempt_result['error'] = "內容驗證失敗"
                    continue
                
            except requests.exceptions.Timeout:
                attempt_result['error'] = "超時"
                if attempt < RETRY - 1:
                    time.sleep(1 * (attempt + 1))  # 指數退避
                continue
                
            except requests.exceptions.ConnectionError:
                attempt_result['error'] = "連線錯誤"
                if attempt < RETRY - 1:
                    time.sleep(1 * (attempt + 1))
                continue
                
            except requests.exceptions.SSLError:
                attempt_result['error'] = "SSL 錯誤"
                # 嘗試忽略 SSL 驗證
                try:
                    response = self.session.get(
                        url,
                        timeout=TIMEOUT,
                        verify=False,
                        allow_redirects=True
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
                continue
        
        return False, attempt_result
    
    def _validate_github_content(self, url: str, content: str) -> bool:
        """驗證 GitHub 內容"""
        if not content or len(content.strip()) < 10:
            return False
        
        content_lower = content.lower()
        
        # 檢查是否為錯誤頁面
        error_patterns = [
            '404: not found',
            '404 not found',
            'raw.githubusercontent.com' in content_lower and '404' in content_lower,
            'github' in content_lower and 'not found' in content_lower,
            'access denied',
            'forbidden',
            'rate limit',
            'too many requests',
            '<html',
            '<!doctype html'
        ]
        
        if any(pattern if isinstance(pattern, bool) else pattern in content_lower for pattern in error_patterns):
            return False
        
        # 如果是 JSON 文件，驗證 JSON 格式
        if url.endswith('.json'):
            try:
                data = json.loads(content)
                # 檢查是否為有效的 TVBox 配置
                if isinstance(data, dict):
                    # 檢查是否有 TVBox 的關鍵欄位
                    tvbox_fields = ['spider', 'sites', 'lives', 'parses']
                    has_tvbox_fields = any(field in data for field in tvbox_fields)
                    if has_tvbox_fields:
                        return True
                    # 如果沒有標準欄位，但內容不為空，也視為有效
                    return len(data) > 0
                return True
            except:
                return False
        
        # 如果是 M3U 文件
        if url.endswith(('.m3u', '.m3u8')):
            return '#EXTM3U' in content.upper()
        
        # 一般文本文件
        lines = [l.strip() for l in content.splitlines() if l.strip()]
        return len(lines) > 2
    
    def _extract_original_github_url(self, url: str) -> Optional[str]:
        """從代理 URL 中提取原始 GitHub URL"""
        # 如果是代理 URL，嘗試提取原始 URL
        for proxy in GITHUB_PROXIES:
            if url.startswith(proxy):
                # 移除代理前綴
                remaining = url[len(proxy):]
                # 檢查是否以 https:// 開頭
                if remaining.startswith('/https://'):
                    return remaining[1:]  # 移除開頭的 /
                elif remaining.startswith('https://'):
                    return remaining
        
        # 如果已經是原始 GitHub URL
        if 'github.com' in url or 'raw.githubusercontent.com' in url:
            return url
        
        return None
    
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
        self.seen = set()
        self.invalid_urls = []
        self.duplicate_urls = []
        self.url_details = {}  # 儲存詳細資訊
        self.proxy_stats = {}
        
        # URL 模式
        self.url_pattern = re.compile(r'https?://[^\s<>"\']+')

    def load_lines(self) -> List[str]:
        """載入輸入檔案"""
        p = Path(INPUT_FILE)
        if not p.exists():
            raise FileNotFoundError(f"找不到檔案: {INPUT_FILE}")
        return p.read_text(encoding='utf-8', errors='ignore').splitlines()

    def save_lines(self, lines: List[str]):
        """儲存輸出檔案（去除空白行和無網址行）"""
        output_path = Path(OUTPUT_FILE)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        
        # 過濾空白行和無網址行
        filtered = []
        for line in lines:
            stripped = line.strip()
            if stripped and self.url_pattern.search(stripped):
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
        """從行中提取所有 URL"""
        return self.url_pattern.findall(line)

    def is_duplicate(self, url: str) -> bool:
        """檢查 URL 是否重複"""
        if url in self.seen:
            self.duplicate += 1
            self.duplicate_urls.append(url)
            return True
        self.seen.add(url)
        return False

    def check_all(self):
        """執行完整檢查"""
        lines = self.load_lines()
        cleaned_lines = []
        tasks = []

        print(f"📂 載入 {len(lines)} 行資料")
        print(f"🔍 開始智慧型檢查...")
        print(f"📡 GitHub 代理服務: {len(GITHUB_PROXIES)} 個")
        print("-" * 60)

        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            # 提交所有任務
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

            # 收集結果
            for idx, (newline, futures) in enumerate(tasks):
                for future, url in futures:
                    try:
                        is_valid, details = future.result(timeout=TIMEOUT + 5)
                        
                        # 儲存詳細資訊
                        self.url_details[url] = details
                        
                        if is_valid:
                            self.valid += 1
                            # 統計代理使用情況
                            method = details.get('method', 'unknown')
                            self.proxy_stats[method] = self.proxy_stats.get(method, 0) + 1
                        else:
                            self.invalid += 1
                            self.invalid_urls.append(url)
                            newline = newline.replace(url, "")
                            
                            # 顯示失敗原因（調試用）
                            if self.invalid <= 10:  # 只顯示前 10 個
                                error = details.get('error', '未知錯誤')
                                attempts = len(details.get('attempts', []))
                                print(f"  ⚠️ {url[:60]}... - {error} (嘗試 {attempts} 次)")
                    except Exception as e:
                        self.invalid += 1
                        self.invalid_urls.append(url)
                        newline = newline.replace(url, "")
                        print(f"  ❌ {url[:60]}... - 檢查異常: {str(e)[:30]}")
                
                cleaned_lines.append(newline)
                
                # 顯示進度
                if (idx + 1) % 10 == 0:
                    progress = (idx + 1) / len(tasks) * 100
                    print(f"  進度: {progress:.1f}% ({idx + 1}/{len(tasks)})")

        # 儲存結果
        print(f"\n💾 儲存結果...")
        final_lines = self.save_lines(cleaned_lines)
        self.save_invalid()
        self.save_duplicate()
        
        # 生成報告
        self.generate_report(final_lines)

    def generate_report(self, final_lines: List[str]):
        """生成詳細報告"""
        lines = [
            "# 📊 TVBox URL 檢查報告（網路優化版）",
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
            "## 🌐 網路策略統計",
            "",
        ]
        
        # 添加網路策略統計
        for method, count in self.proxy_stats.items():
            method_name = {
                'proxy': '代理服務',
                'direct': '直接連接',
                'proxy_alt': '備用代理'
            }.get(method, method)
            lines.append(f"- **{method_name}**：{count} 個")
        
        # 添加代理管理器統計
        lines.extend([
            "",
            "## 📡 GitHub 代理狀態",
            "",
        ])
        
        healthy_proxies = self.checker.proxy_manager._get_healthy_proxies()
        for proxy in GITHUB_PROXIES:
            status = "✅ 健康" if proxy in healthy_proxies else "❌ 不可用"
            lines.append(f"- {proxy}：{status}")
        
        lines.extend([
            "",
            "## 📋 詳細統計",
            "",
            f"- **保留行數**：{len(final_lines)} 行",
            f"- **移除行數**：{self.total - len(final_lines)} 行",
            "",
            "## 📋 無效網址列表",
            "",
        ])
        
        if self.invalid_urls:
            for url in self.invalid_urls[:20]:
                # 嘗試獲取失敗原因
                details = self.url_details.get(url, {})
                error = details.get('error', '未知原因')
                lines.append(f"- `{url}` - {error}")
            if len(self.invalid_urls) > 20:
                lines.append(f"- ... 還有 {len(self.invalid_urls) - 20} 個")
        else:
            lines.append("✅ 沒有無效網址")
        
        lines.extend([
            "",
            "---",
            f"🕐 更新時間：{time.strftime('%Y-%m-%d %H:%M:%S')}",
            "",
            "✅ 報告由 TVBox URL Checker Pro v3 (網路優化版) 自動生成"
        ])
        
        Path(REPORT_FILE).write_text('\n'.join(lines), encoding='utf-8')
        print(f"📄 報告已生成: {REPORT_FILE}")

# ============================================================================
# 主程式
# ============================================================================

if __name__ == "__main__":
    print("=" * 70)
    print("🚀 TVBox URL Checker Pro v3 - 網路優化版")
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
        
    except KeyboardInterrupt:
        print("\n\n⚠️ 使用者中斷執行")
    except Exception as e:
        print(f"\n❌ 錯誤：{e}")
        import traceback
        traceback.print_exc()

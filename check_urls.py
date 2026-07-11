#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
TVBox URL Checker Pro v3
Part 1 - 強化版

功能
-------------------------
✓ 讀取 TXT
✓ 保留原格式
✓ 去除重覆網址
✓ 建立網址清單
✓ 多執行緒準備
✓ 去除空白行
✓ 去除無網址行
"""

from __future__ import annotations
import json
import re
import shutil
import socket
import time
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Tuple, Optional
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
MAX_WORKERS = cfg.get("workers", 50)
TIMEOUT = cfg.get("timeout", 8)
RETRY = cfg.get("retry", 3)
BACKUP_ENABLED = cfg.get("backup", True)
HISTORY_DIR = cfg.get("history", "data/history")

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

# ============================================================================
# URL 檢查器
# ============================================================================

class URLChecker:
    """URL 檢查器主類別"""
    
    def __init__(self):
        self.total = 0
        self.valid = 0
        self.invalid = 0
        self.duplicate = 0
        self.empty_lines = 0  # 空白行計數
        self.no_url_lines = 0  # 無網址行計數
        
        self.seen = set()
        self.invalid_urls = []
        self.duplicate_urls = []
        self.cleaned_lines = []  # 儲存清理後的行
        
        # 建立 Session
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT})
        
        # 設定連線池
        adapter = requests.adapters.HTTPAdapter(
            pool_connections=MAX_WORKERS,
            pool_maxsize=MAX_WORKERS
        )
        self.session.mount('http://', adapter)
        self.session.mount('https://', adapter)

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
        """
        儲存清理後的檔案
        會自動去除：
        1. 空白行（只有空格、tab 或完全空的行）
        2. 無網址行（沒有任何 URL 的行）
        """
        output_path = Path(OUTPUT_FILE)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        
        # 備份現有檔案
        if BACKUP_ENABLED and output_path.exists():
            history_path = Path(HISTORY_DIR)
            history_path.mkdir(parents=True, exist_ok=True)
            ts = time.strftime("%Y%m%d-%H%M%S")
            shutil.copy2(output_path, history_path / f"backup_{ts}.txt")
        
        # 🔥 核心功能：過濾空白行和無網址行
        filtered_lines = []
        url_pattern = re.compile(r'https?://[^\s<>"\']+')
        
        for line in lines:
            # 檢查是否為空白行（去除前後空白後判斷）
            stripped = line.strip()
            
            # 如果是空白行，記錄並跳過
            if not stripped:
                self.empty_lines += 1
                continue
            
            # 檢查是否有 URL
            has_url = bool(url_pattern.search(line))
            
            # 如果沒有 URL，記錄並跳過
            if not has_url:
                self.no_url_lines += 1
                continue
            
            # 保留有 URL 且非空白的行
            filtered_lines.append(line)
        
        # 寫入檔案
        output_path.write_text("\n".join(filtered_lines), encoding="utf-8")
        
        print(f"\n📊 過濾統計：")
        print(f"  - 移除空白行：{self.empty_lines} 行")
        print(f"  - 移除無網址行：{self.no_url_lines} 行")
        print(f"  - 保留有效行：{len(filtered_lines)} 行")
        print(f"  - 輸出檔案：{OUTPUT_FILE}")
        
        return filtered_lines

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

    # ========================================================================
    # URL 處理
    # ========================================================================

    def extract_urls(self, line: str) -> List[str]:
        """從一行文字中提取所有 URL"""
        return URL_PATTERN.findall(line)

    def is_duplicate(self, url: str) -> bool:
        """檢查 URL 是否重複"""
        if url in self.seen:
            self.duplicate += 1
            self.duplicate_urls.append(url)
            return True
        self.seen.add(url)
        return False

    # ========================================================================
    # 主要檢查流程
    # ========================================================================

    def check_all(self) -> None:
        """執行所有檢查流程"""
        lines = self.load()
        cleaned_lines = []
        tasks = []
        
        print(f"🔍 開始檢查網址有效性...")
        
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            # 處理每一行
            for line_num, line in enumerate(lines, 1):
                urls = self.extract_urls(line)
                
                # 沒有 URL 的行直接保留（但之後會被過濾掉）
                if not urls:
                    cleaned_lines.append(line)
                    continue
                
                # 處理有 URL 的行
                newline = line
                futures = []
                
                for url in urls:
                    self.total += 1
                    
                    # 檢查重複
                    if self.is_duplicate(url):
                        newline = newline.replace(url, "")
                        continue
                    
                    # 提交檢查任務
                    future = executor.submit(self.check_url, url)
                    futures.append((future, url))
                
                # 儲存任務
                tasks.append((newline, futures))
                
                # 顯示進度
                if line_num % 50 == 0:
                    print(f"  進度: {line_num}/{len(lines)} 行")
            
            # 收集結果
            for idx, (newline, futures) in enumerate(tasks):
                for future, url in futures:
                    try:
                        is_valid = future.result(timeout=TIMEOUT + 2)
                        if is_valid:
                            self.valid += 1
                        else:
                            self.invalid += 1
                            self.invalid_urls.append(url)
                            newline = newline.replace(url, "")
                    except Exception as e:
                        self.invalid += 1
                        self.invalid_urls.append(url)
                        newline = newline.replace(url, "")
                
                cleaned_lines.append(newline)
            
            # 顯示最終進度
            print(f"  ✅ 完成 {len(tasks)} 行檢查")
        
        # 🔥 儲存結果（會自動過濾空白行和無網址行）
        self.save(cleaned_lines)
        self.save_invalid()
        self.save_duplicate()
        self.generate_report()

    # ========================================================================
    # URL 有效性檢查
    # ========================================================================

    def check_url(self, url: str) -> bool:
        """
        檢查網址是否有效
        流程：
        1. 先嚐試 HEAD 請求
        2. HEAD 失敗則改用 GET
        3. 驗證 HTTP 狀態碼
        4. 驗證內容
        """
        for attempt in range(RETRY):
            try:
                # 先嘗試 HEAD
                try:
                    r = self.session.head(
                        url,
                        timeout=TIMEOUT,
                        allow_redirects=True
                    )
                    if r.status_code < 400:
                        # HEAD 成功，還需要檢查內容嗎？
                        # 對於某些 URL，HEAD 可能成功但 GET 返回錯誤
                        # 所以如果 HEAD 成功，我們還是要用 GET 驗證內容
                        pass
                except Exception:
                    pass
                
                # 使用 GET 獲取內容
                r = self.session.get(
                    url,
                    timeout=TIMEOUT,
                    allow_redirects=True,
                    stream=True
                )
                
                # 檢查狀態碼
                if r.status_code >= 400:
                    continue
                
                # 讀取部分內容
                content = ""
                try:
                    for chunk in r.iter_content(chunk_size=1024):
                        if chunk:
                            content += chunk.decode('utf-8', errors='ignore')
                            if len(content) >= 2000:
                                break
                except Exception:
                    pass
                
                # 驗證內容
                return self.validate_content(url, content)
                
            except (requests.exceptions.Timeout,
                    requests.exceptions.ConnectionError,
                    requests.exceptions.SSLError,
                    requests.exceptions.RequestException,
                    socket.gaierror,
                    socket.timeout):
                if attempt < RETRY - 1:
                    time.sleep(0.5)
                continue
        
        return False

    # ========================================================================
    # 內容驗證
    # ========================================================================

    def validate_content(self, url: str, content: str) -> bool:
        """根據 URL 副檔名驗證內容"""
        if not content:
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
        return bool(content.strip())

    def _validate_json(self, content: str) -> bool:
        """JSON 內容驗證"""
        content = content.strip()
        if not content:
            return False
        
        if '<html' in content.lower():
            return False
        
        try:
            json.loads(content)
            return True
        except json.JSONDecodeError:
            return False

    def _validate_xml(self, content: str) -> bool:
        """XML 內容驗證"""
        content_lower = content.lower()
        return ('<?xml' in content_lower or 
                '<tv' in content_lower or 
                '<rss' in content_lower or 
                '<channel' in content_lower)

    def _validate_m3u(self, content: str) -> bool:
        """M3U 內容驗證"""
        return '#EXTM3U' in content.upper()

    def _validate_txt(self, content: str) -> bool:
        """TXT 內容驗證"""
        content_lower = content.lower()
        
        for keyword in ['404', 'forbidden', 'access denied', 'nginx', '<html', 'error']:
            if keyword in content_lower:
                return False
        
        lines = [line.strip() for line in content.splitlines() if line.strip()]
        return len(lines) > 0

    # ========================================================================
    # 報告生成
    # ========================================================================

    def generate_report(self) -> None:
        """生成檢查報告"""
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
            "",
            "## 🧹 清理統計",
            "",
            f"- **移除空白行**：{self.empty_lines} 行",
            f"- **移除無網址行**：{self.no_url_lines} 行",
            "",
            "## 📋 無效網址列表",
            "",
        ]
        
        if self.invalid_urls:
            for url in self.invalid_urls[:30]:
                lines.append(f"- `{url}`")
            if len(self.invalid_urls) > 30:
                lines.append(f"- ... 還有 {len(self.invalid_urls) - 30} 個")
        else:
            lines.append("✅ 沒有無效網址")
        
        lines.extend([
            "",
            "## 📋 重複網址列表",
            ""
        ])
        
        if self.duplicate_urls:
            for url in self.duplicate_urls[:30]:
                lines.append(f"- `{url}`")
            if len(self.duplicate_urls) > 30:
                lines.append(f"- ... 還有 {len(self.duplicate_urls) - 30} 個")
        else:
            lines.append("✅ 沒有重複網址")
        
        lines.extend([
            "",
            "---",
            f"🕐 更新時間：{time.strftime('%Y-%m-%d %H:%M:%S')}",
            "",
            "✅ 報告由 TVBox URL Checker Pro v3 自動生成"
        ])
        
        Path(REPORT_FILE).write_text("\n".join(lines), encoding="utf-8")
        print(f"📄 報告已生成：{REPORT_FILE}")

# ============================================================================
# 主程式
# ============================================================================

if __name__ == "__main__":
    print("=" * 70)
    print("🚀 TVBox URL Checker Pro v3")
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
        print(f"🧹 移除空白行 : {checker.empty_lines}")
        print(f"🧹 移除無網址行 : {checker.no_url_lines}")
        print(f"⏱️ 耗時   : {time.time() - start_time:.2f} 秒")
        print("=" * 70)
        print(f"\n📁 輸出檔案：")
        print(f"  - 有效清單: {OUTPUT_FILE}")
        print(f"  - 無效清單: {INVALID_FILE}")
        print(f"  - 重複清單: {DUPLICATE_FILE}")
        print(f"  - 檢查報告: {REPORT_FILE}")
        
    except KeyboardInterrupt:
        print("\n\n⚠️ 使用者中斷執行")
    except Exception as e:
        print(f"\n❌ 錯誤：{e}")
        import traceback
        traceback.print_exc()

"""
Git 仓库管理服务
支持扫描指定目录下的 git 仓库，执行一键更新操作
"""

import os
import subprocess
import asyncio
import json
import time
from typing import List, Dict, Optional, Any
from datetime import datetime
from pathlib import Path
import logging

logger = logging.getLogger(__name__)

class GitService:
    def __init__(self):
        self.repositories_cache = {}
        self.last_scan_time = None
        self.scan_cache_duration = 300  # 5分钟缓存
        
    async def scan_git_repositories(self, base_path: str, page: int = 1, limit: int = 20) -> Dict[str, Any]:
        """
        扫描指定路径下的所有 git 仓库
        
        Args:
            base_path: 要扫描的基础路径
            page: 页码（从1开始）
            limit: 每页数量
            
        Returns:
            包含仓库信息和分页信息的字典
        """
        try:
            # 检查缓存
            cache_key = f"{base_path}_{int(time.time() // self.scan_cache_duration)}"
            if cache_key in self.repositories_cache:
                repositories = self.repositories_cache[cache_key]
                # 计算分页
                total = len(repositories)
                start_index = (page - 1) * limit
                end_index = start_index + limit
                paginated_repositories = repositories[start_index:end_index]
                
                return {
                    "repositories": paginated_repositories,
                    "total": total,
                    "page": page,
                    "limit": limit,
                    "total_pages": (total + limit - 1) // limit,
                    "has_next": end_index < total,
                    "has_prev": page > 1
                }
            
            repositories = []
            base_path_obj = Path(base_path)
            
            if not base_path_obj.exists():
                logger.error(f"路径不存在: {base_path}")
                return []
            
            # 递归扫描目录
            for root, dirs, files in os.walk(base_path):
                # 检查是否有 .git 目录
                git_dir = Path(root) / ".git"
                if git_dir.exists() and git_dir.is_dir():
                    try:
                        repo_info = await self._get_repository_info(root)
                        if repo_info:
                            repositories.append(repo_info)
                    except Exception as e:
                        logger.error(f"获取仓库信息失败 {root}: {e}")
                        continue
            
            # 计算分页
            total = len(repositories)
            start_index = (page - 1) * limit
            end_index = start_index + limit
            paginated_repositories = repositories[start_index:end_index]
            
            # 缓存完整结果
            self.repositories_cache[cache_key] = repositories
            self.last_scan_time = datetime.now()
            
            return {
                "repositories": paginated_repositories,
                "total": total,
                "page": page,
                "limit": limit,
                "total_pages": (total + limit - 1) // limit,
                "has_next": end_index < total,
                "has_prev": page > 1
            }
            
        except Exception as e:
            logger.error(f"扫描 git 仓库失败: {e}")
            return {
                "repositories": [],
                "total": 0,
                "page": page,
                "limit": limit,
                "total_pages": 0,
                "has_next": False,
                "has_prev": False
            }
    
    async def _get_repository_info(self, repo_path: str) -> Optional[Dict[str, Any]]:
        """
        获取单个仓库的详细信息
        
        Args:
            repo_path: 仓库路径
            
        Returns:
            仓库信息字典
        """
        try:
            # 获取仓库名称
            repo_name = os.path.basename(repo_path)
            
            # 获取远程仓库 URL
            remote_url = await self._get_remote_url(repo_path)
            
            # 获取当前分支
            current_branch = await self._get_current_branch(repo_path)
            
            # 获取仓库状态
            status = await self._get_repository_status(repo_path)
            
            # 获取最后更新时间
            last_update = await self._get_last_update_time(repo_path)
            
            return {
                "path": repo_path,
                "name": repo_name,
                "remote_url": remote_url,
                "current_branch": current_branch,
                "status": status,
                "last_update": last_update
            }
            
        except Exception as e:
            logger.error(f"获取仓库信息失败 {repo_path}: {e}")
            return None
    
    async def _get_remote_url(self, repo_path: str) -> Optional[str]:
        """获取远程仓库 URL"""
        try:
            result = await self._run_git_command(repo_path, ["config", "--get", "remote.origin.url"])
            return result.strip() if result else None
        except Exception:
            return None
    
    async def _get_current_branch(self, repo_path: str) -> Optional[str]:
        """获取当前分支"""
        try:
            result = await self._run_git_command(repo_path, ["rev-parse", "--abbrev-ref", "HEAD"])
            return result.strip() if result else None
        except Exception:
            return None
    
    async def _get_repository_status(self, repo_path: str) -> str:
        """获取仓库状态"""
        try:
            # 检查是否有未提交的更改
            status_result = await self._run_git_command(repo_path, ["status", "--porcelain"])
            if status_result.strip():
                return "modified"
            
            # 检查是否需要拉取更新
            fetch_result = await self._run_git_command(repo_path, ["fetch", "--dry-run"])
            if "->" in fetch_result:
                return "behind"
            
            return "up_to_date"
            
        except Exception:
            return "unknown"
    
    async def _get_last_update_time(self, repo_path: str) -> Optional[str]:
        """获取最后更新时间"""
        try:
            result = await self._run_git_command(repo_path, ["log", "-1", "--format=%cd", "--date=iso"])
            return result.strip() if result else None
        except Exception:
            return None
    
    async def _run_git_command(self, repo_path: str, command: List[str]) -> str:
        """
        在指定仓库中执行 git 命令
        
        Args:
            repo_path: 仓库路径
            command: git 命令列表
            
        Returns:
            命令输出
        """
        try:
            process = await asyncio.create_subprocess_exec(
                "git",
                *command,
                cwd=repo_path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            
            stdout, stderr = await process.communicate()
            
            if process.returncode != 0:
                logger.warning(f"Git 命令执行失败: {' '.join(command)}, 错误: {stderr.decode()}")
                return ""
            
            return stdout.decode().strip()
            
        except Exception as e:
            logger.error(f"执行 git 命令失败: {e}")
            return ""
    
    async def update_repositories(self, repo_paths: List[str], force: bool = False) -> Dict[str, Any]:
        """
        更新指定的 git 仓库
        
        Args:
            repo_paths: 要更新的仓库路径列表
            force: 是否强制更新（重置本地更改）
            
        Returns:
            更新结果
        """
        results = {
            "success": [],
            "failed": [],
            "skipped": []
        }
        
        for repo_path in repo_paths:
            try:
                if not os.path.exists(repo_path):
                    results["failed"].append({
                        "path": repo_path,
                        "error": "仓库路径不存在"
                    })
                    continue
                
                # 检查是否为 git 仓库
                git_dir = Path(repo_path) / ".git"
                if not git_dir.exists():
                    results["failed"].append({
                        "path": repo_path,
                        "error": "不是有效的 git 仓库"
                    })
                    continue
                
                # 执行更新操作
                update_result = await self._update_single_repository(repo_path, force)
                
                if update_result["success"]:
                    results["success"].append(update_result)
                else:
                    results["failed"].append(update_result)
                    
            except Exception as e:
                results["failed"].append({
                    "path": repo_path,
                    "error": str(e)
                })
        
        return results
    
    async def _update_single_repository(self, repo_path: str, force: bool) -> Dict[str, Any]:
        """
        更新单个仓库
        
        Args:
            repo_path: 仓库路径
            force: 是否强制更新
            
        Returns:
            更新结果
        """
        try:
            # 获取更新前的状态
            before_branch = await self._get_current_branch(repo_path)
            before_commit = await self._run_git_command(repo_path, ["rev-parse", "HEAD"])
            
            # 如果有本地更改且强制更新，先重置
            if force:
                await self._run_git_command(repo_path, ["reset", "--hard", "HEAD"])
                await self._run_git_command(repo_path, ["clean", "-fd"])
            
            # 拉取最新代码
            pull_result = await self._run_git_command(repo_path, ["pull", "origin", before_branch or "main"])
            
            # 获取更新后的状态
            after_branch = await self._get_current_branch(repo_path)
            after_commit = await self._run_git_command(repo_path, ["rev-parse", "HEAD"])
            
            return {
                "path": repo_path,
                "success": True,
                "before_commit": before_commit,
                "after_commit": after_commit,
                "before_branch": before_branch,
                "after_branch": after_branch,
                "pull_result": pull_result,
                "updated": before_commit != after_commit
            }
            
        except Exception as e:
            return {
                "path": repo_path,
                "success": False,
                "error": str(e)
            }
    
    async def get_repository_details(self, repo_path: str) -> Optional[Dict[str, Any]]:
        """
        获取仓库详细信息
        
        Args:
            repo_path: 仓库路径
            
        Returns:
            仓库详细信息
        """
        try:
            if not os.path.exists(repo_path):
                return None
            
            # 获取基本信息
            basic_info = await self._get_repository_info(repo_path)
            if not basic_info:
                return None
            
            # 获取更多详细信息
            details = {
                **basic_info,
                "commit_count": await self._run_git_command(repo_path, ["rev-list", "--count", "HEAD"]),
                "last_commit_message": await self._run_git_command(repo_path, ["log", "-1", "--pretty=format:%s"]),
                "last_commit_author": await self._run_git_command(repo_path, ["log", "-1", "--pretty=format:%an"]),
                "last_commit_date": await self._run_git_command(repo_path, ["log", "-1", "--pretty=format:%cd", "--date=iso"]),
                "remote_branches": await self._get_remote_branches(repo_path),
                "local_branches": await self._get_local_branches(repo_path),
                "stash_count": await self._run_git_command(repo_path, ["stash", "list"]).count("\n") + 1 if await self._run_git_command(repo_path, ["stash", "list"]) else 0
            }
            
            return details
            
        except Exception as e:
            logger.error(f"获取仓库详细信息失败 {repo_path}: {e}")
            return None
    
    async def _get_remote_branches(self, repo_path: str) -> List[str]:
        """获取远程分支列表"""
        try:
            result = await self._run_git_command(repo_path, ["branch", "-r"])
            branches = [branch.strip() for branch in result.split("\n") if branch.strip()]
            return branches
        except Exception:
            return []
    
    async def _get_local_branches(self, repo_path: str) -> List[str]:
        """获取本地分支列表"""
        try:
            result = await self._run_git_command(repo_path, ["branch"])
            branches = [branch.strip().replace("* ", "") for branch in result.split("\n") if branch.strip()]
            return branches
        except Exception:
            return []
    
    def clear_cache(self):
        """清除缓存"""
        self.repositories_cache.clear()
        self.last_scan_time = None 
"""
Redis 任务存储实现
"""

import json
import asyncio
from typing import Dict, List, Optional, Any
import redis.asyncio as redis
from .base import TaskStore


class RedisTaskStore(TaskStore):
    """Redis 任务存储"""
    
    def __init__(self, redis_client: redis.Redis, key_prefix: str = "mini_pm2:tasks"):
        self.redis = redis_client
        self.key_prefix = key_prefix
        self._lock = asyncio.Lock()
    
    def _get_task_key(self, task_id: str) -> str:
        """获取任务键名"""
        return f"{self.key_prefix}:{task_id}"
    
    def _get_tasks_key(self) -> str:
        """获取任务列表键名"""
        return f"{self.key_prefix}:list"
    
    async def get_all_tasks(self) -> List[Dict[str, Any]]:
        """获取所有任务"""
        async with self._lock:
            try:
                # 获取任务ID列表
                task_ids = await self.redis.smembers(self._get_tasks_key())
                if not task_ids:
                    return []
                
                # 批量获取任务数据
                tasks = []
                for task_id in task_ids:
                    task_id_str = task_id.decode('utf-8')
                    task_data = await self.redis.get(self._get_task_key(task_id_str))
                    if task_data:
                        tasks.append(json.loads(task_data))
                
                return tasks
            except Exception as e:
                print(f"从Redis获取任务失败: {e}")
                return []
    
    async def get_task(self, task_id: str) -> Optional[Dict[str, Any]]:
        """获取单个任务"""
        try:
            task_data = await self.redis.get(self._get_task_key(task_id))
            if task_data:
                return json.loads(task_data)
            return None
        except Exception as e:
            print(f"从Redis获取任务失败: {e}")
            return None
    
    async def add_task(self, task: Dict[str, Any]) -> bool:
        """添加任务"""
        async with self._lock:
            try:
                task_id = task['id']
                
                # 检查任务是否已存在
                if await self.task_exists(task_id):
                    raise ValueError(f"任务ID '{task_id}' 已存在")
                
                # 保存任务数据
                await self.redis.set(
                    self._get_task_key(task_id),
                    json.dumps(task, ensure_ascii=False),
                    ex=86400  # 24小时过期
                )
                
                # 添加到任务列表
                await self.redis.sadd(self._get_tasks_key(), task_id)
                
                return True
            except Exception as e:
                print(f"添加任务到Redis失败: {e}")
                return False
    
    async def update_task(self, task_id: str, updates: Dict[str, Any]) -> bool:
        """更新任务"""
        async with self._lock:
            try:
                # 获取现有任务
                task = await self.get_task(task_id)
                if not task:
                    raise ValueError(f"任务 '{task_id}' 不存在")
                
                # 更新任务数据
                task.update(updates)
                
                # 保存更新后的任务
                await self.redis.set(
                    self._get_task_key(task_id),
                    json.dumps(task, ensure_ascii=False),
                    ex=86400
                )
                
                return True
            except Exception as e:
                print(f"更新Redis任务失败: {e}")
                return False
    
    async def delete_task(self, task_id: str) -> bool:
        """删除任务"""
        async with self._lock:
            try:
                # 检查任务是否存在
                if not await self.task_exists(task_id):
                    raise ValueError(f"任务 '{task_id}' 不存在")
                
                # 删除任务数据
                await self.redis.delete(self._get_task_key(task_id))
                
                # 从任务列表中移除
                await self.redis.srem(self._get_tasks_key(), task_id)
                
                return True
            except Exception as e:
                print(f"删除Redis任务失败: {e}")
                return False
    
    async def task_exists(self, task_id: str) -> bool:
        """检查任务是否存在"""
        try:
            return await self.redis.exists(self._get_task_key(task_id)) > 0
        except Exception as e:
            print(f"检查Redis任务存在性失败: {e}")
            return False
    
    async def save_tasks(self, tasks: List[Dict[str, Any]]) -> bool:
        """保存所有任务"""
        async with self._lock:
            try:
                # 清空现有任务
                task_ids = await self.redis.smembers(self._get_tasks_key())
                for task_id in task_ids:
                    await self.redis.delete(self._get_task_key(task_id.decode('utf-8')))
                await self.redis.delete(self._get_tasks_key())
                
                # 保存新任务
                for task in tasks:
                    task_id = task['id']
                    await self.redis.set(
                        self._get_task_key(task_id),
                        json.dumps(task, ensure_ascii=False),
                        ex=86400
                    )
                    await self.redis.sadd(self._get_tasks_key(), task_id)
                
                return True
            except Exception as e:
                print(f"保存任务到Redis失败: {e}")
                return False
    
    async def clear_all(self) -> bool:
        """清空所有任务"""
        try:
            task_ids = await self.redis.smembers(self._get_tasks_key())
            for task_id in task_ids:
                await self.redis.delete(self._get_task_key(task_id.decode('utf-8')))
            await self.redis.delete(self._get_tasks_key())
            return True
        except Exception as e:
            print(f"清空Redis任务失败: {e}")
            return False
    
    def _get_history_key(self, task_id: str) -> str:
        """获取任务历史键名"""
        return f"{self.key_prefix}:history:{task_id}"
    
    async def get_task_history(self, task_id: str, limit: int = 50) -> List[Dict[str, Any]]:
        """获取任务执行历史"""
        try:
            history_key = self._get_history_key(task_id)
            history_data = await self.redis.lrange(history_key, 0, limit - 1)
            
            history = []
            for entry in history_data:
                try:
                    history.append(json.loads(entry.decode('utf-8')))
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
            
            return history
        except Exception as e:
            print(f"从Redis获取任务历史失败: {e}")
            return []
    
    async def add_task_history(self, task_id: str, history_entry: Dict[str, Any]) -> bool:
        """添加任务执行历史"""
        try:
            import datetime
            history_entry['timestamp'] = datetime.datetime.now().isoformat()
            
            history_key = self._get_history_key(task_id)
            history_json = json.dumps(history_entry, ensure_ascii=False)
            
            # 添加到列表头部
            await self.redis.lpush(history_key, history_json)
            
            # 限制历史记录数量（最多保留1000条）
            await self.redis.ltrim(history_key, 0, 999)
            
            # 设置过期时间（30天）
            await self.redis.expire(history_key, 30 * 24 * 3600)
            
            return True
        except Exception as e:
            print(f"添加任务历史到Redis失败: {e}")
            return False
    
    async def clear_task_history(self, task_id: str) -> bool:
        """清空任务执行历史"""
        try:
            history_key = self._get_history_key(task_id)
            await self.redis.delete(history_key)
            return True
        except Exception as e:
            print(f"清空Redis任务历史失败: {e}")
            return False
    
    async def cleanup_old_history(self, days: int = 30) -> bool:
        """清理旧的历史记录"""
        try:
            # Redis会自动通过过期时间清理旧数据
            # 这里只是记录清理操作
            print(f"Redis历史记录将在 {days} 天后自动过期")
            return True
        except Exception as e:
            print(f"清理Redis历史记录失败: {e}")
            return False
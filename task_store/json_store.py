"""
JSON 文件任务存储实现
"""

import json
import os
import asyncio
from typing import Dict, List, Optional, Any
from .base import TaskStore


class JSONTaskStore(TaskStore):
    """JSON 文件任务存储"""
    
    def __init__(self, file_path: str, jobs_directory: str = None, task_file_prefix: str = "task"):
        self.file_path = file_path
        self.jobs_directory = jobs_directory
        self.task_file_prefix = task_file_prefix
        self._lock = asyncio.Lock()
        
        # 如果指定了jobs目录，确保目录存在
        if self.jobs_directory:
            os.makedirs(self.jobs_directory, exist_ok=True)
    
    async def _load_tasks(self) -> List[Dict[str, Any]]:
        """从文件加载任务"""
        if not os.path.exists(self.file_path):
            return []
        
        try:
            with open(self.file_path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError) as e:
            print(f"加载任务文件失败: {e}")
            return []
    
    async def _save_tasks(self, tasks: List[Dict[str, Any]]) -> bool:
        """保存任务到文件"""
        try:
            # 确保目录存在
            os.makedirs(os.path.dirname(self.file_path), exist_ok=True)
            
            with open(self.file_path, 'w', encoding='utf-8') as f:
                json.dump(tasks, f, indent=4, ensure_ascii=False)
            return True
        except IOError as e:
            print(f"保存任务文件失败: {e}")
            return False
    
    async def get_all_tasks(self) -> List[Dict[str, Any]]:
        """获取所有任务"""
        async with self._lock:
            if self.jobs_directory:
                # 从jobs目录读取所有任务文件
                tasks = []
                if os.path.exists(self.jobs_directory):
                    for filename in os.listdir(self.jobs_directory):
                        if filename.startswith(f"{self.task_file_prefix}_") and filename.endswith('.json'):
                            task_id = filename[len(f"{self.task_file_prefix}_"):-5]  # 移除前缀和后缀
                            task_file = os.path.join(self.jobs_directory, filename)
                            try:
                                with open(task_file, 'r', encoding='utf-8') as f:
                                    task = json.load(f)
                                    tasks.append(task)
                            except (json.JSONDecodeError, IOError) as e:
                                print(f"读取任务文件 {filename} 失败: {e}")
                                continue
                return tasks
            else:
                # 传统方式：从统一文件读取
                return await self._load_tasks()
    
    async def get_task(self, task_id: str) -> Optional[Dict[str, Any]]:
        """获取单个任务"""
        if self.jobs_directory:
            # 按任务ID分别存储文件
            task_file = self._get_task_file_path(task_id)
            if not os.path.exists(task_file):
                return None
            
            try:
                with open(task_file, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except (json.JSONDecodeError, IOError) as e:
                print(f"加载任务文件失败: {e}")
                return None
        else:
            # 传统方式：从统一文件中查找
            tasks = await self.get_all_tasks()
            for task in tasks:
                if task.get('id') == task_id:
                    return task
            return None
    
    async def add_task(self, task: Dict[str, Any]) -> bool:
        """添加任务"""
        async with self._lock:
            if self.jobs_directory:
                # 按任务ID分别存储文件
                task_file = self._get_task_file_path(task['id'])
                
                # 检查任务是否已存在
                if os.path.exists(task_file):
                    raise ValueError(f"任务ID '{task['id']}' 已存在")
                
                # 保存单个任务文件
                try:
                    with open(task_file, 'w', encoding='utf-8') as f:
                        json.dump(task, f, indent=4, ensure_ascii=False)
                    return True
                except IOError as e:
                    print(f"保存任务文件失败: {e}")
                    return False
            else:
                # 传统方式：添加到统一文件
                tasks = await self._load_tasks()
                
                # 检查任务ID是否已存在
                if any(t.get('id') == task['id'] for t in tasks):
                    raise ValueError(f"任务ID '{task['id']}' 已存在")
                
                tasks.append(task)
                return await self._save_tasks(tasks)
    
    async def update_task(self, task_id: str, updates: Dict[str, Any]) -> bool:
        """更新任务"""
        async with self._lock:
            if self.jobs_directory:
                # 按任务ID分别存储文件
                task_file = self._get_task_file_path(task_id)
                
                if not os.path.exists(task_file):
                    raise ValueError(f"任务 '{task_id}' 不存在")
                
                # 读取现有任务
                try:
                    with open(task_file, 'r', encoding='utf-8') as f:
                        task = json.load(f)
                except (json.JSONDecodeError, IOError) as e:
                    print(f"读取任务文件失败: {e}")
                    raise ValueError(f"任务 '{task_id}' 文件损坏")
                
                # 更新任务
                task.update(updates)
                
                # 保存更新后的任务
                try:
                    with open(task_file, 'w', encoding='utf-8') as f:
                        json.dump(task, f, indent=4, ensure_ascii=False)
                    return True
                except IOError as e:
                    print(f"保存任务文件失败: {e}")
                    return False
            else:
                # 传统方式：更新统一文件
                tasks = await self._load_tasks()
                
                for i, task in enumerate(tasks):
                    if task.get('id') == task_id:
                        tasks[i].update(updates)
                        return await self._save_tasks(tasks)
                
                raise ValueError(f"任务 '{task_id}' 不存在")
    
    async def delete_task(self, task_id: str) -> bool:
        """删除任务"""
        async with self._lock:
            if self.jobs_directory:
                # 按任务ID分别存储文件
                task_file = self._get_task_file_path(task_id)
                
                if not os.path.exists(task_file):
                    raise ValueError(f"任务 '{task_id}' 不存在")
                
                # 删除任务文件
                try:
                    os.remove(task_file)
                    return True
                except OSError as e:
                    print(f"删除任务文件失败: {e}")
                    return False
            else:
                # 传统方式：从统一文件中删除
                tasks = await self._load_tasks()
                original_count = len(tasks)
                
                tasks = [t for t in tasks if t.get('id') != task_id]
                
                if len(tasks) == original_count:
                    raise ValueError(f"任务 '{task_id}' 不存在")
                
                return await self._save_tasks(tasks)
    
    async def task_exists(self, task_id: str) -> bool:
        """检查任务是否存在"""
        task = await self.get_task(task_id)
        return task is not None
    
    async def save_tasks(self, tasks: List[Dict[str, Any]]) -> bool:
        """保存所有任务"""
        async with self._lock:
            return await self._save_tasks(tasks)
    
    def _get_task_file_path(self, task_id: str) -> str:
        """获取单个任务文件路径"""
        if self.jobs_directory:
            return os.path.join(self.jobs_directory, f'{self.task_file_prefix}_{task_id}.json')
        else:
            return self.file_path
    
    def _get_history_file_path(self, task_id: str) -> str:
        """获取任务历史文件路径"""
        if self.jobs_directory:
            history_dir = os.path.join(self.jobs_directory, 'history')
        else:
            history_dir = os.path.join(os.path.dirname(self.file_path), 'history')
        os.makedirs(history_dir, exist_ok=True)
        return os.path.join(history_dir, f'{task_id}_history.json')
    
    async def _load_task_history(self, task_id: str) -> List[Dict[str, Any]]:
        """加载任务历史"""
        history_file = self._get_history_file_path(task_id)
        if not os.path.exists(history_file):
            return []
        
        try:
            with open(history_file, 'r', encoding='utf-8') as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError) as e:
            print(f"加载任务历史失败: {e}")
            return []
    
    async def _save_task_history(self, task_id: str, history: List[Dict[str, Any]]) -> bool:
        """保存任务历史"""
        history_file = self._get_history_file_path(task_id)
        try:
            with open(history_file, 'w', encoding='utf-8') as f:
                json.dump(history, f, indent=4, ensure_ascii=False)
            return True
        except IOError as e:
            print(f"保存任务历史失败: {e}")
            return False
    
    async def get_task_history(self, task_id: str, limit: int = 50) -> List[Dict[str, Any]]:
        """获取任务执行历史"""
        async with self._lock:
            history = await self._load_task_history(task_id)
            return history[-limit:] if limit > 0 else history
    
    async def add_task_history(self, task_id: str, history_entry: Dict[str, Any]) -> bool:
        """添加任务执行历史"""
        async with self._lock:
            history = await self._load_task_history(task_id)
            
            # 添加时间戳
            import datetime
            history_entry['timestamp'] = datetime.datetime.now().isoformat()
            
            history.append(history_entry)
            
            # 限制历史记录数量（最多保留1000条）
            if len(history) > 1000:
                history = history[-1000:]
            
            return await self._save_task_history(task_id, history)
    
    async def clear_task_history(self, task_id: str) -> bool:
        """清空任务执行历史"""
        async with self._lock:
            return await self._save_task_history(task_id, [])
    
    async def cleanup_old_history(self, days: int = 30) -> bool:
        """清理旧的历史记录"""
        import datetime
        cutoff_date = datetime.datetime.now() - datetime.timedelta(days=days)
        
        async with self._lock:
            history_dir = os.path.join(os.path.dirname(self.file_path), 'history')
            if not os.path.exists(history_dir):
                return True
            
            cleaned_count = 0
            for filename in os.listdir(history_dir):
                if filename.endswith('_history.json'):
                    history_file = os.path.join(history_dir, filename)
                    try:
                        with open(history_file, 'r', encoding='utf-8') as f:
                            history = json.load(f)
                        
                        # 过滤掉旧记录
                        filtered_history = []
                        for entry in history:
                            entry_time = datetime.datetime.fromisoformat(entry.get('timestamp', ''))
                            if entry_time > cutoff_date:
                                filtered_history.append(entry)
                        
                        if len(filtered_history) != len(history):
                            with open(history_file, 'w', encoding='utf-8') as f:
                                json.dump(filtered_history, f, indent=4, ensure_ascii=False)
                            cleaned_count += 1
                    except Exception as e:
                        print(f"清理历史记录失败 {filename}: {e}")
            
            print(f"清理了 {cleaned_count} 个历史文件")
            return True
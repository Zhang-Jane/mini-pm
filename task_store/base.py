"""
任务存储基础接口
定义任务存储的标准接口
"""

from abc import ABC, abstractmethod
from typing import Dict, List, Optional, Any


class TaskStore(ABC):
    """任务存储基础接口"""
    
    @abstractmethod
    async def get_all_tasks(self) -> List[Dict[str, Any]]:
        """获取所有任务"""
        pass
    
    @abstractmethod
    async def get_task(self, task_id: str) -> Optional[Dict[str, Any]]:
        """获取单个任务"""
        pass
    
    @abstractmethod
    async def add_task(self, task: Dict[str, Any]) -> bool:
        """添加任务"""
        pass
    
    @abstractmethod
    async def update_task(self, task_id: str, updates: Dict[str, Any]) -> bool:
        """更新任务"""
        pass
    
    @abstractmethod
    async def delete_task(self, task_id: str) -> bool:
        """删除任务"""
        pass
    
    @abstractmethod
    async def task_exists(self, task_id: str) -> bool:
        """检查任务是否存在"""
        pass
    
    @abstractmethod
    async def save_tasks(self, tasks: List[Dict[str, Any]]) -> bool:
        """保存所有任务"""
        pass
    
    @abstractmethod
    async def get_task_history(self, task_id: str, limit: int = 50) -> List[Dict[str, Any]]:
        """获取任务执行历史"""
        pass
    
    @abstractmethod
    async def add_task_history(self, task_id: str, history_entry: Dict[str, Any]) -> bool:
        """添加任务执行历史"""
        pass
    
    @abstractmethod
    async def clear_task_history(self, task_id: str) -> bool:
        """清空任务执行历史"""
        pass
    
    @abstractmethod
    async def cleanup_old_history(self, days: int = 30) -> bool:
        """清理旧的历史记录"""
        pass
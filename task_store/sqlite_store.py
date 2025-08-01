"""
SQLite 任务存储实现
"""

import sqlite3
import asyncio
import json
import os
from typing import Dict, List, Optional, Any
from datetime import datetime
from .base import TaskStore


class SQLiteTaskStore(TaskStore):
    """SQLite 任务存储"""
    
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._lock = asyncio.Lock()
        self._init_db()
    
    def _init_db(self):
        """初始化数据库表"""
        try:
            # 确保目录存在
            if os.path.dirname(self.db_path):
                os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
            
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                
                # 创建任务表
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS tasks (
                        id TEXT PRIMARY KEY,
                        script_path TEXT NOT NULL,
                        interval_minutes INTEGER NOT NULL,
                        execute_path TEXT,
                        enabled BOOLEAN DEFAULT 1,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                ''')
                
                # 创建任务执行历史表
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS task_history (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        task_id TEXT NOT NULL,
                        status TEXT NOT NULL,
                        message TEXT,
                        executed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        FOREIGN KEY (task_id) REFERENCES tasks (id) ON DELETE CASCADE
                    )
                ''')
                
                # 创建索引
                cursor.execute('CREATE INDEX IF NOT EXISTS idx_task_history_task_id ON task_history (task_id)')
                cursor.execute('CREATE INDEX IF NOT EXISTS idx_task_history_executed_at ON task_history (executed_at)')
                
                conn.commit()
        except Exception as e:
            print(f"初始化 SQLite 数据库失败: {e}")
            raise
    
    def _get_connection(self) -> sqlite3.connect:
        """获取数据库连接"""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row  # 使结果可以通过列名访问
        return conn
    
    async def get_all_tasks(self) -> List[Dict[str, Any]]:
        """获取所有任务"""
        async with self._lock:
            try:
                with self._get_connection() as conn:
                    cursor = conn.cursor()
                    cursor.execute('''
                        SELECT id, script_path, interval_minutes, execute_path, enabled,
                               created_at, updated_at
                        FROM tasks
                        ORDER BY created_at DESC
                    ''')
                    
                    tasks = []
                    for row in cursor.fetchall():
                        task = dict(row)
                        # 转换布尔值
                        task['enabled'] = bool(task['enabled'])
                        tasks.append(task)
                    
                    return tasks
            except Exception as e:
                print(f"从 SQLite 获取任务失败: {e}")
                return []
    
    async def get_task(self, task_id: str) -> Optional[Dict[str, Any]]:
        """获取单个任务"""
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute('''
                    SELECT id, script_path, interval_minutes, execute_path, enabled,
                           created_at, updated_at
                    FROM tasks
                    WHERE id = ?
                ''', (task_id,))
                
                row = cursor.fetchone()
                if row:
                    task = dict(row)
                    task['enabled'] = bool(task['enabled'])
                    return task
                return None
        except Exception as e:
            print(f"从 SQLite 获取任务失败: {e}")
            return None
    
    async def add_task(self, task: Dict[str, Any]) -> bool:
        """添加任务"""
        async with self._lock:
            try:
                with self._get_connection() as conn:
                    cursor = conn.cursor()
                    
                    # 检查任务是否已存在
                    cursor.execute('SELECT id FROM tasks WHERE id = ?', (task['id'],))
                    if cursor.fetchone():
                        raise ValueError(f"任务ID '{task['id']}' 已存在")
                    
                    # 插入新任务
                    cursor.execute('''
                        INSERT INTO tasks (id, script_path, interval_minutes, execute_path, enabled)
                        VALUES (?, ?, ?, ?, ?)
                    ''', (
                        task['id'],
                        task['script_path'],
                        task['interval_minutes'],
                        task.get('execute_path'),
                        1 if task.get('enabled', True) else 0
                    ))
                    
                    conn.commit()
                    return True
            except Exception as e:
                print(f"添加任务到 SQLite 失败: {e}")
                return False
    
    async def update_task(self, task_id: str, updates: Dict[str, Any]) -> bool:
        """更新任务"""
        async with self._lock:
            try:
                with self._get_connection() as conn:
                    cursor = conn.cursor()
                    
                    # 检查任务是否存在
                    cursor.execute('SELECT id FROM tasks WHERE id = ?', (task_id,))
                    if not cursor.fetchone():
                        raise ValueError(f"任务 '{task_id}' 不存在")
                    
                    # 构建更新语句
                    update_fields = []
                    update_values = []
                    
                    if 'script_path' in updates:
                        update_fields.append('script_path = ?')
                        update_values.append(updates['script_path'])
                    
                    if 'interval_minutes' in updates:
                        update_fields.append('interval_minutes = ?')
                        update_values.append(updates['interval_minutes'])
                    
                    if 'execute_path' in updates:
                        update_fields.append('execute_path = ?')
                        update_values.append(updates['execute_path'])
                    
                    if 'enabled' in updates:
                        update_fields.append('enabled = ?')
                        update_values.append(1 if updates['enabled'] else 0)
                    
                    if update_fields:
                        update_fields.append('updated_at = CURRENT_TIMESTAMP')
                        update_values.append(task_id)
                        
                        cursor.execute(f'''
                            UPDATE tasks 
                            SET {', '.join(update_fields)}
                            WHERE id = ?
                        ''', update_values)
                        
                        conn.commit()
                        return True
                    
                    return True  # 没有更新字段时返回成功
            except Exception as e:
                print(f"更新 SQLite 任务失败: {e}")
                return False
    
    async def delete_task(self, task_id: str) -> bool:
        """删除任务"""
        async with self._lock:
            try:
                with self._get_connection() as conn:
                    cursor = conn.cursor()
                    
                    # 检查任务是否存在
                    cursor.execute('SELECT id FROM tasks WHERE id = ?', (task_id,))
                    if not cursor.fetchone():
                        raise ValueError(f"任务 '{task_id}' 不存在")
                    
                    # 删除任务（历史记录会通过外键约束自动删除）
                    cursor.execute('DELETE FROM tasks WHERE id = ?', (task_id,))
                    
                    conn.commit()
                    return True
            except Exception as e:
                print(f"从 SQLite 删除任务失败: {e}")
                return False
    
    async def task_exists(self, task_id: str) -> bool:
        """检查任务是否存在"""
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute('SELECT id FROM tasks WHERE id = ?', (task_id,))
                return cursor.fetchone() is not None
        except Exception as e:
            print(f"检查任务存在性失败: {e}")
            return False
    
    async def save_tasks(self, tasks: List[Dict[str, Any]]) -> bool:
        """保存所有任务（批量操作）"""
        async with self._lock:
            try:
                with self._get_connection() as conn:
                    cursor = conn.cursor()
                    
                    # 清空现有任务
                    cursor.execute('DELETE FROM tasks')
                    
                    # 批量插入新任务
                    for task in tasks:
                        cursor.execute('''
                            INSERT INTO tasks (id, script_path, interval_minutes, execute_path, enabled)
                            VALUES (?, ?, ?, ?, ?)
                        ''', (
                            task['id'],
                            task['script_path'],
                            task['interval_minutes'],
                            task.get('execute_path'),
                            1 if task.get('enabled', True) else 0
                        ))
                    
                    conn.commit()
                    return True
            except Exception as e:
                print(f"批量保存任务到 SQLite 失败: {e}")
                return False
    
    async def add_task_history(self, task_id: str, history_entry: Dict[str, Any]) -> bool:
        """添加任务执行历史"""
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                
                # 从历史记录条目中提取信息
                status = history_entry.get('status', 'UNKNOWN')
                message = history_entry.get('message', '')
                
                cursor.execute('''
                    INSERT INTO task_history (task_id, status, message)
                    VALUES (?, ?, ?)
                ''', (task_id, status, message))
                
                conn.commit()
                return True
        except Exception as e:
            print(f"添加任务历史记录失败: {e}")
            return False
    
    async def get_task_history(self, task_id: str, limit: int = 50) -> List[Dict[str, Any]]:
        """获取任务执行历史"""
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute('''
                    SELECT id, task_id, status, message, executed_at
                    FROM task_history
                    WHERE task_id = ?
                    ORDER BY executed_at DESC
                    LIMIT ?
                ''', (task_id, limit))
                
                history = []
                for row in cursor.fetchall():
                    history.append(dict(row))
                
                return history
        except Exception as e:
            print(f"获取任务历史记录失败: {e}")
            return []
    
    async def get_recent_history(self, limit: int = 100) -> List[Dict[str, Any]]:
        """获取最近的执行历史"""
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute('''
                    SELECT h.id, h.task_id, h.status, h.message, h.executed_at,
                           t.script_path
                    FROM task_history h
                    LEFT JOIN tasks t ON h.task_id = t.id
                    ORDER BY h.executed_at DESC
                    LIMIT ?
                ''', (limit,))
                
                history = []
                for row in cursor.fetchall():
                    history.append(dict(row))
                
                return history
        except Exception as e:
            print(f"获取最近历史记录失败: {e}")
            return []
    
    async def clear_task_history(self, task_id: str) -> bool:
        """清空任务执行历史"""
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute('''
                    DELETE FROM task_history
                    WHERE task_id = ?
                ''', (task_id,))
                
                conn.commit()
                return True
        except Exception as e:
            print(f"清空任务历史记录失败: {e}")
            return False
    
    async def cleanup_old_history(self, days: int = 30) -> bool:
        """清理旧的历史记录"""
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute('''
                    DELETE FROM task_history
                    WHERE executed_at < datetime('now', '-{} days')
                '''.format(days))
                
                deleted_count = cursor.rowcount
                conn.commit()
                print(f"清理了 {deleted_count} 条历史记录")
                return True
        except Exception as e:
            print(f"清理历史记录失败: {e}")
            return False 
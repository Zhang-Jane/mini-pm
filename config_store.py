#!/usr/bin/env python3
"""
配置存储系统
支持JSON文件、数据库和Redis三种存储方式
"""

import os
import json
import asyncio
import sqlite3
from abc import ABC, abstractmethod
from typing import Dict, Any, Optional, Union
from pathlib import Path
import redis.asyncio as redis


class ConfigStore(ABC):
    """配置存储基础接口"""
    
    @abstractmethod
    async def get_config(self, key: str, default: Any = None) -> Any:
        """获取配置值"""
        pass
    
    @abstractmethod
    async def set_config(self, key: str, value: Any) -> bool:
        """设置配置值"""
        pass
    
    @abstractmethod
    async def update_config(self, config_dict: Dict[str, Any]) -> bool:
        """批量更新配置"""
        pass
    
    @abstractmethod
    async def get_all_config(self) -> Dict[str, Any]:
        """获取所有配置"""
        pass
    
    @abstractmethod
    async def delete_config(self, key: str) -> bool:
        """删除配置"""
        pass
    
    @abstractmethod
    async def clear_config(self) -> bool:
        """清空所有配置"""
        pass


class JSONConfigStore(ConfigStore):
    """JSON文件配置存储"""
    
    def __init__(self, config_file: str = "config.json"):
        self.config_file = config_file
        self.config_cache: Dict[str, Any] = {}
        self._ensure_config_file()
        self._load_config()
    
    def _ensure_config_file(self):
        """确保配置文件存在"""
        if not os.path.exists(self.config_file):
            # 创建默认配置
            default_config = {
                "storage_type": "json",
                "enable_monitoring": True,
                "monitoring_interval": 60,
                "enable_dingtalk_alert": False,
                "dingtalk_access_token": "",
                "dingtalk_url": "https://oapi.dingtalk.com/robot/send?access_token=",
                "enable_system_monitor": False,
                "cpu_threshold": 89.0,
                "memory_threshold": 90.0,
                "disk_threshold": 90.0,
                "monitor_check_interval": 60,
                "host": "0.0.0.0",
                "port": 8100,
                "log_limit": 500,
                "check_interval": 30,
                "export_path": "./exports",
                "max_export_size": 10485760
            }
            self._save_config_to_file(default_config)
    
    def _load_config(self):
        """从文件加载配置"""
        try:
            if os.path.exists(self.config_file):
                with open(self.config_file, 'r', encoding='utf-8') as f:
                    self.config_cache = json.load(f)
            else:
                self.config_cache = {}
        except Exception as e:
            print(f"加载配置文件失败: {e}")
            self.config_cache = {}
    
    def _save_config_to_file(self, config: Dict[str, Any]) -> bool:
        """保存配置到文件"""
        try:
            with open(self.config_file, 'w', encoding='utf-8') as f:
                json.dump(config, f, indent=2, ensure_ascii=False)
            return True
        except Exception as e:
            print(f"保存配置文件失败: {e}")
            return False
    
    async def get_config(self, key: str, default: Any = None) -> Any:
        """获取配置值"""
        return self.config_cache.get(key, default)
    
    async def set_config(self, key: str, value: Any) -> bool:
        """设置配置值"""
        self.config_cache[key] = value
        return self._save_config_to_file(self.config_cache)
    
    async def update_config(self, config_dict: Dict[str, Any]) -> bool:
        """批量更新配置"""
        self.config_cache.update(config_dict)
        return self._save_config_to_file(self.config_cache)
    
    async def get_all_config(self) -> Dict[str, Any]:
        """获取所有配置"""
        return self.config_cache.copy()
    
    async def delete_config(self, key: str) -> bool:
        """删除配置"""
        if key in self.config_cache:
            del self.config_cache[key]
            return self._save_config_to_file(self.config_cache)
        return True
    
    async def clear_config(self) -> bool:
        """清空所有配置"""
        self.config_cache.clear()
        return self._save_config_to_file(self.config_cache)


class SQLiteConfigStore(ConfigStore):
    """SQLite数据库配置存储"""
    
    def __init__(self, db_file: str = "config.db"):
        self.db_file = db_file
        self._ensure_db()
    
    def _ensure_db(self):
        """确保数据库和表存在"""
        try:
            conn = sqlite3.connect(self.db_file)
            cursor = conn.cursor()
            
            # 创建配置表
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS config (
                    key TEXT PRIMARY KEY,
                    value TEXT,
                    type TEXT,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            
            # 插入默认配置
            default_configs = [
                ("storage_type", "json", "str"),
                ("enable_monitoring", "true", "bool"),
                ("monitoring_interval", "60", "int"),
                ("enable_dingtalk_alert", "false", "bool"),
                ("dingtalk_access_token", "", "str"),
                ("dingtalk_url", "https://oapi.dingtalk.com/robot/send?access_token=", "str"),
                ("enable_system_monitor", "false", "bool"),
                ("cpu_threshold", "89.0", "float"),
                ("memory_threshold", "90.0", "float"),
                ("disk_threshold", "90.0", "float"),
                ("monitor_check_interval", "60", "int"),
                ("host", "0.0.0.0", "str"),
                ("port", "8100", "int"),
                ("log_limit", "500", "int"),
                ("check_interval", "30", "int"),
                ("export_path", "./exports", "str"),
                ("max_export_size", "10485760", "int")
            ]
            
            for key, value, type_name in default_configs:
                cursor.execute('''
                    INSERT OR IGNORE INTO config (key, value, type) 
                    VALUES (?, ?, ?)
                ''', (key, value, type_name))
            
            conn.commit()
            conn.close()
        except Exception as e:
            print(f"初始化数据库失败: {e}")
    
    def _convert_value(self, value: str, type_name: str) -> Any:
        """转换值类型"""
        try:
            if type_name == "bool":
                return value.lower() == "true"
            elif type_name == "int":
                return int(value)
            elif type_name == "float":
                return float(value)
            else:
                return value
        except:
            return value
    
    def _serialize_value(self, value: Any) -> str:
        """序列化值"""
        return str(value)
    
    async def get_config(self, key: str, default: Any = None) -> Any:
        """获取配置值"""
        try:
            conn = sqlite3.connect(self.db_file)
            cursor = conn.cursor()
            
            cursor.execute('SELECT value, type FROM config WHERE key = ?', (key,))
            result = cursor.fetchone()
            
            conn.close()
            
            if result:
                value, type_name = result
                return self._convert_value(value, type_name)
            else:
                return default
        except Exception as e:
            print(f"获取配置失败: {e}")
            return default
    
    async def set_config(self, key: str, value: Any) -> bool:
        """设置配置值"""
        try:
            conn = sqlite3.connect(self.db_file)
            cursor = conn.cursor()
            
            # 确定值的类型
            if isinstance(value, bool):
                type_name = "bool"
            elif isinstance(value, int):
                type_name = "int"
            elif isinstance(value, float):
                type_name = "float"
            else:
                type_name = "str"
            
            cursor.execute('''
                INSERT OR REPLACE INTO config (key, value, type, updated_at) 
                VALUES (?, ?, ?, CURRENT_TIMESTAMP)
            ''', (key, self._serialize_value(value), type_name))
            
            conn.commit()
            conn.close()
            return True
        except Exception as e:
            print(f"设置配置失败: {e}")
            return False
    
    async def update_config(self, config_dict: Dict[str, Any]) -> bool:
        """批量更新配置"""
        try:
            conn = sqlite3.connect(self.db_file)
            cursor = conn.cursor()
            
            for key, value in config_dict.items():
                # 确定值的类型
                if isinstance(value, bool):
                    type_name = "bool"
                elif isinstance(value, int):
                    type_name = "int"
                elif isinstance(value, float):
                    type_name = "float"
                else:
                    type_name = "str"
                
                cursor.execute('''
                    INSERT OR REPLACE INTO config (key, value, type, updated_at) 
                    VALUES (?, ?, ?, CURRENT_TIMESTAMP)
                ''', (key, self._serialize_value(value), type_name))
            
            conn.commit()
            conn.close()
            return True
        except Exception as e:
            print(f"批量更新配置失败: {e}")
            return False
    
    async def get_all_config(self) -> Dict[str, Any]:
        """获取所有配置"""
        try:
            conn = sqlite3.connect(self.db_file)
            cursor = conn.cursor()
            
            cursor.execute('SELECT key, value, type FROM config')
            results = cursor.fetchall()
            
            conn.close()
            
            config = {}
            for key, value, type_name in results:
                config[key] = self._convert_value(value, type_name)
            
            return config
        except Exception as e:
            print(f"获取所有配置失败: {e}")
            return {}
    
    async def delete_config(self, key: str) -> bool:
        """删除配置"""
        try:
            conn = sqlite3.connect(self.db_file)
            cursor = conn.cursor()
            
            cursor.execute('DELETE FROM config WHERE key = ?', (key,))
            
            conn.commit()
            conn.close()
            return True
        except Exception as e:
            print(f"删除配置失败: {e}")
            return False
    
    async def clear_config(self) -> bool:
        """清空所有配置"""
        try:
            conn = sqlite3.connect(self.db_file)
            cursor = conn.cursor()
            
            cursor.execute('DELETE FROM config')
            
            conn.commit()
            conn.close()
            return True
        except Exception as e:
            print(f"清空配置失败: {e}")
            return False


class RedisConfigStore(ConfigStore):
    """Redis配置存储"""
    
    def __init__(self, redis_url: str = "redis://localhost:6379", db: int = 1):
        self.redis_url = redis_url
        self.db = db
        self.redis_client = None
        self._connect_redis()
    
    def _connect_redis(self):
        """连接Redis"""
        try:
            self.redis_client = redis.from_url(self.redis_url, db=self.db)
        except Exception as e:
            print(f"连接Redis失败: {e}")
            self.redis_client = None
    
    async def _ensure_redis_connected(self):
        """确保Redis连接"""
        if not self.redis_client:
            self._connect_redis()
        if self.redis_client:
            try:
                await self.redis_client.ping()
            except:
                self._connect_redis()
    
    async def get_config(self, key: str, default: Any = None) -> Any:
        """获取配置值"""
        try:
            await self._ensure_redis_connected()
            if not self.redis_client:
                return default
            
            value = await self.redis_client.get(f"config:{key}")
            if value is None:
                return default
            
            # 尝试解析JSON
            try:
                return json.loads(value)
            except:
                return value.decode('utf-8')
        except Exception as e:
            print(f"获取配置失败: {e}")
            return default
    
    async def set_config(self, key: str, value: Any) -> bool:
        """设置配置值"""
        try:
            await self._ensure_redis_connected()
            if not self.redis_client:
                return False
            
            # 序列化为JSON
            serialized_value = json.dumps(value, ensure_ascii=False)
            await self.redis_client.set(f"config:{key}", serialized_value)
            return True
        except Exception as e:
            print(f"设置配置失败: {e}")
            return False
    
    async def update_config(self, config_dict: Dict[str, Any]) -> bool:
        """批量更新配置"""
        try:
            await self._ensure_redis_connected()
            if not self.redis_client:
                return False
            
            # 批量设置
            pipeline = self.redis_client.pipeline()
            for key, value in config_dict.items():
                serialized_value = json.dumps(value, ensure_ascii=False)
                pipeline.set(f"config:{key}", serialized_value)
            
            await pipeline.execute()
            return True
        except Exception as e:
            print(f"批量更新配置失败: {e}")
            return False
    
    async def get_all_config(self) -> Dict[str, Any]:
        """获取所有配置"""
        try:
            await self._ensure_redis_connected()
            if not self.redis_client:
                return {}
            
            # 获取所有配置键
            keys = await self.redis_client.keys("config:*")
            config = {}
            
            for key in keys:
                key_name = key.decode('utf-8').replace("config:", "")
                value = await self.redis_client.get(key)
                
                if value:
                    try:
                        config[key_name] = json.loads(value)
                    except:
                        config[key_name] = value.decode('utf-8')
            
            return config
        except Exception as e:
            print(f"获取所有配置失败: {e}")
            return {}
    
    async def delete_config(self, key: str) -> bool:
        """删除配置"""
        try:
            await self._ensure_redis_connected()
            if not self.redis_client:
                return False
            
            await self.redis_client.delete(f"config:{key}")
            return True
        except Exception as e:
            print(f"删除配置失败: {e}")
            return False
    
    async def clear_config(self) -> bool:
        """清空所有配置"""
        try:
            await self._ensure_redis_connected()
            if not self.redis_client:
                return False
            
            keys = await self.redis_client.keys("config:*")
            if keys:
                await self.redis_client.delete(*keys)
            return True
        except Exception as e:
            print(f"清空配置失败: {e}")
            return False


class ConfigStoreFactory:
    """配置存储工厂"""
    
    @staticmethod
    def create_config_store(store_type: str, **kwargs) -> ConfigStore:
        """创建配置存储实例"""
        if store_type == "json":
            config_file = kwargs.get("config_file", "config.json")
            return JSONConfigStore(config_file)
        elif store_type == "sqlite":
            db_file = kwargs.get("db_file", "config.db")
            return SQLiteConfigStore(db_file)
        elif store_type == "redis":
            redis_url = kwargs.get("redis_url", "redis://localhost:6379")
            db = kwargs.get("db", 1)
            return RedisConfigStore(redis_url, db)
        else:
            raise ValueError(f"不支持的存储类型: {store_type}")


# 全局配置存储实例
config_store: Optional[ConfigStore] = None


async def init_config_store(store_type: str = "json", **kwargs):
    """初始化配置存储"""
    global config_store
    try:
        config_store = ConfigStoreFactory.create_config_store(store_type, **kwargs)
        print(f"✅ 配置存储初始化成功: {store_type}")
        return True
    except Exception as e:
        print(f"❌ 配置存储初始化失败: {e}")
        return False


async def get_config(key: str, default: Any = None) -> Any:
    """获取配置值"""
    if config_store:
        return await config_store.get_config(key, default)
    return default


async def set_config(key: str, value: Any) -> bool:
    """设置配置值"""
    if config_store:
        return await config_store.set_config(key, value)
    return False


async def update_config(config_dict: Dict[str, Any]) -> bool:
    """批量更新配置"""
    if config_store:
        return await config_store.update_config(config_dict)
    return False


async def get_all_config() -> Dict[str, Any]:
    """获取所有配置"""
    if config_store:
        return await config_store.get_all_config()
    return {}


async def delete_config(key: str) -> bool:
    """删除配置"""
    if config_store:
        return await config_store.delete_config(key)
    return False


async def clear_config() -> bool:
    """清空所有配置"""
    if config_store:
        return await config_store.clear_config()
    return False 
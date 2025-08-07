"""
Mini PM2 FastAPI 主应用
支持 JSON 文件、Redis 和 SQLite 三种任务配置源
"""

import os
import json
import asyncio
import threading
import queue
import select
import time
import platform
from typing import Dict, List, Optional, Any
from datetime import datetime
from contextlib import asynccontextmanager

# Windows 兼容性：使用 ProactorEventLoop
if platform.system() == "Windows":
    try:
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
        # 确保事件循环策略在导入其他模块前设置
        print("✅ Windows 兼容性：已设置 ProactorEventLoop")
    except Exception as e:
        print(f"⚠️ Windows 兼容性设置失败: {e}")
        # 尝试强制设置
        try:
            import asyncio.windows_events
            asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
            print("✅ Windows 兼容性：强制设置 ProactorEventLoop 成功")
        except Exception as e2:
            print(f"❌ Windows 兼容性设置完全失败: {e2}")

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect, HTTPException, UploadFile, Form
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field
import uvicorn
import redis.asyncio as redis

from task_store.base import TaskStore
from task_store.json_store import JSONTaskStore
from task_store.redis_store import RedisTaskStore
from task_store.sqlite_store import SQLiteTaskStore
from services.task_service import TaskService
from services.monitoring_service import MonitoringService
from services.export_service import ExportService
from services.git_service import GitService
from config_store import init_config_store, get_config, set_config, update_config, get_all_config

# 配置存储类型
CONFIG_STORAGE_TYPE = os.getenv("CONFIG_STORAGE_TYPE", "json")  # json、sqlite 或 redis

# 默认配置（用于初始化）
DEFAULT_CONFIG = {
    "storage_type": os.getenv("STORAGE_TYPE", "json"),  # json、redis 或 sqlite
    "redis_url": os.getenv("REDIS_URL", "redis://localhost:6379"),
    "redis_db": int(os.getenv("REDIS_DB", "0")),
    "json_file": os.getenv("TASKS_FILE", "tasks.json"),
    "jobs_directory": os.getenv("JOBS_DIRECTORY", "./jobs"),
    "task_file_prefix": os.getenv("TASK_FILE_PREFIX", "task"),
    "sqlite_db": os.getenv("SQLITE_DB", "tasks.db"),
    "log_limit": int(os.getenv("LOG_LIMIT", "500")),
    "check_interval": int(os.getenv("CHECK_INTERVAL", "30")),
    "enable_monitoring": os.getenv("ENABLE_MONITORING", "true").lower() == "true",
    "monitoring_interval": int(os.getenv("MONITORING_INTERVAL", "60")),
    "export_path": os.getenv("EXPORT_PATH", "./exports"),
    "max_export_size": int(os.getenv("MAX_EXPORT_SIZE", "10485760")),  # 10MB
    "host": os.getenv("HOST", "0.0.0.0"),
    "port": int(os.getenv("PORT", "8121")),
    # 钉钉告警配置
    "enable_dingtalk_alert": os.getenv("ENABLE_DINGTALK_ALERT", "false").lower() == "true",
    "dingtalk_access_token": os.getenv("DINGTALK_ACCESS_TOKEN", ""),
    "dingtalk_url": os.getenv("DINGTALK_URL", "https://oapi.dingtalk.com/robot/send?access_token="),
    # 系统监控配置
                 "enable_system_monitor": os.getenv("ENABLE_SYSTEM_MONITOR", "false").lower() == "true",
             "cpu_threshold": float(os.getenv("CPU_THRESHOLD", "89.0")),
             "memory_threshold": float(os.getenv("MEMORY_THRESHOLD", "90.0")),
             "disk_threshold": float(os.getenv("DISK_THRESHOLD", "90.0")),
             "monitor_check_interval": int(os.getenv("MONITOR_CHECK_INTERVAL", "60")),
             "alert_cooldown_interval": int(os.getenv("ALERT_COOLDOWN_INTERVAL", "300"))
}

# 全局配置变量
CONFIG = DEFAULT_CONFIG.copy()

# 全局变量
task_store: Optional[TaskStore] = None
task_service: Optional[TaskService] = None
monitoring_service: Optional[MonitoringService] = None
export_service: Optional[ExportService] = None
git_service: Optional[GitService] = None
redis_client: Optional[redis.Redis] = None
log_buffer: List[str] = []
task_status: Dict[str, Dict[str, Any]] = {}
websocket_connections: List[WebSocket] = []
dingtalk_alert: Optional['DingTalkAlert'] = None
system_monitor: Optional['SystemMonitor'] = None
active_terminals: Dict[str, Dict[str, Any]] = {}

# 广播日志函数 - 异步优化版本
async def _broadcast_log(message: str):
    """广播日志到所有WebSocket连接 - 异步优化版本"""
    if not websocket_connections:
        return
    
    message_data = {
        "type": "log",
        "message": message
    }
    
    # 并发发送到所有连接，提高性能
    tasks = []
    disconnected_websockets = []
    
    for websocket in websocket_connections:
        try:
            task = asyncio.create_task(websocket.send_json(message_data))
            tasks.append((websocket, task))
        except Exception:
            disconnected_websockets.append(websocket)
    
    # 等待所有发送任务完成
    if tasks:
        try:
            await asyncio.wait([task for _, task in tasks], timeout=5.0)
        except asyncio.TimeoutError:
            pass
    
    # 清理断开的连接
    for websocket, task in tasks:
        if task.done() and task.exception():
            disconnected_websockets.append(websocket)
    
    # 移除断开的连接
    for websocket in disconnected_websockets:
        if websocket in websocket_connections:
            websocket_connections.remove(websocket)

# Pydantic 模型
class TaskCreate(BaseModel):
    id: str = Field(..., description="任务ID")
    script_path: str = Field(..., description="脚本路径")
    interval_minutes: int = Field(..., ge=1, description="执行间隔(分钟)")
    execute_path: Optional[str] = Field(None, description="解释器路径")
    enabled: bool = Field(True, description="是否启用")

class TaskUpdate(BaseModel):
    script_path: Optional[str] = Field(None, description="脚本路径")
    interval_minutes: Optional[int] = Field(None, ge=1, description="执行间隔(分钟)")
    execute_path: Optional[str] = Field(None, description="解释器路径")
    enabled: Optional[bool] = Field(None, description="是否启用")

class TaskResponse(BaseModel):
    id: str
    script_path: str
    interval_minutes: int
    execute_path: Optional[str]
    enabled: bool
    status: str  # IDLE, RUNNING, SUCCESS, FAILED, EXCEPTION, DISABLED
    last_success: Optional[str]
    last_error: Optional[str]
    last_run: Optional[float]
    duration: Optional[str]
    run_count: Optional[int] = 0  # 累计运行次数
    output: Optional[List[str]]
    error_detail: Optional[str] = None  # 错误详情
    error_timestamp: Optional[str] = None  # 错误时间戳

class SystemStatus(BaseModel):
    total_tasks: int
    running_tasks: int
    failed_tasks: int
    disabled_tasks: int
    log_count: int

# 应用生命周期
@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用启动和关闭时的处理"""
    global task_store, task_service, redis_client, export_service, git_service, CONFIG
    
    # 启动时初始化
    print("🚀 启动 Mini PM2 FastAPI 应用...")
    
    # 记录应用启动时间
    app.state.start_time = datetime.now()
    
    # 初始化配置存储系统
    print(f"📊 初始化配置存储: {CONFIG_STORAGE_TYPE}")
    
    # 根据配置存储类型初始化
    if CONFIG_STORAGE_TYPE == "redis":
        redis_url = os.getenv("CONFIG_REDIS_URL", "redis://localhost:6379")
        redis_db = int(os.getenv("CONFIG_REDIS_DB", "1"))
        await init_config_store("redis", redis_url=redis_url, db=redis_db)
    elif CONFIG_STORAGE_TYPE == "sqlite":
        db_file = os.getenv("CONFIG_DB_FILE", "config.db")
        await init_config_store("sqlite", db_file=db_file)
    else:
        config_file = os.getenv("CONFIG_FILE", "config.json")
        await init_config_store("json", config_file=config_file)
    
    # 从配置存储加载配置
    stored_config = await get_all_config()
    if stored_config:
        # 合并存储的配置和默认配置
        CONFIG.update(stored_config)
        print("✅ 从配置存储加载配置成功")
    else:
        # 如果没有存储的配置，使用默认配置并保存
        await update_config(DEFAULT_CONFIG)
        CONFIG.update(DEFAULT_CONFIG)
        print("✅ 使用默认配置并保存到存储")
    
    # 初始化存储
    if CONFIG["storage_type"] == "redis":
        redis_client = redis.from_url(CONFIG["redis_url"], db=CONFIG["redis_db"])
        await redis_client.ping()
        task_store = RedisTaskStore(redis_client)
        print(f"✅ Redis 连接成功: {CONFIG['redis_url']}")
    elif CONFIG["storage_type"] == "sqlite":
        task_store = SQLiteTaskStore(CONFIG["sqlite_db"])
        print(f"✅ SQLite 存储初始化: {CONFIG['sqlite_db']}")
    else:
        # JSON存储支持jobs目录
        if CONFIG.get("jobs_directory"):
            # 确保jobs目录存在
            os.makedirs(CONFIG["jobs_directory"], exist_ok=True)
            task_file = os.path.join(CONFIG["jobs_directory"], CONFIG["json_file"])
        else:
            task_file = CONFIG["json_file"]
        
        task_store = JSONTaskStore(
            task_file, 
            jobs_directory=CONFIG.get("jobs_directory"),
            task_file_prefix=CONFIG.get("task_file_prefix", "task")
        )
        print(f"✅ JSON 存储初始化: {task_file}")
        if CONFIG.get("jobs_directory"):
            print(f"📁 Jobs目录: {CONFIG['jobs_directory']}")
            print(f"📄 任务文件前缀: {CONFIG.get('task_file_prefix', 'task')}")
    
    # 初始化任务服务
    task_service = TaskService(task_store, log_buffer, task_status, websocket_connections)
    await task_service.start()
    
    # 初始化监控服务
    global monitoring_service
    if CONFIG["enable_monitoring"]:
        monitoring_service = MonitoringService(
            enabled=CONFIG["enable_monitoring"],
            interval=CONFIG["monitoring_interval"]
        )
        await monitoring_service.start()
    
    # 初始化导出服务
    export_service = ExportService(
        export_path=CONFIG["export_path"],
        max_size=CONFIG["max_export_size"]
    )
    
    # 初始化 Git 服务
    git_service = GitService()
    
    # 初始化钉钉告警
    global dingtalk_alert
    if CONFIG["enable_dingtalk_alert"] and CONFIG["dingtalk_access_token"]:
        try:
            from services.dingtalk_alert import DingTalkAlert
            dingtalk_alert = DingTalkAlert(
                access_token=CONFIG["dingtalk_access_token"],
                ding_url=CONFIG["dingtalk_url"]
            )
            print("✅ 钉钉告警初始化成功")
        except Exception as e:
            print(f"❌ 钉钉告警初始化失败: {e}")
            dingtalk_alert = None
    else:
        dingtalk_alert = None
        print("ℹ️ 钉钉告警未启用")
    
    # 初始化系统监控
    global system_monitor
    if CONFIG["enable_system_monitor"]:
        try:
            from services.system_monitor import SystemMonitor
            system_monitor = SystemMonitor(dingtalk_alert)
            
            # 设置监控阈值
            system_monitor.update_thresholds({
                "cpu_usage": CONFIG["cpu_threshold"],
                "memory_usage": CONFIG["memory_threshold"],
                "disk_usage": CONFIG["disk_threshold"],
                "check_interval": CONFIG["monitor_check_interval"]
            })
            
            await system_monitor.start()
            print("✅ 系统监控初始化成功")
        except Exception as e:
            print(f"❌ 系统监控初始化失败: {e}")
            system_monitor = None
    else:
        system_monitor = None
        print("ℹ️ 系统监控未启用")
    
    # 初始化日志管理器
    from services.log_manager import get_log_manager
    get_log_manager(broadcast_callback=_broadcast_log, log_buffer=log_buffer)
    
    # 发送应用启动通知
    if dingtalk_alert:
        try:
            dingtalk_alert.send_system_alert(
                "系统启动",
                f"Mini PM2 系统已启动\n启动时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n存储类型: {CONFIG['storage_type']}\n监控状态: {'启用' if CONFIG['enable_monitoring'] else '禁用'}"
            )
        except Exception as e:
            print(f"发送启动通知失败: {e}")
    
    print("✅ 应用初始化完成")
    
    yield
    
    # 关闭时清理
    print("🛑 关闭应用...")
    
    # 发送应用关闭通知
    if dingtalk_alert:
        try:
            dingtalk_alert.send_system_alert(
                "系统关闭",
                f"Mini PM2 系统正在关闭\n关闭时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
            )
        except Exception as e:
            print(f"发送关闭通知失败: {e}")
    
    if task_service:
        await task_service.stop()
    if monitoring_service:
        await monitoring_service.stop()
    if system_monitor:
        await system_monitor.stop()
    if redis_client:
        await redis_client.close()
    print("✅ 应用已关闭")

# 创建 FastAPI 应用
app = FastAPI(
    title="Mini PM2",
    description="轻量级任务调度和管理系统",
    version="1.0.0",
    lifespan=lifespan
)

# 静态文件和模板
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

# ==================== 页面路由 ====================

@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    """仪表板页面"""
    return templates.TemplateResponse("dashboard.html", {"request": request})

@app.get("/tasks", response_class=HTMLResponse)
async def tasks_page(request: Request):
    """任务管理页面"""
    return templates.TemplateResponse("tasks.html", {"request": request})

@app.get("/logs", response_class=HTMLResponse)
async def logs_page(request: Request):
    """日志查看页面"""
    return templates.TemplateResponse("logs.html", {"request": request})

@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    """系统设置页面"""
    return templates.TemplateResponse("settings.html", {"request": request})

@app.get("/terminal", response_class=HTMLResponse)
async def terminal_page(request: Request):
    """终端管理页面"""
    return templates.TemplateResponse("terminal.html", {"request": request})

@app.get("/terminal-enhanced", response_class=HTMLResponse)
async def terminal_enhanced_page(request: Request):
    """增强终端管理页面"""
    return templates.TemplateResponse("terminal_enhanced.html", {"request": request})

@app.get("/git", response_class=HTMLResponse)
async def git_page(request: Request):
    """Git 仓库管理页面"""
    return templates.TemplateResponse("git.html", {"request": request})

# ==================== API 路由 ====================

@app.get("/api/status")
async def get_status() -> SystemStatus:
    """获取系统状态"""
    total = len(task_status)
    running = sum(1 for s in task_status.values() if s.get("status") == "RUNNING")
    failed = sum(1 for s in task_status.values() if s.get("status") == "FAILED")
    disabled = sum(1 for s in task_status.values() if s.get("status") == "DISABLED")
    
    return SystemStatus(
        total_tasks=total,
        running_tasks=running,
        failed_tasks=failed,
        disabled_tasks=disabled,
        log_count=len(log_buffer)
    )

@app.get("/api/tasks/stats")
async def get_tasks_stats() -> Dict[str, Any]:
    """获取任务统计信息"""
    if not task_store:
        raise HTTPException(status_code=500, detail="任务存储未初始化")
    
    try:
        tasks = await task_store.get_all_tasks()
        
        stats = {
            "total": len(tasks),
            "enabled": 0,
            "disabled": 0,
            "running": 0,
            "failed": 0,
            "success": 0,
            "status_breakdown": {}
        }
        
        for task in tasks:
            if task.get("enabled", True):
                stats["enabled"] += 1
            else:
                stats["disabled"] += 1
            
            task_status_info = task_status.get(task["id"], {"status": "DISABLED"})
            status = task_status_info.get("status", "DISABLED")
            
            if status not in stats["status_breakdown"]:
                stats["status_breakdown"][status] = 0
            stats["status_breakdown"][status] += 1
            
            if status == "RUNNING":
                stats["running"] += 1
            elif status == "FAILED":
                stats["failed"] += 1
            elif status == "SUCCESS":
                stats["success"] += 1
        
        return stats
    except Exception as e:
        print(f"获取任务统计失败: {e}")
        return {
            "total": 0,
            "enabled": 0,
            "disabled": 0,
            "running": 0,
            "failed": 0,
            "success": 0,
            "status_breakdown": {}
        }

@app.get("/api/tasks/search")
async def search_tasks(
    query: Optional[str] = None,
    status: Optional[str] = None,
    enabled: Optional[bool] = None,
    page: int = 1,
    limit: int = 20
) -> Dict[str, Any]:
    """搜索和筛选任务"""
    if not task_store:
        raise HTTPException(status_code=500, detail="任务存储未初始化")
    
    try:
        tasks = await task_store.get_all_tasks()
        result = []
        
        for task in tasks:
            task_status_info = task_status.get(task["id"], {"status": "DISABLED"})
            
            # 应用筛选条件
            if query and query.lower() not in task["id"].lower() and query.lower() not in task["script_path"].lower():
                continue
            if status and task_status_info.get("status") != status:
                continue
            if enabled is not None and task.get("enabled", True) != enabled:
                continue
            
            result.append(TaskResponse(
                id=task["id"],
                script_path=task["script_path"],
                interval_minutes=task["interval_minutes"],
                execute_path=task.get("execute_path"),
                enabled=task.get("enabled", True),
                status=task_status_info.get("status", "DISABLED"),
                last_success=task_status_info.get("last_success"),
                last_error=task_status_info.get("last_error")
            ))
        
        # 分页
        total = len(result)
        start = (page - 1) * limit
        end = start + limit
        paginated_tasks = result[start:end]
        
        return {
            "tasks": [task.dict() for task in paginated_tasks],
            "total": total,
            "page": page,
            "limit": limit,
            "total_pages": (total + limit - 1) // limit,
            "has_prev": page > 1,
            "has_next": end < total
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"搜索任务失败: {str(e)}")

@app.get("/api/tasks")
async def get_tasks() -> List[TaskResponse]:
    """获取所有任务"""
    if not task_store:
        raise HTTPException(status_code=500, detail="任务存储未初始化")
    
    tasks = await task_store.get_all_tasks()
    result = []
    
    for task in tasks:
        status = task_status.get(task["id"], {"status": "DISABLED"})
        # 确保enabled字段与status字段一致
        task_enabled = task.get("enabled", True)
        task_status_value = status.get("status", "DISABLED")
        
        # 如果任务被禁用，状态应该是DISABLED
        if not task_enabled:
            task_status_value = "DISABLED"
        
        result.append(TaskResponse(
            id=task["id"],
            script_path=task["script_path"],
            interval_minutes=task["interval_minutes"],
            execute_path=task.get("execute_path"),
            enabled=task_enabled,
            status=task_status_value,
            last_success=status.get("last_success"),
            last_error=status.get("last_error"),
            last_run=status.get("last_run"),
            duration=status.get("duration"),
            run_count=status.get("run_count", 0),
            output=status.get("output", []),
            error_detail=status.get("error_detail"),
            error_timestamp=status.get("error_timestamp")
        ))
    
    return result

@app.post("/api/tasks")
async def create_task(task: TaskCreate) -> JSONResponse:
    """创建新任务"""
    if not task_service:
        raise HTTPException(status_code=500, detail="任务服务未初始化")
    
    try:
        await task_service.add_task(
            task_id=task.id,
            script_path=task.script_path,
            interval_minutes=task.interval_minutes,
            execute_path=task.execute_path
        )
        return JSONResponse({"message": "任务创建成功"}, status_code=201)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))



@app.put("/api/tasks/{task_id}")
async def update_task(task_id: str, task_update: TaskUpdate) -> JSONResponse:
    """更新任务配置"""
    if not task_store:
        raise HTTPException(status_code=500, detail="任务存储未初始化")
    
    try:
        # 构建更新数据，只包含非None的字段
        update_data = {}
        if task_update.script_path is not None:
            update_data["script_path"] = task_update.script_path
        if task_update.interval_minutes is not None:
            update_data["interval_minutes"] = task_update.interval_minutes
        if task_update.execute_path is not None:
            update_data["execute_path"] = task_update.execute_path
        if task_update.enabled is not None:
            update_data["enabled"] = task_update.enabled
        
        await task_store.update_task(task_id, update_data)
        await task_service.update_jobs()
        return JSONResponse({"message": "任务更新成功"})
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.delete("/api/tasks/{task_id}")
async def delete_task(task_id: str) -> JSONResponse:
    """删除任务"""
    if not task_service:
        raise HTTPException(status_code=500, detail="任务服务未初始化")
    
    try:
        await task_service.remove_task(task_id)
        return JSONResponse({"message": "任务删除成功"})
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

# 批量操作请求模型
class BatchToggleRequest(BaseModel):
    task_ids: List[str]
    enable: bool

class BatchTaskRequest(BaseModel):
    task_ids: List[str]

# 批量操作API - 必须在单个任务操作之前定义
@app.post("/api/tasks/batch/toggle")
async def batch_toggle_tasks(request: BatchToggleRequest) -> JSONResponse:
    """批量切换任务启用/禁用状态"""
    if not task_service:
        raise HTTPException(status_code=500, detail="任务服务未初始化")
    
    try:
        success_count = 0
        failed_tasks = []
        
        print(f"批量操作: task_ids={request.task_ids}, enable={request.enable}")
        
        for task_id in request.task_ids:
            try:
                print(f"处理任务: {task_id}")
                await task_service.toggle_task(task_id, request.enable)
                success_count += 1
                print(f"任务 {task_id} 处理成功")
            except ValueError as e:
                print(f"任务 {task_id} 处理失败: {e}")
                failed_tasks.append({"task_id": task_id, "error": str(e)})
        
        status = "启用" if request.enable else "禁用"
        message = f"成功{status} {success_count} 个任务"
        if failed_tasks:
            message += f"，失败 {len(failed_tasks)} 个任务"
        
        return JSONResponse({
            "message": message,
            "success_count": success_count,
            "failed_tasks": failed_tasks
        })
    except Exception as e:
        print(f"批量操作异常: {e}")
        raise HTTPException(status_code=500, detail=f"批量操作失败: {str(e)}")

@app.post("/api/tasks/batch/run")
async def batch_run_tasks(request: BatchTaskRequest) -> JSONResponse:
    """批量立即运行任务"""
    if not task_service:
        raise HTTPException(status_code=500, detail="任务服务未初始化")
    
    try:
        success_count = 0
        failed_tasks = []
        
        for task_id in request.task_ids:
            try:
                await task_service.run_task_now(task_id)
                success_count += 1
            except ValueError as e:
                failed_tasks.append({"task_id": task_id, "error": str(e)})
        
        message = f"成功启动 {success_count} 个任务"
        if failed_tasks:
            message += f"，失败 {len(failed_tasks)} 个任务"
        
        return JSONResponse({
            "message": message,
            "success_count": success_count,
            "failed_tasks": failed_tasks
        })
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"批量运行失败: {str(e)}")

@app.post("/api/tasks/batch/delete")
async def batch_delete_tasks(request: BatchTaskRequest) -> JSONResponse:
    """批量删除任务"""
    if not task_service:
        raise HTTPException(status_code=500, detail="任务服务未初始化")
    
    try:
        success_count = 0
        failed_tasks = []
        
        for task_id in request.task_ids:
            try:
                await task_service.remove_task(task_id)
                success_count += 1
            except ValueError as e:
                failed_tasks.append({"task_id": task_id, "error": str(e)})
        
        message = f"成功删除 {success_count} 个任务"
        if failed_tasks:
            message += f"，失败 {len(failed_tasks)} 个任务"
        
        return JSONResponse({
            "message": message,
            "success_count": success_count,
            "failed_tasks": failed_tasks
        })
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"批量删除失败: {str(e)}")

@app.post("/api/tasks/batch/kill")
async def batch_kill_tasks(request: BatchTaskRequest) -> JSONResponse:
    """批量停止任务"""
    if not task_service:
        raise HTTPException(status_code=500, detail="任务服务未初始化")
    
    try:
        success_count = 0
        failed_tasks = []
        
        for task_id in request.task_ids:
            try:
                # 检查任务是否存在
                task = await task_store.get_task(task_id)
                if not task:
                    failed_tasks.append({"task_id": task_id, "error": "任务不存在"})
                    continue
                
                # 获取任务状态
                status = task_status.get(task_id, {})
                if status.get("status") != "RUNNING":
                    failed_tasks.append({"task_id": task_id, "error": "任务当前未在运行"})
                    continue
                
                # 获取进程ID
                process_id = status.get("process_id")
                if not process_id:
                    failed_tasks.append({"task_id": task_id, "error": "无法获取任务进程ID"})
                    continue
                
                # 尝试终止进程
                try:
                    import psutil
                    process = psutil.Process(process_id)
                    process.terminate()  # 先尝试优雅终止
                    
                    # 等待进程结束
                    try:
                        process.wait(timeout=5)
                    except psutil.TimeoutExpired:
                        # 如果超时，强制杀死进程
                        process.kill()
                        process.wait()
                    
                    # 更新任务状态
                    task_status[task_id] = {
                        "status": "STOPPED",
                        "last_stop": time.time(),
                        "process_id": None
                    }
                    
                    success_count += 1
                    
                except psutil.NoSuchProcess:
                    # 进程已经不存在
                    task_status[task_id] = {
                        "status": "STOPPED",
                        "last_stop": time.time(),
                        "process_id": None
                    }
                    success_count += 1
                    
                except Exception as e:
                    failed_tasks.append({"task_id": task_id, "error": f"停止任务失败: {str(e)}"})
                    
            except Exception as e:
                failed_tasks.append({"task_id": task_id, "error": f"操作失败: {str(e)}"})
        
        message = f"成功停止 {success_count} 个任务"
        if failed_tasks:
            message += f"，失败 {len(failed_tasks)} 个任务"
        
        return JSONResponse({
            "message": message,
            "success_count": success_count,
            "failed_tasks": failed_tasks
        })
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"批量停止失败: {str(e)}")

@app.post("/api/tasks/batch/clear-history")
async def batch_clear_task_history(request: BatchTaskRequest) -> JSONResponse:
    """批量清空任务历史"""
    if not task_store:
        raise HTTPException(status_code=500, detail="任务存储未初始化")
    
    try:
        success_count = 0
        failed_tasks = []
        
        for task_id in request.task_ids:
            try:
                await task_store.clear_task_history(task_id)
                success_count += 1
            except ValueError as e:
                failed_tasks.append({"task_id": task_id, "error": str(e)})
        
        message = f"成功清空 {success_count} 个任务的历史记录"
        if failed_tasks:
            message += f"，失败 {len(failed_tasks)} 个任务"
        
        return JSONResponse({
            "message": message,
            "success_count": success_count,
            "failed_tasks": failed_tasks
        })
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"批量清空历史失败: {str(e)}")

# ==================== 终端管理 API ====================

class TerminalCommand(BaseModel):
    command: str = Field(..., description="要执行的命令")

def process_terminal_input(command: str) -> str:
    """处理终端输入，确保兼容所有键盘"""
    if not command:
        return command
    
    processed = command
    
    # 处理常见的键盘输入问题（删除、退格、回车、Ctrl序列）
    # 退格键
    processed = processed.replace('\x08', '\x08')  # 保持退格字符
    
    # 删除键 - 确保正确的ANSI序列
    processed = processed.replace('\x1B[3~', '\x1B[3~')  # 保持删除键序列
    
    # 回车键
    processed = processed.replace('\r\n', '\n')
    processed = processed.replace('\r', '\n')
    
    # Ctrl+U (清空行)
    processed = processed.replace('\x15', '\x15')
    
    # Ctrl+W (删除前一个单词)
    processed = processed.replace('\x17', '\x17')
    
    # Ctrl+K (删除到行尾)
    processed = processed.replace('\x0B', '\x0B')
    
    # Ctrl+A (移动到行首)
    processed = processed.replace('\x01', '\x01')
    
    # Ctrl+E (移动到行尾)
    processed = processed.replace('\x05', '\x05')
    
    # Ctrl+L (清屏)
    processed = processed.replace('\x0C', '\x0C')
    
    # 移除有问题的控制字符
    processed = processed.replace('\x1B[?2004h', '')  # 移除括号粘贴模式
    processed = processed.replace('\x1B[?2004l', '')  # 移除括号粘贴模式
    
    # 处理某些系统发送的DEL字符
    processed = processed.replace('\x7F', '\x1B[3~')
    
    return processed

def clean_pty_output(output: str) -> str:
    """清理PTY输出，移除有问题的控制字符但保留用户输入回显"""
    if not output:
        return output
    
    import re
    
    cleaned = output
    
    # 移除有问题的ANSI序列，但保留颜色和基本格式化
    cleaned = re.sub(r'\x1B\[[0-9]*[@ABCDEFGHIJKLMNOPQRSTUVWXYZ]', '', cleaned)
    cleaned = re.sub(r'\x1B\[[0-9;]*[a-zA-Z]', lambda match: 
        match.group(0) if match.group(0).endswith(('m', 'h', 'l', 's', 'u')) else '', cleaned)
    
    # 移除反向搜索提示符
    cleaned = re.sub(r'\(reverse-i-search\)`[^`]*`:', '', cleaned)
    cleaned = re.sub(r'\(forward-i-search\)`[^`]*`:', '', cleaned)
    
    # 移除bash提示符中的异常字符，但保留基本提示符
    cleaned = re.sub(r'bash-[0-9.]+\[[^\]]*\]', 'bash$ ', cleaned)
    cleaned = re.sub(r'\[[0-9]+@[^\]]*\]', '', cleaned)
    
    # 移除光标位置序列
    cleaned = re.sub(r'\x1B\[[0-9]+;[0-9]+[HR]', '', cleaned)
    
    # 移除字符插入/删除序列
    cleaned = re.sub(r'\x1B\[[0-9]*[P@]', '', cleaned)
    
    # 移除字符替换序列
    cleaned = re.sub(r'\x1B\[[0-9]*[X]', '', cleaned)
    
    # 移除字符擦除序列，但保留删除键的回显
    cleaned = re.sub(r'\x1B\[[0-9]*[K]', '', cleaned)
    
    # 处理删除键的回显 - 替换为空格
    cleaned = cleaned.replace('\x7F', ' ')
    
    # 移除行擦除序列
    cleaned = re.sub(r'\x1B\[[0-9]*[J]', '', cleaned)
    
    # 移除光标保存/恢复序列
    cleaned = re.sub(r'\x1B\[[su]', '', cleaned)
    
    # 移除回车符，保留换行符
    cleaned = cleaned.replace('\r\n', '\n')
    cleaned = cleaned.replace('\r', '\n')
    
    # 移除多余的换行符
    cleaned = re.sub(r'\n{3,}', '\n\n', cleaned)
    
    return cleaned

# 存储活跃的终端会话
active_terminals = {}

@app.websocket("/ws/terminal/{session_id}")
async def websocket_terminal(websocket: WebSocket, session_id: str):
    """WebSocket终端连接 - 跨平台兼容版本"""
    await websocket.accept()
    
    try:
        import os
        import threading
        import queue
        import time
        import platform
        import json
        
        system = platform.system()
        process = None
        master = None
        slave = None
        data_queue = queue.Queue()
        
        # 跨平台兼容性处理
        if system == "Windows":
            # Windows系统使用subprocess直接创建进程
            import subprocess
            import shutil
            
            # 创建PowerShell或cmd进程
            if shutil.which("powershell.exe"):
                process = subprocess.Popen(
                    ["powershell.exe", "-NoLogo"],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                    universal_newlines=True,
                    env={
                        **os.environ,
                        "TERM": "xterm-256color",
                        "POWERSHELL_TELEMETRY_OPTOUT": "1",
                        "POWERSHELL_UPDATECHECK": "Off"
                    }
                )
            else:
                process = subprocess.Popen(
                    ["cmd.exe", "/K", "echo Windows Command Prompt Ready"],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                    universal_newlines=True,
                    env={
                        **os.environ,
                        "TERM": "xterm-256color"
                    }
                )
            
            # Windows系统使用subprocess直接读取
            def read_from_process():
                try:
                    while process.poll() is None:
                        output = process.stdout.readline()
                        if output:
                            cleaned_output = clean_pty_output(output)
                            data_queue.put(cleaned_output)
                        time.sleep(0.01)
                except Exception as e:
                    print(f"Windows进程读取错误: {e}")
            
            # 发送初始命令来设置Windows终端环境
            def send_initial_commands():
                try:
                    time.sleep(0.5)  # 等待进程启动
                    if process and process.poll() is None:
                        # 发送初始命令
                        if shutil.which("powershell.exe"):
                            initial_commands = [
                                "Write-Host 'Windows PowerShell Terminal Ready' -ForegroundColor Green\n",
                                "Write-Host ('Current Directory: ' + (Get-Location).Path) -ForegroundColor Yellow\n",
                                "Write-Host 'Type Get-Help for available commands' -ForegroundColor Cyan\n",
                                "Write-Host 'PS> ' -NoNewline\n"
                            ]
                        else:
                            initial_commands = [
                                "echo Windows Command Prompt Ready\n",
                                "echo Current Directory: %CD%\n",
                                "echo Type 'help' for available commands\n",
                                "echo.\n"
                            ]
                        for cmd in initial_commands:
                            process.stdin.write(cmd)
                            process.stdin.flush()
                            time.sleep(0.1)
                except Exception as e:
                    print(f"发送初始命令失败: {e}")
            
            # 启动初始命令线程
            init_thread = threading.Thread(target=send_initial_commands, daemon=True)
            init_thread.start()
            
            read_thread = threading.Thread(target=read_from_process, daemon=True)
            read_thread.start()
            
        else:
            # Unix/Linux/macOS系统使用pty
            try:
                import pty
                import select
                import subprocess
                import shutil
                
                # 创建伪终端
                master, slave = pty.openpty()
                
                # 检测系统类型和可用shell
                shell_path = None
                if system == "Darwin":  # macOS
                    if shutil.which("zsh"):
                        shell_path = "zsh"
                    elif shutil.which("bash"):
                        shell_path = "bash"
                    else:
                        shell_path = "/bin/sh"
                else:  # Linux
                    if shutil.which("bash"):
                        shell_path = "bash"
                    elif shutil.which("zsh"):
                        shell_path = "zsh"
                    else:
                        shell_path = "/bin/sh"
                
                # 设置环境变量
                env = os.environ.copy()
                env.update({
                    "TERM": "xterm-256color",
                    "LANG": "en_US.UTF-8",
                    "LC_ALL": "en_US.UTF-8",
                    "LC_CTYPE": "en_US.UTF-8",
                    "LC_MESSAGES": "en_US.UTF-8",
                    "LC_MONETARY": "en_US.UTF-8",
                    "LC_NUMERIC": "en_US.UTF-8",
                    "LC_TIME": "en_US.UTF-8",
                    "LC_COLLATE": "en_US.UTF-8",
                })
                
                # 根据shell类型设置特定环境变量
                if shell_path == "zsh":
                    env.update({
                        "BASH_ENV": "",
                        "ENV": "",
                        "ZDOTDIR": "",
                        "ZSHRC": "",
                        "PROMPT": "%n@%m:%1~$ ",
                        "RPROMPT": "",
                        "PS1": "%n@%m:%1~$ ",
                    })
                elif shell_path == "bash":
                    env.update({
                        "BASH_ENV": "",
                        "ENV": "",
                        "PS1": "\\u@\\h:\\w$ ",
                    })
                
                # 启动shell进程
                process = subprocess.Popen(
                    [shell_path],
                    stdin=slave,
                    stdout=slave,
                    stderr=slave,
                    start_new_session=True,
                    env=env
                )
                
                # Unix系统使用PTY读取
                def read_from_pty():
                    try:
                        while process.poll() is None:
                            ready, _, _ = select.select([master], [], [], 0.1)
                            if ready:
                                data = os.read(master, 1024)
                                if data:
                                    try:
                                        decoded_data = data.decode('utf-8', errors='replace')
                                        cleaned_data = clean_pty_output(decoded_data)
                                        data_queue.put(cleaned_data)
                                    except Exception as decode_error:
                                        print(f"解码PTY输出失败: {decode_error}")
                                        try:
                                            decoded_data = data.decode('latin-1', errors='replace')
                                            cleaned_data = clean_pty_output(decoded_data)
                                            data_queue.put(cleaned_data)
                                        except:
                                            pass
                    except Exception as e:
                        print(f"PTY读取错误: {e}")
                
                read_thread = threading.Thread(target=read_from_pty, daemon=True)
                read_thread.start()
                
            except ImportError as e:
                # 如果pty不可用，返回错误
                await websocket.send_text(json.dumps({
                    "type": "error",
                    "message": f"终端创建失败: {str(e)}"
                }))
                return
        
        # 存储会话信息
        active_terminals[session_id] = {
            "websocket": websocket,
            "process": process,
            "master": master,
            "slave": slave,
            "created_at": time.time()
        }
        
        # 发送连接成功消息
        await websocket.send_text(json.dumps({
            "type": "connection",
            "status": "connected",
            "session_id": session_id,
            "system": system
        }))
        
        # 主循环：处理WebSocket消息和进程输出
        while True:
            try:
                # 检查进程输出
                try:
                    while not data_queue.empty():
                        output_data = data_queue.get_nowait()
                        await websocket.send_text(json.dumps({
                            "type": "output",
                            "data": output_data
                        }))
                except queue.Empty:
                    pass
                
                # 处理WebSocket消息
                try:
                    data = await asyncio.wait_for(websocket.receive_text(), timeout=0.1)
                    message = json.loads(data)
                    
                    if message.get("type") == "input":
                        command = message.get("command", "")
                        if command and process and process.poll() is None:
                            try:
                                processed_command = process_terminal_input(command)
                                if system == "Windows":
                                    # Windows系统直接写入进程
                                    process.stdin.write(processed_command)
                                    process.stdin.flush()
                                    
                                    # 对于Windows系统，手动回显用户输入
                                    if command.strip() and not command.startswith('\x1B'):  # 不是控制字符
                                        echo_data = f"{command}"
                                        await websocket.send_text(json.dumps({
                                            "type": "output",
                                            "data": echo_data
                                        }))
                                else:
                                    # Unix系统写入PTY
                                    os.write(master, processed_command.encode('utf-8', errors='ignore'))
                            except Exception as write_error:
                                print(f"写入进程失败: {write_error}")
                                
                except asyncio.TimeoutError:
                    pass
                except Exception as e:
                    print(f"处理WebSocket消息错误: {e}")
                    break
                    
            except Exception as e:
                print(f"WebSocket循环错误: {e}")
                break
        
    except Exception as e:
        print(f"终端会话错误: {e}")
        await websocket.send_text(json.dumps({
            "type": "error",
            "message": f"终端会话错误: {str(e)}"
        }))
    finally:
        # 清理资源
        if session_id in active_terminals:
            terminal_info = active_terminals[session_id]
            if terminal_info.get("process"):
                try:
                    terminal_info["process"].terminate()
                    terminal_info["process"].wait(timeout=5)
                except:
                    try:
                        terminal_info["process"].kill()
                    except:
                        pass
            if terminal_info.get("master"):
                try:
                    os.close(terminal_info["master"])
                except:
                    pass
            if terminal_info.get("slave"):
                try:
                    os.close(terminal_info["slave"])
                except:
                    pass
            del active_terminals[session_id]

@app.get("/api/terminal/sessions")
async def get_terminal_sessions() -> Dict[str, Any]:
    """获取活跃的终端会话列表"""
    sessions = []
    for session_id, session_info in active_terminals.items():
        sessions.append({
            "session_id": session_id,
            "created_at": session_info["created_at"],
            "uptime": time.time() - session_info["created_at"],
            "system": platform.system()
        })
    
    return {
        "active_sessions": len(sessions),
        "sessions": sessions
    }

@app.post("/api/terminal/sessions/{session_id}/create")
async def create_terminal_session(session_id: str) -> Dict[str, Any]:
    """创建新的终端会话"""
    try:
        import pty
        import subprocess
        import platform
        
        # 创建伪终端
        master, slave = pty.openpty()
        
        # 启动shell进程
        system = platform.system()
        if system == "Windows":
            # Windows使用cmd
            process = subprocess.Popen(
                ["cmd.exe"],
                stdin=slave,
                stdout=slave,
                stderr=slave,
                start_new_session=True
            )
        else:
            # 检测并使用合适的shell
            import shutil
            shell_path = None
            
            # 优先使用zsh（macOS默认）
            if shutil.which("zsh"):
                shell_path = "zsh"
            elif shutil.which("bash"):
                shell_path = "bash"
            else:
                shell_path = "/bin/sh"
            
            # 设置环境变量以避免配置文件中的错误
            env = os.environ.copy()
            env.update({
                "TERM": "xterm-256color",
                "LANG": "en_US.UTF-8",
                "LC_ALL": "en_US.UTF-8",
                # 禁用可能导致错误的shell配置
                "BASH_ENV": "",
                "ENV": "",
                "ZDOTDIR": "",
                "ZSHRC": "",
                # 禁用zsh的复杂提示符
                "PROMPT": "%n@%m:%1~$ ",
                "RPROMPT": "",
                "PS1": "%n@%m:%1~$ ",
                "PS2": "> ",
                "PS3": "? ",
                "PS4": "+ "
            })
            
            # 启动shell进程，使用非交互模式避免配置文件问题
            process = subprocess.Popen(
                [shell_path, "--no-rcs"],
                stdin=slave,
                stdout=slave,
                stderr=slave,
                start_new_session=True,
                env=env
            )
        
        # 存储会话信息
        active_terminals[session_id] = {
            "process": process,
            "master": master,
            "slave": slave,
            "created_at": time.time()
        }
        
        return {
            "success": True,
            "session_id": session_id,
            "message": "会话创建成功"
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"创建会话失败: {str(e)}")

@app.post("/api/terminal/sessions/{session_id}/terminate")
async def terminate_terminal_session(session_id: str) -> Dict[str, Any]:
    """终止终端会话"""
    try:
        if session_id not in active_terminals:
            raise HTTPException(status_code=404, detail="会话不存在")
        
        session_info = active_terminals[session_id]
        
        # 终止进程
        try:
            session_info["process"].terminate()
            session_info["process"].wait(timeout=5)
        except:
            session_info["process"].kill()
        
        # 关闭文件描述符
        try:
            os.close(session_info["master"])
            os.close(session_info["slave"])
        except:
            pass
        
        # 从活跃会话中移除
        del active_terminals[session_id]
        
        return {
            "success": True,
            "message": "会话已终止"
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"终止会话失败: {str(e)}")

@app.post("/api/terminal/execute")
async def execute_terminal_command(command_data: TerminalCommand) -> Dict[str, Any]:
    """执行终端命令（HTTP API版本，用于兼容性）"""
    import subprocess
    import platform
    import shlex
    
    try:
        command = command_data.command.strip()
        if not command:
            raise HTTPException(status_code=400, detail="命令不能为空")
        
        # 安全检查：禁止执行危险命令
        dangerous_commands = [
            'rm -rf', 'dd', 'mkfs', 'fdisk', 'format', 'del /s', 'rd /s',
            'shutdown', 'halt', 'reboot', 'init', 'systemctl', 'service',
            'sudo', 'su', 'passwd', 'chmod 777', 'chown root'
        ]
        
        command_lower = command.lower()
        for dangerous in dangerous_commands:
            if dangerous in command_lower:
                raise HTTPException(status_code=400, detail=f"禁止执行危险命令: {dangerous}")
        
        # 根据操作系统选择合适的shell
        system = platform.system()
        if system == "Windows":
            # Windows使用cmd
            shell_cmd = ["cmd", "/c", command]
        else:
            # Linux/macOS使用bash
            shell_cmd = ["/bin/bash", "-c", command]
        
        # 执行命令
        process = subprocess.Popen(
            shell_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True
        )
        
        try:
            stdout, stderr = process.communicate(timeout=30)  # 30秒超时
        except subprocess.TimeoutExpired:
            process.kill()
            raise subprocess.TimeoutExpired(command, 30)
        
        return {
            "success": process.returncode == 0,
            "output": stdout,
            "error": stderr if stderr else None,
            "return_code": process.returncode,
            "command": command
        }
        
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=408, detail="命令执行超时")
    except subprocess.SubprocessError as e:
        raise HTTPException(status_code=500, detail=f"命令执行失败: {str(e)}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"执行命令时发生错误: {str(e)}")

@app.get("/api/terminal/system-info")
async def get_terminal_system_info() -> Dict[str, Any]:
    """获取终端系统信息"""
    import platform
    import psutil
    
    try:
        system_info = {
            "platform": platform.system(),
            "platform_version": platform.version(),
            "machine": platform.machine(),
            "processor": platform.processor(),
            "python_version": platform.python_version(),
            "cpu_count": psutil.cpu_count(),
            "memory_total": psutil.virtual_memory().total,
            "disk_partitions": []
        }
        
        # 获取磁盘分区信息
        for partition in psutil.disk_partitions():
            try:
                usage = psutil.disk_usage(partition.mountpoint)
                system_info["disk_partitions"].append({
                    "device": partition.device,
                    "mountpoint": partition.mountpoint,
                    "fstype": partition.fstype,
                    "total": usage.total,
                    "used": usage.used,
                    "free": usage.free,
                    "percent": usage.percent
                })
            except PermissionError:
                continue
        
        return system_info
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"获取系统信息失败: {str(e)}")

# 单个任务操作API - 必须在批量操作之后定义
@app.post("/api/tasks/{task_id}/run")
async def run_task(task_id: str) -> JSONResponse:
    """立即运行任务"""
    if not task_service:
        raise HTTPException(status_code=500, detail="任务服务未初始化")
    
    try:
        await task_service.run_task_now(task_id)
        return JSONResponse({"message": f"任务 {task_id} 已启动"})
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.post("/api/tasks/{task_id}/restart")
async def restart_task(task_id: str) -> JSONResponse:
    """重启任务"""
    if not task_service:
        raise HTTPException(status_code=500, detail="任务服务未初始化")
    
    try:
        await task_service.restart_task(task_id)
        return JSONResponse({"message": f"任务 {task_id} 已重启"})
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.post("/api/tasks/{task_id}/toggle")
async def toggle_task(task_id: str) -> JSONResponse:
    """切换任务启用/禁用状态"""
    if not task_service:
        raise HTTPException(status_code=500, detail="任务服务未初始化")
    
    try:
        task = await task_store.get_task(task_id)
        if not task:
            raise HTTPException(status_code=404, detail="任务不存在")
        
        # 切换启用状态
        new_enabled = not task.get("enabled", True)
        await task_service.toggle_task(task_id, new_enabled)
        
        status = "启用" if new_enabled else "禁用"
        return JSONResponse({"message": f"任务 {task_id} 已{status}"})
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.post("/api/tasks/{task_id}/kill")
async def kill_task(task_id: str) -> JSONResponse:
    """强制停止任务进程"""
    if not task_service:
        raise HTTPException(status_code=500, detail="任务服务未初始化")
    
    try:
        # 检查任务是否存在
        task = await task_store.get_task(task_id)
        if not task:
            raise HTTPException(status_code=404, detail="任务不存在")
        
        # 获取任务状态
        status = task_status.get(task_id, {})
        if status.get("status") != "RUNNING":
            raise HTTPException(status_code=400, detail="任务当前未在运行")
        
        # 获取进程ID
        process_id = status.get("process_id")
        if not process_id:
            raise HTTPException(status_code=400, detail="无法获取任务进程ID")
        
        # 尝试终止进程
        try:
            import psutil
            process = psutil.Process(process_id)
            process.terminate()  # 先尝试优雅终止
            
            # 等待进程结束
            try:
                process.wait(timeout=5)
            except psutil.TimeoutExpired:
                # 如果超时，强制杀死进程
                process.kill()
                process.wait()
            
            # 更新任务状态
            task_status[task_id] = {
                "status": "STOPPED",
                "last_stop": time.time(),
                "process_id": None
            }
            
            return JSONResponse({"message": f"任务 {task_id} 已停止"})
            
        except psutil.NoSuchProcess:
            # 进程已经不存在
            task_status[task_id] = {
                "status": "STOPPED",
                "last_stop": time.time(),
                "process_id": None
            }
            return JSONResponse({"message": f"任务 {task_id} 进程已结束"})
            
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"停止任务失败: {str(e)}")
            
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.get("/api/tasks/{task_id}/status")
async def get_task_status(task_id: str) -> Dict[str, Any]:
    """获取任务详细状态"""
    if not task_store:
        raise HTTPException(status_code=500, detail="任务存储未初始化")
    
    task = await task_store.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    
    status = task_status.get(task_id, {"status": "DISABLED"})
    
    return {
        "id": task_id,
        "enabled": task.get("enabled", True),
        "status": status.get("status", "DISABLED"),
        "last_success": status.get("last_success"),
        "last_error": status.get("last_error"),
        "next_run": status.get("next_run"),
        "run_count": status.get("run_count", 0),
        "error_count": status.get("error_count", 0),
        "process_id": status.get("process_id")
    }

@app.get("/api/tasks/{task_id}/history")
async def get_task_history(task_id: str, limit: int = 50) -> List[Dict[str, Any]]:
    """获取任务执行历史"""
    if not task_store:
        raise HTTPException(status_code=500, detail="任务存储未初始化")
    
    try:
        history = await task_store.get_task_history(task_id, limit)
        return history
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.get("/api/tasks/exceptions")
async def get_all_exceptions(limit: int = 50) -> List[Dict[str, Any]]:
    """获取所有任务异常日志"""
    try:
        from services.log_manager import get_log_manager
        log_manager = get_log_manager()
        exceptions = log_manager.get_all_exceptions(limit)
        return exceptions
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"获取异常日志失败: {str(e)}")

@app.get("/api/tasks/{task_id}/exceptions")
async def get_task_exceptions(task_id: str, limit: int = 10) -> List[Dict[str, Any]]:
    """获取任务异常日志"""
    try:
        from services.log_manager import get_log_manager
        log_manager = get_log_manager()
        exceptions = log_manager.get_task_exceptions(task_id, limit)
        return exceptions
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"获取异常日志失败: {str(e)}")

@app.delete("/api/tasks/exceptions")
async def clear_all_exceptions() -> JSONResponse:
    """清空所有异常日志"""
    try:
        from services.log_manager import get_log_manager
        log_manager = get_log_manager()
        log_manager.clear_all_exceptions()
        return JSONResponse({"message": "所有异常日志已清空"})
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"清空异常日志失败: {str(e)}")

@app.delete("/api/tasks/{task_id}/exceptions")
async def clear_task_exceptions(task_id: str) -> JSONResponse:
    """清空任务异常日志"""
    try:
        from services.log_manager import get_log_manager
        log_manager = get_log_manager()
        log_manager.clear_task_exceptions(task_id)
        return JSONResponse({"message": f"任务 {task_id} 的异常日志已清空"})
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"清空异常日志失败: {str(e)}")

@app.get("/api/tasks/{task_id}")
async def get_task(task_id: str) -> TaskResponse:
    """获取单个任务"""
    if not task_store:
        raise HTTPException(status_code=500, detail="任务存储未初始化")
    
    task = await task_store.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    
    status = task_status.get(task_id, {"status": "DISABLED"})
    # 确保enabled字段与status字段一致
    task_enabled = task.get("enabled", True)
    task_status_value = status.get("status", "DISABLED")
    
    # 如果任务被禁用，状态应该是DISABLED
    if not task_enabled:
        task_status_value = "DISABLED"
    
    return TaskResponse(
        id=task["id"],
        script_path=task["script_path"],
        interval_minutes=task["interval_minutes"],
        execute_path=task.get("execute_path"),
        enabled=task_enabled,
        status=task_status_value,
        last_success=status.get("last_success"),
        last_error=status.get("last_error"),
        last_run=status.get("last_run"),
        duration=status.get("duration"),
        output=status.get("output", [])
    )

@app.get("/api/logs")
async def get_logs(limit: int = 100) -> List[str]:
    """获取日志"""
    return log_buffer[-limit:] if log_buffer else []

@app.delete("/api/logs")
async def clear_logs() -> JSONResponse:
    """清空日志"""
    log_buffer.clear()
    return JSONResponse({"message": "日志已清空"})

@app.post("/api/export/logs")
async def export_logs(format: str = "json") -> JSONResponse:
    """导出日志"""
    if not export_service:
        raise HTTPException(status_code=500, detail="导出服务未初始化")
    
    try:
        filepath = export_service.export_logs(log_buffer, format)
        return JSONResponse({
            "message": "日志导出成功",
            "filepath": filepath
        })
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"导出失败: {str(e)}")

@app.post("/api/export/tasks")
async def export_tasks(format: str = "json") -> JSONResponse:
    """导出任务配置"""
    if not export_service or not task_store:
        raise HTTPException(status_code=500, detail="服务未初始化")
    
    try:
        tasks = await task_store.get_all_tasks()
        filepath = export_service.export_tasks(tasks, format)
        return JSONResponse({
            "message": "任务配置导出成功",
            "filepath": filepath
        })
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"导出失败: {str(e)}")

@app.post("/api/export/all")
async def export_all() -> JSONResponse:
    """导出所有数据"""
    if not export_service or not task_store:
        raise HTTPException(status_code=500, detail="服务未初始化")
    
    try:
        tasks = await task_store.get_all_tasks()
        status = {
            "total_tasks": len(task_status),
            "running_tasks": sum(1 for s in task_status.values() if s.get("status") == "RUNNING"),
            "failed_tasks": sum(1 for s in task_status.values() if s.get("status") == "FAILED"),
            "disabled_tasks": sum(1 for s in task_status.values() if s.get("status") == "DISABLED"),
            "log_count": len(log_buffer)
        }
        
        filepath = export_service.export_all(log_buffer, tasks, status)
        return JSONResponse({
            "message": "数据导出成功",
            "filepath": filepath
        })
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"导出失败: {str(e)}")

@app.post("/api/export/history")
async def export_task_history() -> JSONResponse:
    """导出任务执行历史"""
    try:
        if not export_service or not task_store:
            raise HTTPException(status_code=500, detail="导出服务或任务存储未初始化")
        
        # 获取所有任务
        tasks = await task_store.get_all_tasks()
        
        # 获取每个任务的历史记录
        all_history = []
        for task in tasks:
            task_id = task["id"]
            try:
                history = await task_store.get_task_history(task_id, limit=100)
                all_history.append({
                    "task_id": task_id,
                    "task_name": task.get("script_path", ""),
                    "history": history
                })
            except Exception as e:
                print(f"获取任务 {task_id} 历史记录失败: {e}")
                all_history.append({
                    "task_id": task_id,
                    "task_name": task.get("script_path", ""),
                    "history": []
                })
        
        # 创建历史记录数据
        history_data = {
            "export_time": datetime.now().isoformat(),
            "total_tasks": len(tasks),
            "task_history": all_history
        }
        
        # 导出为JSON文件
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"task_history_{timestamp}.json"
        filepath = os.path.join(CONFIG["export_path"], filename)
        
        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump(history_data, f, ensure_ascii=False, indent=2)
        
        return JSONResponse({
            "message": "任务执行历史导出成功",
            "filepath": filepath
        })
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"导出历史记录失败: {str(e)}")

@app.get("/api/monitoring/metrics")
async def get_monitoring_metrics(limit: int = 100) -> Dict[str, Any]:
    """获取监控指标"""
    if not monitoring_service:
        raise HTTPException(status_code=500, detail="监控服务未初始化")
    
    try:
        current_metrics = monitoring_service.get_current_metrics()
        system_info = monitoring_service.get_system_info()
        
        # 根据操作系统获取不同的监控指标
        import platform
        
        # 获取磁盘使用率
        disk_usage = "0%"
        try:
            import psutil
            import platform
            if platform.system() == "Darwin":
                # macOS: 使用 /System/Volumes/Data 获取用户数据卷的使用情况
                try:
                    # 尝试获取用户数据卷的使用情况
                    data_usage = psutil.disk_usage('/System/Volumes/Data')
                    disk_usage = f"{data_usage.percent:.1f}%"
                except:
                    try:
                        # 备用方案：使用根目录
                        root_usage = psutil.disk_usage('/')
                        disk_usage = f"{root_usage.percent:.1f}%"
                    except:
                        # 如果都失败，尝试汇总所有可写挂载点
                        total, used = 0, 0
                        for part in psutil.disk_partitions(all=False):
                            if part.fstype and "rw" in part.opts:
                                try:
                                    usage = psutil.disk_usage(part.mountpoint)
                                    total += usage.total
                                    used += usage.used
                                except Exception:
                                    continue
                        if total > 0:
                            percent = used / total * 100
                            disk_usage = f"{percent:.1f}%"
                        else:
                            disk_usage = "0%"
            else:
                # 其他系统保持原有实现
                if platform.system() == "Windows":
                    disk = psutil.disk_usage('C:\\')
                else:
                    disk = psutil.disk_usage('/')
                disk_usage = f"{disk.percent:.1f}%"
        except Exception as e:
            print(f"获取磁盘使用率失败: {e}")
            disk_usage = "0%"
        
        # 获取负载平均值（仅Linux和macOS支持）
        load_average = "0.00"
        try:
            import psutil
            if platform.system() in ["Linux", "Darwin"]:
                load = psutil.getloadavg()
                load_average = f"{load[0]:.2f}"
            else:
                # Windows不支持load average，使用CPU使用率代替
                cpu_percent = psutil.cpu_percent(interval=1)
                load_average = f"{cpu_percent:.2f}"
        except:
            pass
        
        # 获取进程数量
        process_count = "0"
        try:
            import psutil
            process_count = str(len(psutil.pids()))
        except:
            pass
        
        # 添加单位转换函数
        def format_bytes(bytes_value):
            """格式化字节数"""
            if bytes_value < 1024:
                return f"{bytes_value} B"
            elif bytes_value < 1024**2:
                return f"{bytes_value/1024:.1f} KB"
            elif bytes_value < 1024**3:
                return f"{bytes_value/(1024**2):.1f} MB"
            else:
                return f"{bytes_value/(1024**3):.1f} GB"
        
        def format_duration(seconds):
            """格式化时长"""
            if seconds < 60:
                return f"{seconds:.0f}秒"
            elif seconds < 3600:
                return f"{seconds/60:.0f}分钟"
            elif seconds < 86400:
                return f"{seconds/3600:.1f}小时"
            else:
                return f"{seconds/86400:.1f}天"
        
        # 从监控服务获取CPU和内存数据
        cpu_data = {}
        memory_data = {}
        
        if current_metrics:
            # CPU数据
            if "cpu_percent" in current_metrics:
                cpu_data = {
                    "percent": current_metrics["cpu_percent"],
                    "cores": system_info.get("cpu_count", 0),
                    "freq": system_info.get("cpu_freq", {}).get("current", 0)
                }
            
            # 内存数据
            if "memory_percent" in current_metrics:
                memory_data = {
                    "percent": current_metrics["memory_percent"],
                    "used": format_bytes(current_metrics.get("memory_used", 0)),
                    "total": format_bytes(current_metrics.get("memory_total", 0)),
                    "used_gb": current_metrics.get("memory_used_gb", 0),
                    "total_gb": current_metrics.get("memory_total_gb", 0)
                }
        
        # 格式化运行时长
        uptime_formatted = format_duration(system_info.get("uptime", 0))
        
        # 获取磁盘IO数据
        disk_io_data = {}
        if current_metrics and "disk_io" in current_metrics:
            disk_io_data = current_metrics["disk_io"]
        
        return {
            "disk_usage": disk_usage,
            "load_average": load_average,
            "process_count": process_count,
            "cpu": cpu_data,
            "memory": memory_data,
            "disk_io": disk_io_data,
            "system_info": {
                **system_info,
                "uptime_formatted": uptime_formatted
            }
        }
    except Exception as e:
        print(f"获取监控指标失败: {e}")
        return {
            "disk_usage": "0%",
            "network_io": "0 KB/s", 
            "load_average": "0.00",
            "process_count": "0",
            "cpu": {},
            "memory": {},
            "system_info": {}
        }

# ==================== WebSocket 支持 ====================

@app.websocket("/ws/logs")
async def websocket_logs(websocket: WebSocket):
    """WebSocket 连接处理 - 异步优化版本"""
    await websocket.accept()
    websocket_connections.append(websocket)
    
    try:
        # 发送初始状态
        await websocket.send_json({
            "type": "status",
            "data": task_status
        })
        
        # 发送历史日志 - 异步发送
        history_logs = log_buffer[-50:]
        for log in history_logs:
            try:
                await websocket.send_json({
                    "type": "log",
                    "message": log
                })
            except Exception:
                break
        
        # 保持连接 - 使用异步心跳
        while True:
            try:
                # 使用超时接收，避免阻塞
                data = await asyncio.wait_for(
                    websocket.receive_text(), 
                    timeout=30.0
                )
                # 可以处理客户端发送的消息
                if data == "ping":
                    await websocket.send_json({"type": "pong"})
            except asyncio.TimeoutError:
                # 发送心跳保持连接
                await websocket.send_json({"type": "heartbeat"})
            except WebSocketDisconnect:
                break
            
    except WebSocketDisconnect:
        pass
    except Exception as e:
        print(f"WebSocket 错误: {e}")
    finally:
        # 确保连接被移除
        if websocket in websocket_connections:
            websocket_connections.remove(websocket)

# ==================== 系统管理 API ====================

@app.get("/api/system/info")
async def get_system_info() -> Dict[str, Any]:
    """获取系统信息"""
    # 获取任务统计
    tasks = await task_store.get_all_tasks() if task_store else []
    total_tasks = len(tasks)
    running_tasks = sum(1 for task in tasks if task.get('enabled', True))
    failed_tasks = sum(1 for s in task_status.values() if s.get("status") == "FAILED")
    
    # 获取存储信息
    storage_info = {
        "type": CONFIG["storage_type"],
        "config": {}
    }
    
    if CONFIG["storage_type"] == "redis":
        storage_info["config"] = {
            "url": CONFIG["redis_url"],
            "db": CONFIG["redis_db"]
        }
    elif CONFIG["storage_type"] == "sqlite":
        storage_info["config"] = {
            "database": CONFIG["sqlite_db"]
        }
    else:
        storage_info["config"] = {
            "file": CONFIG["json_file"]
        }
    
    info = {
        "version": "1.0.0",
        "storage_type": CONFIG["storage_type"],
        "storage_info": storage_info,
        "start_time": datetime.now().isoformat(),
        "log_count": len(log_buffer),
        "active_connections": len(websocket_connections),
        "total_tasks": total_tasks,
        "running_tasks": running_tasks,
        "failed_tasks": failed_tasks
    }
    
    # 添加系统资源信息
    if monitoring_service:
        metrics = monitoring_service.get_current_metrics()
        system_info = monitoring_service.get_system_info()
        
        # 单位转换函数
        def format_duration(seconds):
            """格式化时长"""
            if seconds < 60:
                return f"{seconds:.0f}秒"
            elif seconds < 3600:
                return f"{seconds/60:.0f}分钟"
            elif seconds < 86400:
                return f"{seconds/3600:.1f}小时"
            else:
                return f"{seconds/86400:.1f}天"
        
        # 更新 CPU 和内存信息
        if metrics:
            if "cpu_percent" in metrics:
                info["cpu"] = {
                    "percent": metrics["cpu_percent"],
                    "cores": system_info.get("cpu_count", 0),
                    "freq": system_info.get("cpu_freq", {}).get("current", 0)
                }
            if "memory_percent" in metrics:
                info["memory"] = {
                    "percent": metrics["memory_percent"],
                    "used_gb": metrics.get("memory_used_gb", 0),
                    "total_gb": metrics.get("memory_total_gb", 0)
                }
        
        # 更新系统基本信息
        if "system_name" in system_info:
            info["system_name"] = system_info["system_name"]
        if "system_version" in system_info:
            info["system_version"] = system_info["system_version"]
        if "uptime" in system_info:
            info["uptime"] = system_info["uptime"]
            info["uptime_formatted"] = format_duration(system_info["uptime"])
        # 移除runtime，避免与uptime冲突
        # if "runtime" in system_info:
        #     info["runtime"] = system_info["runtime"]
    
    # 更新配置文件信息
    if CONFIG["storage_type"] == "json":
        info["config_file"] = CONFIG["json_file"]
    elif CONFIG["storage_type"] == "sqlite":
        info["config_file"] = CONFIG["sqlite_db"]
    else:
        info["config_file"] = CONFIG["redis_url"]
    
    # 添加应用启动时间
    if hasattr(app.state, 'start_time'):
        info["app_start_time"] = app.state.start_time.isoformat()
    else:
        info["app_start_time"] = datetime.now().isoformat()
    
    # 添加WebSocket连接数
    info["websocket_connections"] = len(websocket_connections)
    
    # 添加任务状态统计
    task_status_stats = {
        "running": 0,
        "stopped": 0,
        "failed": 0,
        "unknown": 0
    }
    
    for status in task_status.values():
        status_type = status.get("status", "unknown")
        if status_type in task_status_stats:
            task_status_stats[status_type] += 1
        else:
            task_status_stats["unknown"] += 1
    
    info["task_status_stats"] = task_status_stats
    
    return info

# 系统设置相关API
@app.get("/api/settings/config")
async def get_system_config() -> Dict[str, Any]:
    """获取系统配置"""
    try:
        # 从配置存储获取所有配置
        all_config = await get_all_config()
        
        # 构建存储配置
        storage_config = {
            "type": all_config.get("storage_type", "json"),
            "config": {}
        }
        
        storage_type = all_config.get("storage_type", "json")
        if storage_type == "redis":
            storage_config["config"] = {
                "redis_url": all_config.get("redis_url", "redis://localhost:6379"),
                "redis_db": all_config.get("redis_db", 0)
            }
        elif storage_type == "sqlite":
            storage_config["config"] = {
                "sqlite_db": all_config.get("sqlite_db", "tasks.db")
            }
        else:
            storage_config["config"] = {
                "json_file": all_config.get("json_file", "tasks.json"),
                "jobs_directory": all_config.get("jobs_directory"),
                "task_file_prefix": all_config.get("task_file_prefix", "task")
            }
        
        config = {
            "storage": storage_config,
            "logging": {
                "max_lines": all_config.get("log_limit", 500),
                "log_level": "INFO"
            },
            "monitoring": {
                "refresh_interval": all_config.get("monitoring_interval", 60),
                "enabled": all_config.get("enable_monitoring", True)
            },
            "tasks": {
                "max_concurrent": 5,
                "default_interval": 10
            },
            "system": {
                "host": all_config.get("host", "0.0.0.0"),
                "port": all_config.get("port", 8100),
                "check_interval": all_config.get("check_interval", 30)
            },
            "export": {
                "export_path": all_config.get("export_path", "./exports"),
                "max_export_size": all_config.get("max_export_size", 10485760)
            },
            "dingtalk": {
                "enable_dingtalk_alert": all_config.get("enable_dingtalk_alert", False),
                "dingtalk_access_token": all_config.get("dingtalk_access_token", ""),
                "dingtalk_url": all_config.get("dingtalk_url", "https://oapi.dingtalk.com/robot/send?access_token=")
            },
            "system_monitor": {
                "enable_system_monitor": all_config.get("enable_system_monitor", False),
                "cpu_threshold": all_config.get("cpu_threshold", 89.0),
                "memory_threshold": all_config.get("memory_threshold", 90.0),
                "disk_threshold": all_config.get("disk_threshold", 90.0),
                "monitor_check_interval": all_config.get("monitor_check_interval", 60),
                "alert_cooldown_interval": all_config.get("alert_cooldown_interval", 300)
            }
        }
        return config
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"获取配置失败: {str(e)}")

@app.put("/api/settings/config")
async def update_system_config(config: Dict[str, Any]) -> JSONResponse:
    """更新系统配置"""
    try:
        # 准备要更新的配置
        config_to_update = {}
        
        # 处理嵌套配置
        for section, section_config in config.items():
            if isinstance(section_config, dict):
                for key, value in section_config.items():
                    config_key = f"{section}_{key}" if section != "storage" else key
                    config_to_update[config_key] = value
            else:
                config_to_update[section] = section_config
        
        # 更新到配置存储
        if config_to_update:
            await update_config(config_to_update)
            
            # 更新全局配置
            global CONFIG
            CONFIG.update(config_to_update)
            
            print(f"✅ 配置已更新: {list(config_to_update.keys())}")
        
        # 如果存储类型发生变化，需要重新初始化存储
        if "storage_type" in config_to_update:
            await reinitialize_storage()
        
        return JSONResponse({"message": "系统配置已更新"})
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"更新配置失败: {str(e)}")

async def reinitialize_storage():
    """重新初始化存储"""
    global task_store, task_service
    
    print("🔄 重新初始化存储...")
    
    # 停止当前任务服务
    if task_service:
        await task_service.stop()
    
    # 关闭当前存储连接
    if hasattr(task_store, 'redis_client') and task_store.redis_client:
        await task_store.redis_client.close()
    
    # 重新初始化存储
    if CONFIG["storage_type"] == "redis":
        redis_client = redis.from_url(CONFIG["redis_url"], db=CONFIG["redis_db"])
        await redis_client.ping()
        task_store = RedisTaskStore(redis_client)
        print(f"✅ Redis 连接重新初始化: {CONFIG['redis_url']}")
    elif CONFIG["storage_type"] == "sqlite":
        task_store = SQLiteTaskStore(CONFIG["sqlite_db"])
        print(f"✅ SQLite 存储重新初始化: {CONFIG['sqlite_db']}")
    else:
        # JSON存储支持jobs目录
        if CONFIG.get("jobs_directory"):
            # 确保jobs目录存在
            os.makedirs(CONFIG["jobs_directory"], exist_ok=True)
            task_file = os.path.join(CONFIG["jobs_directory"], CONFIG["json_file"])
        else:
            task_file = CONFIG["json_file"]
        
        task_store = JSONTaskStore(
            task_file, 
            jobs_directory=CONFIG.get("jobs_directory"),
            task_file_prefix=CONFIG.get("task_file_prefix", "task")
        )
        print(f"✅ JSON 存储重新初始化: {task_file}")
    
    # 重新初始化任务服务
    task_service = TaskService(task_store, log_buffer, task_status, websocket_connections)
    await task_service.start()
    
    print("✅ 存储重新初始化完成")

@app.post("/api/settings/clear-logs")
async def clear_system_logs() -> JSONResponse:
    """清空系统日志"""
    try:
        global log_buffer
        log_buffer.clear()
        return JSONResponse({"message": "系统日志已清空"})
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"清空日志失败: {str(e)}")

@app.post("/api/settings/export-config")
async def export_system_config() -> JSONResponse:
    """导出系统配置"""
    try:
        if not export_service:
            raise HTTPException(status_code=500, detail="导出服务未初始化")
        
        # 获取当前配置
        config = {
            "tasks": await task_store.get_all_tasks() if task_store else [],
            "system": {
                "storage_type": "json",
                "log_buffer_size": len(log_buffer),
                "task_status": task_status
            }
        }
        
        print(f"准备导出配置: {config}")
        filepath = export_service.export_config(config, "json")
        print(f"配置导出成功: {filepath}")
        
        return JSONResponse({
            "message": "系统配置导出成功",
            "filepath": filepath
        })
    except Exception as e:
        print(f"导出配置异常: {e}")
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"导出配置失败: {str(e)}")

@app.post("/api/settings/import-config")
async def import_system_config(config_data: Dict[str, Any]) -> JSONResponse:
    """导入系统配置"""
    try:
        if not task_store:
            raise HTTPException(status_code=500, detail="任务存储未初始化")
        
        # 导入任务配置
        if "tasks" in config_data:
            tasks = config_data["tasks"]
            # 清空现有任务
            all_tasks = await task_store.get_all_tasks()
            for task in all_tasks:
                await task_store.delete_task(task["id"])
            
            # 导入新任务
            for task in tasks:
                await task_store.add_task(task)
        
        # 重新加载任务
        if task_service:
            await task_service.update_jobs()
        
        return JSONResponse({"message": "系统配置导入成功"})
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"导入配置失败: {str(e)}")

@app.get("/api/settings/storage-info")
async def get_storage_info() -> Dict[str, Any]:
    """获取存储信息"""
    try:
        if not task_store:
            raise HTTPException(status_code=500, detail="任务存储未初始化")
        
        tasks = await task_store.get_all_tasks()
        
        # 计算存储大小
        size_bytes = 0
        status = "healthy"
        
        if CONFIG["storage_type"] == "json":
            # JSON文件存储
            if CONFIG.get("jobs_directory") and os.path.exists(CONFIG["jobs_directory"]):
                try:
                    for filename in os.listdir(CONFIG["jobs_directory"]):
                        if filename.endswith('.json'):
                            file_path = os.path.join(CONFIG["jobs_directory"], filename)
                            if os.path.exists(file_path):
                                size_bytes += os.path.getsize(file_path)
                except Exception as e:
                    status = "error"
                    print(f"计算JSON存储大小失败: {e}")
            else:
                # 单个JSON文件
                json_file = CONFIG.get("json_file", "tasks.json")
                if os.path.exists(json_file):
                    size_bytes = os.path.getsize(json_file)
                else:
                    status = "warning"
        
        elif CONFIG["storage_type"] == "sqlite":
            # SQLite数据库
            db_file = CONFIG.get("sqlite_db", "tasks.db")
            if os.path.exists(db_file):
                size_bytes = os.path.getsize(db_file)
            else:
                status = "warning"
        
        elif CONFIG["storage_type"] == "redis":
            # Redis存储 - 估算大小
            try:
                import redis.asyncio as redis
                redis_client = redis.from_url(CONFIG["redis_url"], db=CONFIG["redis_db"])
                await redis_client.ping()
                # Redis大小估算：每个任务约1KB
                size_bytes = len(tasks) * 1024
            except Exception as e:
                status = "error"
                print(f"Redis连接失败: {e}")
        
        # 计算使用率（基于任务数量）
        usage_percent = min(len(tasks) * 2, 100)  # 每个任务2%使用率，最大100%
        
        info = {
            "status": status,
            "size_bytes": size_bytes,
            "usage_percent": usage_percent,
            "total_tasks": len(tasks),
            "enabled_tasks": len([t for t in tasks if t.get("enabled", True)]),
            "disabled_tasks": len([t for t in tasks if not t.get("enabled", True)]),
            "storage_type": CONFIG["storage_type"],
            "config_file": CONFIG.get("json_file", CONFIG.get("sqlite_db", "Redis"))
        }
        
        return info
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"获取存储信息失败: {str(e)}")

@app.post("/api/settings/cleanup")
async def cleanup_system_data() -> JSONResponse:
    """清理系统数据"""
    try:
        # 清理旧的历史记录
        if task_store:
            await task_store.cleanup_old_history(days=30)
        
        # 清理日志缓存
        global log_buffer
        if len(log_buffer) > 1000:
            log_buffer = log_buffer[-500:]
        
        return JSONResponse({"message": "系统数据清理完成"})
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"清理失败: {str(e)}")

@app.post("/api/settings/restart-task-service")
async def restart_task_service() -> JSONResponse:
    """重启任务服务"""
    try:
        if not task_service:
            raise HTTPException(status_code=500, detail="任务服务未初始化")
        
        # 重新加载任务配置
        await task_service.update_jobs()
        
        return JSONResponse({"message": "任务服务已重启"})
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"重启任务服务失败: {str(e)}")

@app.post("/api/settings/validate-config")
async def validate_config() -> JSONResponse:
    """验证当前配置"""
    try:
        validation_results = {}
        
        # 验证存储配置
        if CONFIG["storage_type"] == "redis":
            try:
                import redis.asyncio as redis
                redis_client = redis.from_url(CONFIG["redis_url"], db=CONFIG["redis_db"])
                await redis_client.ping()
                validation_results["redis"] = {"status": "success", "message": "Redis连接正常"}
                await redis_client.close()
            except Exception as e:
                validation_results["redis"] = {"status": "error", "message": f"Redis连接失败: {str(e)}"}
        
        elif CONFIG["storage_type"] == "sqlite":
            try:
                import sqlite3
                # 尝试连接SQLite数据库
                conn = sqlite3.connect(CONFIG["sqlite_db"])
                conn.close()
                validation_results["sqlite"] = {"status": "success", "message": "SQLite数据库正常"}
            except Exception as e:
                validation_results["sqlite"] = {"status": "error", "message": f"SQLite数据库错误: {str(e)}"}
        
        else:  # JSON存储
            try:
                if CONFIG.get("jobs_directory"):
                    # 检查jobs目录
                    jobs_dir = CONFIG["jobs_directory"]
                    if not os.path.exists(jobs_dir):
                        os.makedirs(jobs_dir, exist_ok=True)
                    validation_results["json"] = {"status": "success", "message": f"Jobs目录正常: {jobs_dir}"}
                else:
                    # 检查JSON文件
                    json_file = CONFIG["json_file"]
                    if os.path.exists(json_file):
                        validation_results["json"] = {"status": "success", "message": f"JSON文件正常: {json_file}"}
                    else:
                        validation_results["json"] = {"status": "warning", "message": f"JSON文件不存在，将自动创建: {json_file}"}
            except Exception as e:
                validation_results["json"] = {"status": "error", "message": f"JSON存储错误: {str(e)}"}
        
        return JSONResponse({
            "message": "配置验证完成",
            "validation_results": validation_results
        })
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"配置验证失败: {str(e)}")

# 单个任务操作API - 必须在批量操作之后定义
@app.post("/api/tasks/{task_id}/clear-history")
async def clear_task_history(task_id: str) -> JSONResponse:
    """清空任务执行历史"""
    if not task_store:
        raise HTTPException(status_code=500, detail="任务存储未初始化")
    
    try:
        await task_store.clear_task_history(task_id)
        return JSONResponse({"message": f"任务 {task_id} 的历史记录已清空"})
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.get("/api/system/stats")
async def get_system_stats() -> Dict[str, Any]:
    """获取系统统计信息"""
    try:
        import psutil
        
        # CPU 统计
        cpu_percent = psutil.cpu_percent(interval=1)
        cpu_count = psutil.cpu_count()
        cpu_freq = psutil.cpu_freq()
        
        # 内存统计
        memory = psutil.virtual_memory()
        
        # 磁盘统计
        disk = psutil.disk_usage('/')
        
        # 网络统计
        net_io = psutil.net_io_counters()
        
        # 进程统计
        process_count = len(psutil.pids())
        
        return {
            "cpu": {
                "percent": cpu_percent,
                "count": cpu_count,
                "freq": cpu_freq.current if cpu_freq else 0
            },
            "memory": {
                "total": f"{memory.total // (1024**3):.1f} GB",
                "available": f"{memory.available // (1024**3):.1f} GB",
                "used": f"{memory.used // (1024**3):.1f} GB",
                "percent": memory.percent
            },
            "disk": {
                "total": f"{disk.total // (1024**3):.1f} GB",
                "used": f"{disk.used // (1024**3):.1f} GB",
                "free": f"{disk.free // (1024**3):.1f} GB",
                "percent": disk.percent
            },
            "network": {
                "bytes_sent": f"{net_io.bytes_sent // 1024} KB",
                "bytes_recv": f"{net_io.bytes_recv // 1024} KB"
            },
            "processes": {
                "count": process_count
            }
        }
    except Exception as e:
        print(f"获取系统统计信息失败: {e}")
        return {
            "cpu": {"percent": 0, "count": 0, "freq": 0},
            "memory": {"total": "0 GB", "available": "0 GB", "used": "0 GB", "percent": 0},
            "disk": {"total": "0 GB", "used": "0 GB", "free": "0 GB", "percent": 0},
            "network": {"bytes_sent": "0 KB", "bytes_recv": "0 KB"},
            "processes": {"count": 0}
        }

@app.get("/api/system/processes")
async def get_processes(page: int = 1, limit: int = 20) -> Dict[str, Any]:
    """获取进程列表"""
    try:
        import psutil
        
        processes = []
        for proc in psutil.process_iter(['pid', 'name', 'cpu_percent', 'memory_percent', 'status', 'create_time']):
            try:
                proc_info = proc.info
                processes.append({
                    "pid": proc_info['pid'],
                    "name": proc_info['name'],
                    "cpu_percent": proc_info['cpu_percent'],
                    "memory_percent": proc_info['memory_percent'],
                    "status": proc_info['status'],
                    "create_time": datetime.fromtimestamp(proc_info['create_time']).isoformat()
                })
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                pass
        
        # 按CPU使用率排序（处理None值）
        processes.sort(key=lambda x: x['cpu_percent'] or 0, reverse=True)
        
        total = len(processes)
        start = (page - 1) * limit
        end = start + limit
        
        return {
            "processes": processes[start:end],
            "total": total,
            "page": page,
            "limit": limit,
            "total_pages": (total + limit - 1) // limit,
            "has_prev": page > 1,
            "has_next": end < total
        }
    except Exception as e:
        print(f"获取进程信息失败: {e}")
        return {
            "processes": [],
            "total": 0,
            "page": page,
            "limit": limit,
            "total_pages": 0,
            "has_prev": False,
            "has_next": False
        }

@app.post("/api/system/processes/{pid}/stop")
async def stop_process(pid: int) -> JSONResponse:
    """停止进程（发送SIGTERM信号）"""
    try:
        import psutil
        import signal
        
        # 检查进程是否存在
        if not psutil.pid_exists(pid):
            raise HTTPException(status_code=404, detail="进程不存在")
        
        process = psutil.Process(pid)
        
        # 检查权限
        try:
            process.status()
        except psutil.AccessDenied:
            raise HTTPException(status_code=403, detail="没有权限操作此进程")
        
        # 发送SIGTERM信号
        process.terminate()
        
        # 等待进程结束（最多等待5秒）
        try:
            process.wait(timeout=5)
            return JSONResponse({"message": f"进程 {pid} 已成功停止"})
        except psutil.TimeoutExpired:
            # 如果进程没有在5秒内结束，返回警告
            return JSONResponse(
                {"message": f"进程 {pid} 停止信号已发送，但进程可能仍在运行"}, 
                status_code=202
            )
            
    except psutil.NoSuchProcess:
        raise HTTPException(status_code=404, detail="进程不存在")
    except psutil.AccessDenied:
        raise HTTPException(status_code=403, detail="没有权限操作此进程")
    except Exception as e:
        print(f"停止进程失败: {e}")
        raise HTTPException(status_code=500, detail=f"停止进程失败: {str(e)}")

@app.post("/api/system/processes/{pid}/kill")
async def kill_process(pid: int) -> JSONResponse:
    """强制杀死进程（发送SIGKILL信号）"""
    try:
        import psutil
        import signal
        
        # 检查进程是否存在
        if not psutil.pid_exists(pid):
            raise HTTPException(status_code=404, detail="进程不存在")
        
        process = psutil.Process(pid)
        
        # 检查权限
        try:
            process.status()
        except psutil.AccessDenied:
            raise HTTPException(status_code=403, detail="没有权限操作此进程")
        
        # 发送SIGKILL信号
        process.kill()
        
        # 等待进程结束（最多等待3秒）
        try:
            process.wait(timeout=3)
            return JSONResponse({"message": f"进程 {pid} 已被强制终止"})
        except psutil.TimeoutExpired:
            # 如果进程没有在3秒内结束，返回警告
            return JSONResponse(
                {"message": f"进程 {pid} 终止信号已发送，但进程可能仍在运行"}, 
                status_code=202
            )
            
    except psutil.NoSuchProcess:
        raise HTTPException(status_code=404, detail="进程不存在")
    except psutil.AccessDenied:
        raise HTTPException(status_code=403, detail="没有权限操作此进程")
    except Exception as e:
        print(f"杀死进程失败: {e}")
        raise HTTPException(status_code=500, detail=f"杀死进程失败: {str(e)}")

@app.get("/api/system/processes/{pid}/info")
async def get_process_info(pid: int) -> Dict[str, Any]:
    """获取进程详细信息"""
    try:
        import psutil
        
        # 检查进程是否存在
        if not psutil.pid_exists(pid):
            raise HTTPException(status_code=404, detail="进程不存在")
        
        process = psutil.Process(pid)
        
        # 获取进程信息
        with process.oneshot():
            info = {
                "pid": process.pid,
                "name": process.name(),
                "exe": process.exe(),
                "cmdline": process.cmdline(),
                "status": process.status(),
                "create_time": datetime.fromtimestamp(process.create_time()).isoformat(),
                "cpu_percent": process.cpu_percent(),
                "memory_percent": process.memory_percent(),
                "memory_info": {
                    "rss": process.memory_info().rss,
                    "vms": process.memory_info().vms
                },
                "num_threads": process.num_threads(),
                "num_fds": process.num_fds() if hasattr(process, 'num_fds') else None,
                "connections": []
            }
            
            # 获取网络连接信息
            try:
                connections = process.connections()
                for conn in connections:
                    info["connections"].append({
                        "fd": conn.fd,
                        "family": conn.family,
                        "type": conn.type,
                        "laddr": f"{conn.laddr.ip}:{conn.laddr.port}" if conn.laddr else None,
                        "raddr": f"{conn.raddr.ip}:{conn.raddr.port}" if conn.raddr else None,
                        "status": conn.status
                    })
            except (psutil.AccessDenied, psutil.ZombieProcess):
                pass
            
            return info
            
    except psutil.NoSuchProcess:
        raise HTTPException(status_code=404, detail="进程不存在")
    except psutil.AccessDenied:
        raise HTTPException(status_code=403, detail="没有权限访问此进程")
    except Exception as e:
        print(f"获取进程信息失败: {e}")
        raise HTTPException(status_code=500, detail=f"获取进程信息失败: {str(e)}")

# ==================== 健康检查 ====================

@app.get("/health")
async def health_check() -> Dict[str, str]:
    """健康检查"""
    return {"status": "healthy", "timestamp": datetime.now().isoformat()}

@app.post("/api/upload")
async def upload_file(file: UploadFile, remote_path: str = Form(...)):
    """上传文件"""
    try:
        # 安全检查：确保远程路径不为空
        if not remote_path or remote_path.strip() == '':
            raise HTTPException(status_code=400, detail="文件路径不能为空")
        
        # 去除首尾空格
        remote_path = remote_path.strip()
        
        # 检查路径遍历攻击
        if '..' in remote_path:
            raise HTTPException(status_code=400, detail="路径中不能包含 .. 等特殊字符")
        
        # 定义需要管理员权限的目录（禁止访问）
        admin_dirs = [
            '/etc', '/var', '/usr', '/bin', '/sbin', '/lib', '/lib64',
            '/root', '/boot', '/dev', '/proc', '/sys',
            '/System', '/Applications', '/Library',
            'C:\\', 'D:\\', 'E:\\', 'F:\\', 'G:\\', 'H:\\', 'I:\\', 'J:\\', 'K:\\', 'L:\\', 'M:\\', 'N:\\', 'O:\\', 'P:\\', 'Q:\\', 'R:\\', 'S:\\', 'T:\\', 'U:\\', 'V:\\', 'W:\\', 'X:\\', 'Y:\\', 'Z:\\'
        ]
        
        # 检查是否访问管理员目录
        normalized_path = os.path.normpath(remote_path)
        for admin_dir in admin_dirs:
            if normalized_path.startswith(admin_dir):
                raise HTTPException(status_code=403, detail=f"禁止访问系统目录: {admin_dir}")
        
        # 标准化远程路径
        if not remote_path.startswith('./') and not remote_path.startswith('/'):
            remote_path = f'./{remote_path}'
        
        # 确保目标目录存在
        target_dir = os.path.dirname(remote_path)
        if target_dir and not os.path.exists(target_dir):
            try:
                os.makedirs(target_dir, exist_ok=True)
            except PermissionError:
                raise HTTPException(status_code=403, detail=f"没有权限创建目录: {target_dir}")
        
        # 保存文件
        try:
            with open(remote_path, 'wb') as f:
                content = await file.read()
                f.write(content)
        except PermissionError:
            raise HTTPException(status_code=403, detail=f"没有权限写入文件: {remote_path}")
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"写入文件失败: {str(e)}")
        
        return JSONResponse({
            "message": "文件上传成功",
            "filename": file.filename,
            "remote_path": remote_path,
            "size": len(content)
        })
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"文件上传失败: {str(e)}")

# Git仓库管理相关模型
class GitRepository(BaseModel):
    path: str = Field(..., description="Git仓库路径")
    name: str = Field(..., description="仓库名称")
    remote_url: Optional[str] = Field(None, description="远程仓库URL")
    current_branch: Optional[str] = Field(None, description="当前分支")
    status: str = Field("unknown", description="仓库状态")
    last_update: Optional[str] = Field(None, description="最后更新时间")

class GitUpdateRequest(BaseModel):
    repositories: List[str] = Field(..., description="要更新的仓库路径列表")
    force: bool = Field(False, description="是否强制更新")

class GitScanRequest(BaseModel):
    base_path: str = Field(..., description="扫描的基础路径")
    page: int = Field(1, ge=1, description="页码")
    limit: int = Field(20, ge=1, le=100, description="每页数量")

class DingTalkAlertConfig(BaseModel):
    """钉钉告警配置"""
    enable: bool = Field(False, description="是否启用")
    access_token: str = Field("", description="钉钉机器人访问令牌")
    secret: str = Field("", description="钉钉机器人密钥（可选）")
    url: str = Field("https://oapi.dingtalk.com/robot/send?access_token=", description="钉钉机器人URL")
    name: str = Field("", description="告警配置名称")

class DingTalkConfig(BaseModel):
    """钉钉告警总配置"""
    # 程序异常中断退出通知告警
    system_alert: DingTalkAlertConfig = Field(
        default_factory=lambda: DingTalkAlertConfig(name="系统异常告警"),
        description="系统异常告警配置"
    )
    # 系统负载监控告警
    monitor_alert: DingTalkAlertConfig = Field(
        default_factory=lambda: DingTalkAlertConfig(name="系统监控告警"),
        description="系统监控告警配置"
    )
    # 任务执行异常告警
    task_alert: DingTalkAlertConfig = Field(
        default_factory=lambda: DingTalkAlertConfig(name="任务异常告警"),
        description="任务异常告警配置"
    )

class SystemMonitorConfig(BaseModel):
    enable_system_monitor: bool = Field(False, description="是否启用系统监控")
    cpu_threshold: float = Field(89.0, ge=0, le=100, description="CPU使用率阈值")
    memory_threshold: float = Field(90.0, ge=0, le=100, description="内存使用率阈值")
    disk_threshold: float = Field(90.0, ge=0, le=100, description="磁盘使用率阈值")
    monitor_check_interval: int = Field(60, ge=10, le=3600, description="监控检查间隔（秒）")
    alert_cooldown_interval: int = Field(300, ge=60, le=3600, description="告警冷却间隔（秒）")

# ==================== Git 仓库管理 API ====================

@app.post("/api/git/scan")
async def scan_git_repositories(request: GitScanRequest) -> Dict[str, Any]:
    """扫描指定路径下的 git 仓库"""
    try:
        if not git_service:
            raise HTTPException(status_code=500, detail="Git 服务未初始化")
        
        result = await git_service.scan_git_repositories(
            request.base_path, 
            request.page, 
            request.limit
        )
        
        return {
            "success": True,
            "repositories": result["repositories"],
            "pagination": {
                "total": result["total"],
                "page": result["page"],
                "limit": result["limit"],
                "total_pages": result["total_pages"],
                "has_next": result["has_next"],
                "has_prev": result["has_prev"]
            },
            "scan_time": datetime.now().isoformat()
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"扫描失败: {str(e)}")

@app.post("/api/git/update")
async def update_git_repositories(request: GitUpdateRequest) -> Dict[str, Any]:
    """更新指定的 git 仓库"""
    try:
        if not git_service:
            raise HTTPException(status_code=500, detail="Git 服务未初始化")
        
        results = await git_service.update_repositories(request.repositories, request.force)
        
        return {
            "success": True,
            "results": results,
            "summary": {
                "total": len(request.repositories),
                "success": len(results["success"]),
                "failed": len(results["failed"]),
                "skipped": len(results["skipped"])
            },
            "update_time": datetime.now().isoformat()
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"更新失败: {str(e)}")

@app.get("/api/git/repository/{repo_path:path}")
async def get_repository_details(repo_path: str) -> Dict[str, Any]:
    """获取仓库详细信息"""
    try:
        if not git_service:
            raise HTTPException(status_code=500, detail="Git 服务未初始化")
        
        # URL 解码路径
        import urllib.parse
        decoded_path = urllib.parse.unquote(repo_path)
        
        details = await git_service.get_repository_details(decoded_path)
        
        if not details:
            raise HTTPException(status_code=404, detail="仓库不存在或无法访问")
        
        return {
            "success": True,
            "repository": details
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"获取仓库信息失败: {str(e)}")

@app.post("/api/git/clear-cache")
async def clear_git_cache() -> JSONResponse:
    """清除 git 服务缓存"""
    try:
        if not git_service:
            raise HTTPException(status_code=500, detail="Git 服务未初始化")
        
        git_service.clear_cache()
        
        return JSONResponse(
            status_code=200,
            content={"success": True, "message": "缓存已清除"}
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"清除缓存失败: {str(e)}")

# ==================== 钉钉告警 API ====================

@app.get("/api/settings/dingtalk-config")
async def get_dingtalk_config() -> DingTalkConfig:
    """获取钉钉告警配置"""
    # 从CONFIG中读取配置，如果不存在则使用默认值
    return DingTalkConfig(
        system_alert=DingTalkAlertConfig(
            enable=CONFIG.get("dingtalk_system_alert_enable", False),
            access_token=CONFIG.get("dingtalk_system_alert_token", ""),
            secret=CONFIG.get("dingtalk_system_alert_secret", ""),
            url=CONFIG.get("dingtalk_system_alert_url", "https://oapi.dingtalk.com/robot/send?access_token="),
            name="系统异常告警"
        ),
        monitor_alert=DingTalkAlertConfig(
            enable=CONFIG.get("dingtalk_monitor_alert_enable", False),
            access_token=CONFIG.get("dingtalk_monitor_alert_token", ""),
            secret=CONFIG.get("dingtalk_monitor_alert_secret", ""),
            url=CONFIG.get("dingtalk_monitor_alert_url", "https://oapi.dingtalk.com/robot/send?access_token="),
            name="系统监控告警"
        ),
        task_alert=DingTalkAlertConfig(
            enable=CONFIG.get("dingtalk_task_alert_enable", False),
            access_token=CONFIG.get("dingtalk_task_alert_token", ""),
            secret=CONFIG.get("dingtalk_task_alert_secret", ""),
            url=CONFIG.get("dingtalk_task_alert_url", "https://oapi.dingtalk.com/robot/send?access_token="),
            name="任务异常告警"
        )
    )

@app.put("/api/settings/dingtalk-config")
async def update_dingtalk_config(config: DingTalkConfig) -> JSONResponse:
    """更新钉钉告警配置"""
    try:
        # 准备要保存的配置
        config_to_save = {
            # 系统异常告警配置
            "dingtalk_system_alert_enable": config.system_alert.enable,
            "dingtalk_system_alert_token": config.system_alert.access_token,
            "dingtalk_system_alert_secret": config.system_alert.secret,
            "dingtalk_system_alert_url": config.system_alert.url,
            
            # 系统监控告警配置
            "dingtalk_monitor_alert_enable": config.monitor_alert.enable,
            "dingtalk_monitor_alert_token": config.monitor_alert.access_token,
            "dingtalk_monitor_alert_secret": config.monitor_alert.secret,
            "dingtalk_monitor_alert_url": config.monitor_alert.url,
            
            # 任务异常告警配置
            "dingtalk_task_alert_enable": config.task_alert.enable,
            "dingtalk_task_alert_token": config.task_alert.access_token,
            "dingtalk_task_alert_secret": config.task_alert.secret,
            "dingtalk_task_alert_url": config.task_alert.url
        }
        
        # 更新内存中的配置
        global CONFIG
        CONFIG.update(config_to_save)
        
        # 保存到配置存储系统
        await update_config(config_to_save)
        
        print(f"✅ 钉钉告警配置已保存到存储: {list(config_to_save.keys())}")
        
        return JSONResponse({"message": "钉钉告警配置更新成功"})
            
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"更新钉钉告警配置失败: {str(e)}")

@app.post("/api/settings/dingtalk-test")
async def test_dingtalk_connection() -> JSONResponse:
    """测试钉钉连接"""
    try:
        # 检查是否有任何告警配置启用
        has_enabled_alert = (
            CONFIG.get("dingtalk_system_alert_enable", False) or
            CONFIG.get("dingtalk_monitor_alert_enable", False) or
            CONFIG.get("dingtalk_task_alert_enable", False)
        )
        
        if not has_enabled_alert:
            return JSONResponse(
                {"message": "没有启用任何钉钉告警配置"}, 
                status_code=400
            )
        
        # 测试所有启用的告警配置
        test_results = []
        
        if CONFIG.get("dingtalk_system_alert_enable", False):
            try:
                from services.dingtalk_alert import DingTalkAlert
                alert = DingTalkAlert(
                    ding_access_token=CONFIG.get("dingtalk_system_alert_token", ""),
                    ding_url=CONFIG.get("dingtalk_system_alert_url", "https://oapi.dingtalk.com/robot/send?access_token="),
                    secret=CONFIG.get("dingtalk_system_alert_secret", "")
                )
                success = alert.test_connection()
                test_results.append(f"系统异常告警: {'成功' if success else '失败'}")
            except Exception as e:
                test_results.append(f"系统异常告警: 失败 ({str(e)})")
        
        if CONFIG.get("dingtalk_monitor_alert_enable", False):
            try:
                from services.dingtalk_alert import DingTalkAlert
                alert = DingTalkAlert(
                    ding_access_token=CONFIG.get("dingtalk_monitor_alert_token", ""),
                    ding_url=CONFIG.get("dingtalk_monitor_alert_url", "https://oapi.dingtalk.com/robot/send?access_token="),
                    secret=CONFIG.get("dingtalk_monitor_alert_secret", "")
                )
                success = alert.test_connection()
                test_results.append(f"系统监控告警: {'成功' if success else '失败'}")
            except Exception as e:
                test_results.append(f"系统监控告警: 失败 ({str(e)})")
        
        if CONFIG.get("dingtalk_task_alert_enable", False):
            try:
                from services.dingtalk_alert import DingTalkAlert
                alert = DingTalkAlert(
                    ding_access_token=CONFIG.get("dingtalk_task_alert_token", ""),
                    ding_url=CONFIG.get("dingtalk_task_alert_url", "https://oapi.dingtalk.com/robot/send?access_token="),
                    secret=CONFIG.get("dingtalk_task_alert_secret", "")
                )
                success = alert.test_connection()
                test_results.append(f"任务异常告警: {'成功' if success else '失败'}")
            except Exception as e:
                test_results.append(f"任务异常告警: 失败 ({str(e)})")
        
        return JSONResponse({
            "message": "钉钉连接测试完成",
            "results": test_results
        })
        
    except Exception as e:
        return JSONResponse(
            {"message": f"钉钉连接测试失败: {str(e)}"}, 
            status_code=500
        )

@app.post("/api/settings/dingtalk-send-test")
async def send_dingtalk_test_message() -> JSONResponse:
    """发送钉钉测试消息"""
    try:
        # 检查是否有任何告警配置启用
        has_enabled_alert = (
            CONFIG.get("dingtalk_system_alert_enable", False) or
            CONFIG.get("dingtalk_monitor_alert_enable", False) or
            CONFIG.get("dingtalk_task_alert_enable", False)
        )
        
        if not has_enabled_alert:
            return JSONResponse(
                {"message": "没有启用任何钉钉告警配置"}, 
                status_code=400
            )
        
        # 发送测试消息到所有启用的告警配置
        send_results = []
        
        if CONFIG.get("dingtalk_system_alert_enable", False):
            try:
                from services.dingtalk_alert import DingTalkAlert
                alert = DingTalkAlert(
                    ding_access_token=CONFIG.get("dingtalk_system_alert_token", ""),
                    ding_url=CONFIG.get("dingtalk_system_alert_url", "https://oapi.dingtalk.com/robot/send?access_token="),
                    secret=CONFIG.get("dingtalk_system_alert_secret", "")
                )
                result = alert.send_system_alert(
                    alert_type="系统测试",
                    details="这是一条系统异常告警测试消息，用于验证钉钉告警功能是否正常工作。"
                )
                if result.get("errcode") == 0:
                    send_results.append("系统异常告警: 发送成功")
                else:
                    send_results.append(f"系统异常告警: 发送失败 ({result.get('errmsg', '未知错误')})")
            except Exception as e:
                send_results.append(f"系统异常告警: 发送失败 ({str(e)})")
        
        if CONFIG.get("dingtalk_monitor_alert_enable", False):
            try:
                from services.dingtalk_alert import DingTalkAlert
                alert = DingTalkAlert(
                    ding_access_token=CONFIG.get("dingtalk_monitor_alert_token", ""),
                    ding_url=CONFIG.get("dingtalk_monitor_alert_url", "https://oapi.dingtalk.com/robot/send?access_token="),
                    secret=CONFIG.get("dingtalk_monitor_alert_secret", "")
                )
                result = alert.send_system_alert(
                    alert_type="系统测试",
                    details="这是一条系统监控告警测试消息，用于验证钉钉告警功能是否正常工作。"
                )
                if result.get("errcode") == 0:
                    send_results.append("系统监控告警: 发送成功")
                else:
                    send_results.append(f"系统监控告警: 发送失败 ({result.get('errmsg', '未知错误')})")
            except Exception as e:
                send_results.append(f"系统监控告警: 发送失败 ({str(e)})")
        
        if CONFIG.get("dingtalk_task_alert_enable", False):
            try:
                from services.dingtalk_alert import DingTalkAlert
                alert = DingTalkAlert(
                    ding_access_token=CONFIG.get("dingtalk_task_alert_token", ""),
                    ding_url=CONFIG.get("dingtalk_task_alert_url", "https://oapi.dingtalk.com/robot/send?access_token="),
                    secret=CONFIG.get("dingtalk_task_alert_secret", "")
                )
                result = alert.send_system_alert(
                    alert_type="系统测试",
                    details="这是一条任务异常告警测试消息，用于验证钉钉告警功能是否正常工作。"
                )
                if result.get("errcode") == 0:
                    send_results.append("任务异常告警: 发送成功")
                else:
                    send_results.append(f"任务异常告警: 发送失败 ({result.get('errmsg', '未知错误')})")
            except Exception as e:
                send_results.append(f"任务异常告警: 发送失败 ({str(e)})")
        
        return JSONResponse({
            "message": "钉钉测试消息发送完成",
            "results": send_results
        })
        
    except Exception as e:
        return JSONResponse(
            {"message": f"发送钉钉测试消息失败: {str(e)}"}, 
            status_code=500
        )

# ==================== 系统监控 API ====================

@app.get("/api/settings/system-monitor-config")
async def get_system_monitor_config() -> SystemMonitorConfig:
    """获取系统监控配置"""
    return SystemMonitorConfig(
        enable_system_monitor=CONFIG["enable_system_monitor"],
        cpu_threshold=CONFIG["cpu_threshold"],
        memory_threshold=CONFIG["memory_threshold"],
        disk_threshold=CONFIG["disk_threshold"],
        monitor_check_interval=CONFIG["monitor_check_interval"],
        alert_cooldown_interval=CONFIG.get("alert_cooldown_interval", 300)
    )

@app.put("/api/settings/system-monitor-config")
async def update_system_monitor_config(config: SystemMonitorConfig) -> JSONResponse:
    """更新系统监控配置"""
    global system_monitor
    
    try:
        # 准备要保存的配置
        config_to_save = {
            "enable_system_monitor": config.enable_system_monitor,
            "cpu_threshold": config.cpu_threshold,
            "memory_threshold": config.memory_threshold,
            "disk_threshold": config.disk_threshold,
            "monitor_check_interval": config.monitor_check_interval,
            "alert_cooldown_interval": config.alert_cooldown_interval
        }
        
        # 更新内存中的配置
        global CONFIG
        CONFIG.update(config_to_save)
        
        # 保存到配置存储系统
        await update_config(config_to_save)
        
        print(f"✅ 系统监控配置已保存到存储: {list(config_to_save.keys())}")
        
        # 重新初始化系统监控
        if config.enable_system_monitor:
            try:
                from services.system_monitor import SystemMonitor
                if system_monitor:
                    await system_monitor.stop()
                
                system_monitor = SystemMonitor(dingtalk_alert)
                system_monitor.update_thresholds({
                    "cpu_usage": config.cpu_threshold,
                    "memory_usage": config.memory_threshold,
                    "disk_usage": config.disk_threshold,
                    "check_interval": config.monitor_check_interval,
                    "alert_cooldown_interval": config.alert_cooldown_interval
                })
                await system_monitor.start()
                return JSONResponse({"message": "系统监控配置更新成功"})
            except Exception as e:
                raise HTTPException(status_code=400, detail=f"系统监控初始化失败: {str(e)}")
        else:
            if system_monitor:
                await system_monitor.stop()
                system_monitor = None
            return JSONResponse({"message": "系统监控已禁用"})
            
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"更新系统监控配置失败: {str(e)}")

@app.get("/api/settings/system-monitor-metrics")
async def get_system_monitor_metrics() -> Dict[str, Any]:
    """获取系统监控指标"""
    global system_monitor
    
    try:
        # 如果系统监控未启用，返回基本指标
        if not CONFIG.get("enable_system_monitor", False):
            # 获取基本系统指标作为fallback
            import psutil
            import time
            
            # CPU使用率
            cpu_usage = psutil.cpu_percent(interval=1)
            
            # 内存使用率
            memory = psutil.virtual_memory()
            memory_usage = memory.percent
            
            # 磁盘使用率
            try:
                disk = psutil.disk_usage('/')
                disk_usage = (disk.used / disk.total) * 100
            except:
                disk_usage = 0.0
            
            return {
                "cpu_usage": cpu_usage,
                "memory_usage": memory_usage,
                "disk_usage": disk_usage,
                "timestamp": time.time(),
                "thresholds": {
                    "cpu_usage": CONFIG.get("cpu_threshold", 89.0),
                    "memory_usage": CONFIG.get("memory_threshold", 90.0),
                    "disk_usage": CONFIG.get("disk_threshold", 90.0)
                },
                "alert_status": {
                    "cpu_alerted": False,
                    "memory_alerted": False,
                    "disk_alerted": False
                },
                "monitor_enabled": False,
                "message": "系统监控未启用，显示基本指标"
            }
        
        # 如果系统监控已启用但实例不存在，尝试重新初始化
        if not system_monitor:
            try:
                from services.system_monitor import SystemMonitor
                
                # 获取系统监控钉钉告警实例
                monitor_dingtalk_alert = None
                if CONFIG.get("dingtalk_monitor_alert_enable", False) and CONFIG.get("dingtalk_monitor_alert_token"):
                    from services.dingtalk_alert import DingTalkAlert
                    monitor_dingtalk_alert = DingTalkAlert(
                        ding_access_token=CONFIG["dingtalk_monitor_alert_token"],
                        ding_url=CONFIG.get("dingtalk_monitor_alert_url", "https://oapi.dingtalk.com/robot/send?access_token="),
                        secret=CONFIG.get("dingtalk_monitor_alert_secret", "")
                    )
                
                # 创建系统监控实例
                system_monitor = SystemMonitor(monitor_dingtalk_alert)
                
                # 设置监控阈值
                system_monitor.update_thresholds({
                    "cpu_usage": CONFIG.get("cpu_threshold", 89.0),
                    "memory_usage": CONFIG.get("memory_threshold", 90.0),
                    "disk_usage": CONFIG.get("disk_threshold", 90.0),
                    "check_interval": CONFIG.get("monitor_check_interval", 60)
                })
                
                print("✅ 系统监控实例已重新初始化")
            except Exception as e:
                print(f"❌ 重新初始化系统监控失败: {e}")
                # 如果重新初始化失败，返回基本指标
                import psutil
                import time
                
                cpu_usage = psutil.cpu_percent(interval=1)
                memory = psutil.virtual_memory()
                memory_usage = memory.percent
                
                try:
                    disk = psutil.disk_usage('/')
                    disk_usage = (disk.used / disk.total) * 100
                except:
                    disk_usage = 0.0
                
                return {
                    "cpu_usage": cpu_usage,
                    "memory_usage": memory_usage,
                    "disk_usage": disk_usage,
                    "timestamp": time.time(),
                    "thresholds": {
                        "cpu_usage": CONFIG.get("cpu_threshold", 89.0),
                        "memory_usage": CONFIG.get("memory_threshold", 90.0),
                        "disk_usage": CONFIG.get("disk_threshold", 90.0)
                    },
                    "alert_status": {
                        "cpu_alerted": False,
                        "memory_alerted": False,
                        "disk_alerted": False
                    },
                    "monitor_enabled": False,
                    "message": f"系统监控初始化失败: {str(e)}"
                }
        
        # 获取系统监控指标
        metrics = system_monitor.get_current_metrics()
        metrics["monitor_enabled"] = True
        metrics["message"] = "系统监控正常运行"
        
        return metrics
        
    except Exception as e:
        print(f"❌ 获取系统监控指标失败: {e}")
        # 返回错误信息但包含基本指标
        import psutil
        import time
        
        try:
            cpu_usage = psutil.cpu_percent(interval=1)
            memory = psutil.virtual_memory()
            memory_usage = memory.percent
            
            try:
                disk = psutil.disk_usage('/')
                disk_usage = (disk.used / disk.total) * 100
            except:
                disk_usage = 0.0
        except:
            cpu_usage = 0.0
            memory_usage = 0.0
            disk_usage = 0.0
        
        return {
            "cpu_usage": cpu_usage,
            "memory_usage": memory_usage,
            "disk_usage": disk_usage,
            "timestamp": time.time(),
            "thresholds": {
                "cpu_usage": CONFIG.get("cpu_threshold", 89.0),
                "memory_usage": CONFIG.get("memory_threshold", 90.0),
                "disk_usage": CONFIG.get("disk_threshold", 90.0)
            },
            "alert_status": {
                "cpu_alerted": False,
                "memory_alerted": False,
                "disk_alerted": False
            },
            "monitor_enabled": False,
            "message": f"获取系统监控指标失败: {str(e)}"
        }

@app.post("/api/settings/system-monitor-test")
async def test_system_monitor() -> JSONResponse:
    """测试系统监控告警"""
    try:
        # 检查系统监控是否启用
        if not CONFIG.get("enable_system_monitor", False):
            return JSONResponse(
                {"message": "系统监控未启用，无法进行测试"}, 
                status_code=400
            )
        
        # 检查系统监控钉钉告警是否启用
        if not CONFIG.get("dingtalk_monitor_alert_enable", False):
            return JSONResponse(
                {"message": "系统监控钉钉告警未启用，无法发送测试消息"}, 
                status_code=400
            )
        
        # 检查钉钉告警配置是否完整
        if not CONFIG.get("dingtalk_monitor_alert_token"):
            return JSONResponse(
                {"message": "系统监控钉钉告警配置不完整，缺少访问令牌"}, 
                status_code=400
            )
        
        # 创建钉钉告警实例并发送测试消息
        try:
            from services.dingtalk_alert import DingTalkAlert
            monitor_alert = DingTalkAlert(
                ding_access_token=CONFIG["dingtalk_monitor_alert_token"],
                ding_url=CONFIG.get("dingtalk_monitor_alert_url", "https://oapi.dingtalk.com/robot/send?access_token="),
                secret=CONFIG.get("dingtalk_monitor_alert_secret", "")
            )
            
            result = monitor_alert.send_system_alert(
                "系统监控测试",
                "这是一条系统监控测试消息，用于验证监控告警功能是否正常工作。"
            )
            
            if result.get("errcode") == 0:
                return JSONResponse({"message": "系统监控测试告警发送成功"})
            else:
                return JSONResponse(
                    {"message": f"系统监控测试告警发送失败: {result.get('errmsg', '未知错误')}"}, 
                    status_code=400
                )
        except Exception as e:
            return JSONResponse(
                {"message": f"发送钉钉告警失败: {str(e)}"}, 
                status_code=500
            )
            
    except Exception as e:
        return JSONResponse(
            {"message": f"发送系统监控测试告警失败: {str(e)}"}, 
            status_code=500
        )

@app.get("/api/download")
async def download_file(filepath: str):
    """下载文件"""
    try:
        # 安全检查：确保文件路径不为空
        if not filepath or filepath.strip() == '':
            raise HTTPException(status_code=400, detail="文件路径不能为空")
        
        # 去除首尾空格
        filepath = filepath.strip()
        
        # 检查路径遍历攻击
        if '..' in filepath:
            raise HTTPException(status_code=400, detail="路径中不能包含 .. 等特殊字符")
        
        # 定义需要管理员权限的目录（禁止访问）
        admin_dirs = [
            '/etc', '/var', '/usr', '/bin', '/sbin', '/lib', '/lib64',
            '/root', '/boot', '/dev', '/proc', '/sys',
            '/System', '/Applications', '/Library',
            'C:\\', 'D:\\', 'E:\\', 'F:\\', 'G:\\', 'H:\\', 'I:\\', 'J:\\', 'K:\\', 'L:\\', 'M:\\', 'N:\\', 'O:\\', 'P:\\', 'Q:\\', 'R:\\', 'S:\\', 'T:\\', 'U:\\', 'V:\\', 'W:\\', 'X:\\', 'Y:\\', 'Z:\\'
        ]
        
        # 检查是否访问管理员目录
        normalized_path = os.path.normpath(filepath)
        for admin_dir in admin_dirs:
            if normalized_path.startswith(admin_dir):
                raise HTTPException(status_code=403, detail=f"禁止访问系统目录: {admin_dir}")
        
        # 标准化文件路径
        if not filepath.startswith('./') and not filepath.startswith('/'):
            filepath = f'./{filepath}'
        
        # 清理路径中的多余的 ./
        import re
        # 移除路径中多余的 ./
        filepath = re.sub(r'\./\./', './', filepath)
        filepath = re.sub(r'\./\.\./', './', filepath)
        # 移除路径中的 ./ 和 ../
        filepath = re.sub(r'/[.]/', '/', filepath)
        filepath = re.sub(r'/[.][.]/', '/', filepath)
        
        # 检查文件是否存在
        if not os.path.exists(filepath):
            raise HTTPException(status_code=404, detail=f"文件不存在: {filepath}")
        
        # 获取文件名
        filename = os.path.basename(filepath)
        
        # 读取文件内容
        try:
            with open(filepath, 'rb') as f:
                content = f.read()
        except PermissionError:
            raise HTTPException(status_code=403, detail=f"没有权限读取文件: {filepath}")
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"读取文件失败: {str(e)}")
        
        # 返回文件下载响应
        from fastapi.responses import Response
        return Response(
            content=content,
            media_type='application/octet-stream',
            headers={
                'Content-Disposition': f'attachment; filename="{filename}"'
            }
        )
    except HTTPException:
        # 重新抛出HTTPException
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"下载文件失败: {str(e)}")

# ==================== 启动入口 ====================

if __name__ == "__main__":
    uvicorn.run(
        "main:app",
        host=CONFIG["host"],
        port=CONFIG["port"],
        reload=True,
        log_level="info"
    )
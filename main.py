"""
Mini PM2 FastAPI 主应用
支持 JSON 文件、Redis 和 SQLite 三种任务配置源
"""

import os
import json
import asyncio
from typing import Dict, List, Optional, Any
from datetime import datetime
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect, HTTPException
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
from config_manager import update_config, get_all_config

# 配置
CONFIG = {
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
    "port": int(os.getenv("PORT", "8100"))
}

# 全局变量
task_store: Optional[TaskStore] = None
task_service: Optional[TaskService] = None
monitoring_service: Optional[MonitoringService] = None
export_service: Optional[ExportService] = None
redis_client: Optional[redis.Redis] = None
log_buffer: List[str] = []
task_status: Dict[str, Dict[str, Any]] = {}
websocket_connections: List[WebSocket] = []

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
    output: Optional[List[str]]

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
    global task_store, task_service, redis_client, export_service
    
    # 启动时初始化
    print("🚀 启动 Mini PM2 FastAPI 应用...")
    
    # 记录应用启动时间
    app.state.start_time = datetime.now()
    
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
    
    # 初始化日志管理器
    from services.log_manager import get_log_manager
    get_log_manager(broadcast_callback=_broadcast_log, log_buffer=log_buffer)
    
    print("✅ 应用初始化完成")
    
    yield
    
    # 关闭时清理
    print("🛑 关闭应用...")
    if task_service:
        await task_service.stop()
    if monitoring_service:
        await monitoring_service.stop()
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
            output=status.get("output", [])
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
        "error_count": status.get("error_count", 0)
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
        # 构建存储配置
        storage_config = {
            "type": CONFIG["storage_type"],
            "config": {}
        }
        
        if CONFIG["storage_type"] == "redis":
            storage_config["config"] = {
                "redis_url": CONFIG["redis_url"],
                "redis_db": CONFIG["redis_db"]
            }
        elif CONFIG["storage_type"] == "sqlite":
            storage_config["config"] = {
                "sqlite_db": CONFIG["sqlite_db"]
            }
        else:
            storage_config["config"] = {
                "json_file": CONFIG["json_file"],
                "jobs_directory": CONFIG.get("jobs_directory"),
                "task_file_prefix": CONFIG.get("task_file_prefix", "task")
            }
        
        config = {
            "storage": storage_config,
            "logging": {
                "max_lines": CONFIG["log_limit"],
                "log_level": "INFO"
            },
            "monitoring": {
                "refresh_interval": CONFIG["monitoring_interval"],
                "enabled": CONFIG["enable_monitoring"]
            },
            "tasks": {
                "max_concurrent": 5,
                "default_interval": 10
            }
        }
        return config
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"获取配置失败: {str(e)}")

@app.put("/api/settings/config")
async def update_system_config(config: Dict[str, Any]) -> JSONResponse:
    """更新系统配置"""
    try:
        # 更新全局配置
        config_to_update = {}
        for key, value in config.items():
            if key in CONFIG:
                CONFIG[key] = value
                config_to_update[key] = value
                print(f"更新配置: {key} = {value}")
        
        # 批量更新到配置文件
        if config_to_update:
            update_config(config_to_update)
        
        # 如果存储类型发生变化，需要重新初始化存储
        if "storage_type" in config:
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
        
        # 获取存储配置信息
        storage_config = {
            "type": CONFIG["storage_type"],
            "config": {}
        }
        
        if CONFIG["storage_type"] == "redis":
            storage_config["config"] = {
                "url": CONFIG["redis_url"],
                "db": CONFIG["redis_db"]
            }
        elif CONFIG["storage_type"] == "sqlite":
            storage_config["config"] = {
                "database": CONFIG["sqlite_db"]
            }
        else:
            storage_config["config"] = {
                "file": CONFIG["json_file"],
                "jobs_directory": CONFIG.get("jobs_directory"),
                "task_file_prefix": CONFIG.get("task_file_prefix", "task")
            }
        
        info = {
            "storage_config": storage_config,
            "total_tasks": len(tasks),
            "enabled_tasks": len([t for t in tasks if t.get("enabled", True)]),
            "disabled_tasks": len([t for t in tasks if not t.get("enabled", True)]),
            "storage_size": "N/A"  # 可以添加实际文件大小计算
        }
        
        # 如果是JSON存储且有jobs目录，计算存储大小
        if CONFIG["storage_type"] == "json" and CONFIG.get("jobs_directory"):
            try:
                total_size = 0
                if os.path.exists(CONFIG["jobs_directory"]):
                    for filename in os.listdir(CONFIG["jobs_directory"]):
                        if filename.endswith('.json'):
                            file_path = os.path.join(CONFIG["jobs_directory"], filename)
                            total_size += os.path.getsize(file_path)
                info["storage_size"] = f"{total_size} bytes"
            except Exception as e:
                info["storage_size"] = f"计算失败: {str(e)}"
        
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

@app.get("/api/download")
async def download_file(filepath: str):
    """下载文件"""
    try:
        # 安全检查：确保文件路径在exports目录内
        if not filepath.startswith('./exports/') and not filepath.startswith('exports/'):
            raise HTTPException(status_code=400, detail="无效的文件路径")
        
        # 标准化文件路径
        if filepath.startswith('./'):
            filepath = filepath[2:]
        
        # 检查文件是否存在
        if not os.path.exists(filepath):
            raise HTTPException(status_code=404, detail="文件不存在")
        
        # 获取文件名
        filename = os.path.basename(filepath)
        
        # 读取文件内容
        with open(filepath, 'rb') as f:
            content = f.read()
        
        # 返回文件下载响应
        from fastapi.responses import Response
        return Response(
            content=content,
            media_type='application/octet-stream',
            headers={
                'Content-Disposition': f'attachment; filename="{filename}"'
            }
        )
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
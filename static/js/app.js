 /**
 * Mini PM2 前端应用主文件
 * 包含 WebSocket 连接、HTMX 事件处理、工具函数等
 */

// 全局变量
let ws = null;
let reconnectAttempts = 0;
const MAX_RECONNECT_ATTEMPTS = 5;
const RECONNECT_DELAY = 3000;

// 应用状态
const AppState = {
  isConnected: false,
  autoScroll: true,
  logCount: 0,
  errorCount: 0,
  warningCount: 0,
  infoCount: 0,
  debugCount: 0,
  allLogs: [],
  taskStatus: {},
  settings: {
    logLimit: 500,
    checkInterval: 30,
    storageType: 'json',
    redisUrl: 'localhost:6379',
    redisDb: 0
  }
};

/**
 * WebSocket 连接管理
 */
class WebSocketManager {
  constructor() {
    this.ws = null;
    this.reconnectAttempts = 0;
    this.isConnecting = false;
  }

  connect() {
    if (this.isConnecting) return;
    
    this.isConnecting = true;
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const wsUrl = `${protocol}//${window.location.host}/ws/logs`;
    
    try {
      this.ws = new WebSocket(wsUrl);
      this.setupEventHandlers();
    } catch (error) {
      console.error('WebSocket 连接失败:', error);
      this.handleConnectionError();
    }
  }

  setupEventHandlers() {
    this.ws.onopen = () => {
      console.log('WebSocket 已连接');
      this.isConnecting = false;
      this.reconnectAttempts = 0;
      AppState.isConnected = true;
      this.updateConnectionStatus(true);
      this.showToast('连接已建立', 'success');
    };

    this.ws.onclose = (event) => {
      console.log('WebSocket 连接已关闭:', event.code, event.reason);
      this.isConnecting = false;
      AppState.isConnected = false;
      this.updateConnectionStatus(false);
      
      if (!event.wasClean) {
        this.handleConnectionError();
      }
    };

    this.ws.onerror = (error) => {
      console.error('WebSocket 错误:', error);
      this.handleConnectionError();
    };

    this.ws.onmessage = (event) => {
      try {
        const data = JSON.parse(event.data);
        this.handleMessage(data);
      } catch (error) {
        console.error('消息解析错误:', error);
      }
    };
  }

  handleMessage(data) {
    switch (data.type) {
      case 'log':
        this.appendLog(data.message);
        break;
      case 'status':
        this.updateTaskStatus(data.data);
        break;
      case 'stats':
        this.updateStats(data.data);
        break;
      case 'error':
        this.showToast(data.message, 'error');
        break;
      default:
        console.log('未知消息类型:', data.type);
    }
  }

  handleConnectionError() {
    if (this.reconnectAttempts < MAX_RECONNECT_ATTEMPTS) {
      this.reconnectAttempts++;
      console.log(`尝试重连 (${this.reconnectAttempts}/${MAX_RECONNECT_ATTEMPTS})...`);
      
      setTimeout(() => {
        this.connect();
      }, RECONNECT_DELAY * this.reconnectAttempts);
    } else {
      console.error('WebSocket 重连失败，已达到最大重试次数');
      this.showToast('连接失败，请刷新页面重试', 'error');
    }
  }

  updateConnectionStatus(isConnected) {
    const statusEl = document.getElementById('connection-status');
    if (!statusEl) return;

    const container = statusEl.parentElement;
    
    if (isConnected) {
      statusEl.textContent = '已连接';
      container.className = 'flex items-center gap-2 px-3 py-1 bg-green-100 text-green-800 rounded-full text-sm';
      container.querySelector('div').className = 'w-2 h-2 bg-green-500 rounded-full';
    } else {
      statusEl.textContent = '已断开';
      container.className = 'flex items-center gap-2 px-3 py-1 bg-red-100 text-red-800 rounded-full text-sm';
      container.querySelector('div').className = 'w-2 h-2 bg-red-500 rounded-full';
    }
  }

  send(message) {
    if (this.ws && this.ws.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify(message));
    }
  }

  disconnect() {
    if (this.ws) {
      this.ws.close();
    }
  }
}

/**
 * 日志管理
 */
class LogManager {
  constructor() {
    this.container = document.getElementById('log-container');
    this.autoScroll = true;
  }

  appendLog(message) {
    if (!this.container) return;

    const logDiv = document.createElement('div');
    logDiv.className = this.getLogClass(message);
    logDiv.textContent = message;
    
    this.container.appendChild(logDiv);
    
    if (this.autoScroll) {
      this.container.scrollTop = this.container.scrollHeight;
    }

    // 更新统计
    this.updateLogStats(message);
    
    // 限制日志数量
    this.limitLogs();
  }

  getLogClass(message) {
    if (message.includes('[ERROR]')) return 'log-error';
    if (message.includes('[WARNING]')) return 'log-warning';
    if (message.includes('[DEBUG]')) return 'log-debug';
    return 'log-info';
  }

  updateLogStats(message) {
    AppState.logCount++;
    
    if (message.includes('[ERROR]')) {
      AppState.errorCount++;
    } else if (message.includes('[WARNING]')) {
      AppState.warningCount++;
    } else if (message.includes('[DEBUG]')) {
      AppState.debugCount++;
    } else {
      AppState.infoCount++;
    }

    this.updateStatsDisplay();
  }

  updateStatsDisplay() {
    const elements = {
      'log-count': AppState.logCount,
      'error-count': AppState.errorCount,
      'warning-count': AppState.warningCount,
      'info-count': AppState.infoCount,
      'debug-count': AppState.debugCount
    };

    Object.entries(elements).forEach(([id, value]) => {
      const element = document.getElementById(id);
      if (element) {
        element.textContent = value;
      }
    });
  }

  limitLogs() {
    if (!this.container) return;
    
    const maxLogs = AppState.settings.logLimit;
    const logs = this.container.children;
    
    while (logs.length > maxLogs) {
      this.container.removeChild(logs[0]);
    }
  }

  clearLogs() {
    if (!this.container) return;
    
    this.container.innerHTML = '<div class="text-center text-gray-500">日志已清空</div>';
    
    // 重置统计
    AppState.logCount = 0;
    AppState.errorCount = 0;
    AppState.warningCount = 0;
    AppState.infoCount = 0;
    AppState.debugCount = 0;
    AppState.allLogs = [];
    
    this.updateStatsDisplay();
  }

  toggleAutoScroll() {
    this.autoScroll = !this.autoScroll;
    const btn = document.getElementById('auto-scroll-btn');
    
    if (btn) {
      if (this.autoScroll) {
        btn.innerHTML = '<svg class="w-4 h-4 mr-2" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M7 16l4-4m0 0l4 4m-4-4v12"></path></svg>自动滚动';
      } else {
        btn.innerHTML = '<svg class="w-4 h-4 mr-2" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M7 20l4-4m0 0l4 4m-4-4v12"></path></svg>手动滚动';
      }
    }
    
    showToast(`自动滚动已${this.autoScroll ? '启用' : '禁用'}`, this.autoScroll ? 'success' : 'warning');
  }

  exportLogs() {
    if (AppState.allLogs.length === 0) {
      showToast('没有日志可导出', 'warning');
      return;
    }
    
    const content = AppState.allLogs.map(log => 
      `[${log.timestamp.toISOString()}] ${log.message}`
    ).join('\n');
    
    const blob = new Blob([content], { type: 'text/plain' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `logs_${new Date().toISOString().slice(0, 19).replace(/:/g, '-')}.txt`;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
    
    showToast('日志已导出', 'success');
  }
}

/**
 * 任务管理
 */
class TaskManager {
  constructor() {
    this.tasks = [];
  }

  updateTaskStatus(statusData) {
    AppState.taskStatus = statusData;
    this.updateTaskTable();
    this.updateTaskStats();
  }

  updateTaskTable() {
    const tbody = document.getElementById('task-table');
    if (!tbody) return;

    tbody.innerHTML = '';
    
    Object.entries(AppState.taskStatus).forEach(([taskId, status]) => {
      const row = this.createTaskRow(taskId, status);
      tbody.appendChild(row);
    });
  }

  createTaskRow(taskId, status) {
    const row = document.createElement('tr');
    row.className = 'hover:bg-gray-50';
    
    const statusClass = this.getStatusClass(status.status);
    const lastRun = status.last_success || status.last_error || '从未运行';
    
    row.innerHTML = `
      <td class="px-6 py-4 whitespace-nowrap text-sm font-medium text-gray-900">${taskId}</td>
      <td class="px-6 py-4 whitespace-nowrap">
        <span class="status-badge ${statusClass}">${this.getStatusText(status.status)}</span>
      </td>
      <td class="px-6 py-4 whitespace-nowrap text-sm text-gray-500">${lastRun}</td>
      <td class="px-6 py-4 whitespace-nowrap text-sm font-medium">
        <div class="flex items-center gap-2">
          <button onclick="taskManager.runTask('${taskId}')" class="btn btn-sm btn-primary">运行</button>
          <button onclick="taskManager.toggleTask('${taskId}', ${status.status === 'DISABLED'})" class="btn btn-sm btn-secondary">
            ${status.status === 'DISABLED' ? '启用' : '禁用'}
          </button>
          <button onclick="taskManager.deleteTask('${taskId}')" class="btn btn-sm btn-error">删除</button>
        </div>
      </td>
    `;
    
    return row;
  }

  getStatusClass(status) {
    const classes = {
      'IDLE': 'status-idle',
      'RUNNING': 'status-running',
      'FAILED': 'status-failed',
      'DISABLED': 'status-disabled'
    };
    return classes[status] || 'status-disabled';
  }

  getStatusText(status) {
    const texts = {
      'IDLE': '空闲',
      'RUNNING': '运行中',
      'FAILED': '失败',
      'DISABLED': '已禁用'
    };
    return texts[status] || '未知';
  }

  updateTaskStats() {
    const total = Object.keys(AppState.taskStatus).length;
    const running = Object.values(AppState.taskStatus).filter(s => s.status === 'RUNNING').length;
    const failed = Object.values(AppState.taskStatus).filter(s => s.status === 'FAILED').length;
    
    const elements = {
      'total-tasks': total,
      'running-tasks': running,
      'failed-tasks': failed
    };

    Object.entries(elements).forEach(([id, value]) => {
      const element = document.getElementById(id);
      if (element) {
        element.textContent = value;
      }
    });
  }

  async runTask(taskId) {
    try {
      const response = await fetch(`/api/tasks/${taskId}/run`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json'
        }
      });
      
      if (response.ok) {
        showToast('任务已启动', 'success');
      } else {
        showToast('任务启动失败', 'error');
      }
    } catch (error) {
      console.error('运行任务错误:', error);
      showToast('任务启动失败', 'error');
    }
  }

  async toggleTask(taskId, enable) {
    try {
      const response = await fetch(`/api/tasks/${taskId}/toggle`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json'
        },
        body: JSON.stringify({ enable })
      });
      
      if (response.ok) {
        const message = enable ? '任务已启用' : '任务已禁用';
        showToast(message, 'success');
      } else {
        showToast('操作失败', 'error');
      }
    } catch (error) {
      console.error('切换任务状态错误:', error);
      showToast('操作失败', 'error');
    }
  }

  async deleteTask(taskId) {
    if (!confirm(`确定要删除任务 ${taskId} 吗？`)) {
      return;
    }
    
    try {
      const response = await fetch(`/api/tasks/${taskId}`, {
        method: 'DELETE'
      });
      
      if (response.ok) {
        showToast('任务已删除', 'success');
      } else {
        showToast('删除失败', 'error');
      }
    } catch (error) {
      console.error('删除任务错误:', error);
      showToast('删除失败', 'error');
    }
  }
}

/**
 * 工具函数
 */
const Utils = {
  // 显示提示消息
  showToast(message, type = 'info') {
    const toast = document.createElement('div');
    const colors = {
      success: 'bg-green-500',
      error: 'bg-red-500',
      warning: 'bg-yellow-500',
      info: 'bg-blue-500'
    };
    
    toast.className = `fixed top-4 left-1/2 z-50 ${colors[type]} text-white px-6 py-3 rounded-lg shadow-lg`;
    toast.style.transform = 'translateX(-50%)';
    toast.style.transition = 'none';
    toast.textContent = message;
    document.body.appendChild(toast);
    
    setTimeout(() => {
      toast.remove();
    }, 3000);
  },

  // 确认操作
  confirmAction(message, callback) {
    if (confirm(message)) {
      callback();
    }
  },

  // 格式化时间
  formatTime(date) {
    return new Intl.DateTimeFormat('zh-CN', {
      year: 'numeric',
      month: '2-digit',
      day: '2-digit',
      hour: '2-digit',
      minute: '2-digit',
      second: '2-digit'
    }).format(date);
  },

  // 格式化文件大小
  formatFileSize(bytes) {
    if (bytes === 0) return '0 B';
    const k = 1024;
    const sizes = ['B', 'KB', 'MB', 'GB'];
    const i = Math.floor(Math.log(bytes) / Math.log(k));
    return parseFloat((bytes / Math.pow(k, i)).toFixed(2)) + ' ' + sizes[i];
  },

  // 防抖函数
  debounce(func, wait) {
    let timeout;
    return function executedFunction(...args) {
      const later = () => {
        clearTimeout(timeout);
        func(...args);
      };
      clearTimeout(timeout);
      timeout = setTimeout(later, wait);
    };
  },

  // 节流函数
  throttle(func, limit) {
    let inThrottle;
    return function() {
      const args = arguments;
      const context = this;
      if (!inThrottle) {
        func.apply(context, args);
        inThrottle = true;
        setTimeout(() => inThrottle = false, limit);
      }
    };
  }
};

/**
 * HTMX 事件处理
 */
class HTMXHandler {
  constructor() {
    this.setupEventListeners();
  }

  setupEventListeners() {
    // HTMX 请求前
    document.body.addEventListener('htmx:beforeRequest', (evt) => {
      this.showLoading(evt.detail.elt);
    });

    // HTMX 请求后
    document.body.addEventListener('htmx:afterRequest', (evt) => {
      this.hideLoading(evt.detail.elt);
      
      if (evt.detail.xhr.status === 200) {
        Utils.showToast('操作成功', 'success');
      } else {
        Utils.showToast('操作失败', 'error');
      }
    });

    // HTMX 错误处理
    document.body.addEventListener('htmx:responseError', (evt) => {
      console.error('HTMX 错误:', evt.detail);
      Utils.showToast('请求失败', 'error');
    });
  }

  showLoading(element) {
    if (element && element.tagName === 'BUTTON') {
      element.disabled = true;
      element.classList.add('loading');
    }
  }

  hideLoading(element) {
    if (element && element.tagName === 'BUTTON') {
      element.disabled = false;
      element.classList.remove('loading');
    }
  }
}

/**
 * 应用初始化
 */
class App {
  constructor() {
    this.wsManager = new WebSocketManager();
    this.logManager = new LogManager();
    this.taskManager = new TaskManager();
    this.htmxHandler = new HTMXHandler();
  }

  init() {
    console.log('Mini PM2 应用初始化...');
    
    // 连接 WebSocket
    this.wsManager.connect();
    
    // 设置全局函数
    window.showToast = Utils.showToast;
    window.confirmAction = Utils.confirmAction;
    window.taskManager = this.taskManager;
    window.logManager = this.logManager;
    
    // 页面卸载时清理
    window.addEventListener('beforeunload', () => {
      this.wsManager.disconnect();
    });
    
    console.log('应用初始化完成');
  }
}

// 全局实例
let app;

// 页面加载完成后初始化
document.addEventListener('DOMContentLoaded', () => {
  app = new App();
  app.init();
});

// 导出模块（如果使用模块系统）
if (typeof module !== 'undefined' && module.exports) {
  module.exports = {
    App,
    WebSocketManager,
    LogManager,
    TaskManager,
    Utils
  };
}